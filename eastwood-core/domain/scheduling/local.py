"""The licensed agent's own calendar: part-time availability without overbooking.

AGENT_AVAILABILITY describes weekly windows in the agent's timezone
(CALENDLY_DEFAULT_TIMEZONE), e.g. "mon-fri 17:00-20:00; sat 09:00-12:00".
Slots are APPOINTMENT_SLOT_MINUTES long, spaced by APPOINTMENT_BUFFER_MINUTES,
no sooner than APPOINTMENT_MIN_NOTICE_HOURS, at most APPOINTMENT_MAX_PER_DAY,
within APPOINTMENT_HORIZON_DAYS, never on AGENT_BLACKOUT_DATES.

Overbooking is impossible by construction:
  1. only grid-aligned starts inside a window can be reserved (no overlaps);
  2. reservation takes a transaction-scoped advisory lock for that local day,
     then checks "already taken" and the daily cap before inserting;
  3. a partial unique index on booking_slots(tenant_id, starts_at) WHERE
     available = false backs it up if anything slips past (1) and (2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from db.models import BookingSlot

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class AvailabilityConfigError(ValueError):
    pass


class SlotUnavailableError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_availability(spec: str) -> dict[int, list[tuple[time, time]]]:
    """"mon-fri 17:00-20:00; sat 09:00-12:00, 13:00-15:00" → weekday → windows."""
    out: dict[int, list[tuple[time, time]]] = {}
    for part in [p.strip() for p in (spec or "").split(";") if p.strip()]:
        m = re.match(r"^([a-z]{3})(?:\s*-\s*([a-z]{3}))?\s+(.+)$", part.lower())
        if not m or m.group(1) not in _DAYS or (m.group(2) and m.group(2) not in _DAYS):
            raise AvailabilityConfigError(f"cannot parse availability entry {part!r}")
        first, last = _DAYS[m.group(1)], _DAYS[m.group(2) or m.group(1)]
        days = list(range(first, last + 1)) if first <= last else list(range(first, 7)) + list(range(0, last + 1))
        for window in m.group(3).split(","):
            w = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", window)
            if not w:
                raise AvailabilityConfigError(f"cannot parse time window {window!r}")
            start, end = time(int(w.group(1)), int(w.group(2))), time(int(w.group(3)), int(w.group(4)))
            if end <= start:
                raise AvailabilityConfigError(f"window ends before it starts: {window!r}")
            for d in days:
                out.setdefault(d, []).append((start, end))
    return out


@dataclass
class LocalCalendar:
    windows: dict[int, list[tuple[time, time]]]
    tz: ZoneInfo
    slot_minutes: int = 30
    buffer_minutes: int = 0
    max_per_day: int = 4
    min_notice_hours: int = 2
    horizon_days: int = 14
    blackouts: frozenset[date] = frozenset()

    @classmethod
    def from_settings(cls, s) -> "LocalCalendar | None":
        if not (s.agent_availability or "").strip():
            return None
        blackouts = frozenset(date.fromisoformat(d.strip()) for d in s.agent_blackout_dates.split(",") if d.strip())
        return cls(
            windows=parse_availability(s.agent_availability), tz=ZoneInfo(s.calendly_default_timezone),
            slot_minutes=s.appointment_slot_minutes, buffer_minutes=s.appointment_buffer_minutes,
            max_per_day=s.appointment_max_per_day, min_notice_hours=s.appointment_min_notice_hours,
            horizon_days=s.appointment_horizon_days, blackouts=blackouts,
        )

    def candidate_starts(self, now: datetime) -> list[datetime]:
        local_now = now.astimezone(self.tz)
        earliest = now + timedelta(hours=self.min_notice_hours)
        step = timedelta(minutes=self.slot_minutes + self.buffer_minutes)
        length = timedelta(minutes=self.slot_minutes)
        out: list[datetime] = []
        for offset in range(self.horizon_days + 1):
            day = local_now.date() + timedelta(days=offset)
            if day in self.blackouts:
                continue
            for start_t, end_t in self.windows.get(day.weekday(), []):
                cur = datetime.combine(day, start_t, tzinfo=self.tz)
                end = datetime.combine(day, end_t, tzinfo=self.tz)
                while cur + length <= end:
                    if cur >= earliest:
                        out.append(cur)
                    cur += step
        return out

    async def _reserved(self, session, tenant_id: str, start: datetime, end: datetime) -> list[datetime]:
        return list((await session.execute(
            select(BookingSlot.starts_at).where(
                BookingSlot.tenant_id == tenant_id, BookingSlot.available.is_(False),
                BookingSlot.starts_at >= start, BookingSlot.starts_at < end,
            )
        )).scalars())

    async def available_times(self, session, tenant_id: str, *, now: datetime | None = None,
                              limit: int | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        candidates = self.candidate_starts(now)
        if not candidates:
            return []
        reserved = await self._reserved(session, tenant_id, candidates[0], candidates[-1] + timedelta(days=1))
        taken = {r.astimezone(timezone.utc) for r in reserved}
        per_day: dict[date, int] = {}
        for r in reserved:
            d = r.astimezone(self.tz).date()
            per_day[d] = per_day.get(d, 0) + 1
        out = []
        for c in candidates:
            if c.astimezone(timezone.utc) in taken or per_day.get(c.date(), 0) >= self.max_per_day:
                continue
            out.append(c.isoformat())
            if limit and len(out) >= limit:
                break
        return out

    async def reserve(self, session, tenant_id: str, start: datetime, *, now: datetime | None = None) -> BookingSlot:
        """Reserve one slot inside the caller's transaction, or raise SlotUnavailableError."""
        now = now or datetime.now(timezone.utc)
        if start.tzinfo is None:
            raise SlotUnavailableError("start_time must include a UTC offset")
        start_utc = start.astimezone(timezone.utc)
        if start_utc not in {c.astimezone(timezone.utc) for c in self.candidate_starts(now)}:
            raise SlotUnavailableError("not_an_available_time")
        local_day = start.astimezone(self.tz).date()
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                              {"k": f"calendar:{tenant_id}:{local_day.isoformat()}"})
        day_start = datetime.combine(local_day, time(0), tzinfo=self.tz)
        reserved = await self._reserved(session, tenant_id, day_start, day_start + timedelta(days=1))
        if any(r.astimezone(timezone.utc) == start_utc for r in reserved):
            raise SlotUnavailableError("slot_taken")
        if len(reserved) >= self.max_per_day:
            raise SlotUnavailableError("day_full")
        slot = BookingSlot(tenant_id=tenant_id, starts_at=start_utc,
                           ends_at=start_utc + timedelta(minutes=self.slot_minutes), available=False)
        session.add(slot)
        await session.flush()
        return slot
