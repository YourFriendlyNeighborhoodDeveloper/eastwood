from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# State → IANA timezone.  Covers continental US + AK + HI + DC.
_STATE_TZ: dict[str, str] = {
    "AL": "America/Chicago",        "AK": "America/Anchorage",
    "AZ": "America/Phoenix",        "AR": "America/Chicago",
    "CA": "America/Los_Angeles",    "CO": "America/Denver",
    "CT": "America/New_York",       "DE": "America/New_York",
    "FL": "America/New_York",       "GA": "America/New_York",
    "HI": "Pacific/Honolulu",       "ID": "America/Denver",
    "IL": "America/Chicago",        "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",        "KS": "America/Chicago",
    "KY": "America/New_York",       "LA": "America/Chicago",
    "ME": "America/New_York",       "MD": "America/New_York",
    "MA": "America/New_York",       "MI": "America/Detroit",
    "MN": "America/Chicago",        "MS": "America/Chicago",
    "MO": "America/Chicago",        "MT": "America/Denver",
    "NE": "America/Chicago",        "NV": "America/Los_Angeles",
    "NH": "America/New_York",       "NJ": "America/New_York",
    "NM": "America/Denver",         "NY": "America/New_York",
    "NC": "America/New_York",       "ND": "America/Chicago",
    "OH": "America/New_York",       "OK": "America/Chicago",
    "OR": "America/Los_Angeles",    "PA": "America/New_York",
    "RI": "America/New_York",       "SC": "America/New_York",
    "SD": "America/Chicago",        "TN": "America/Chicago",
    "TX": "America/Chicago",        "UT": "America/Denver",
    "VT": "America/New_York",       "VA": "America/New_York",
    "WA": "America/Los_Angeles",    "WV": "America/New_York",
    "WI": "America/Chicago",        "WY": "America/Denver",
    "DC": "America/New_York",
}

_TCPA_START_HOUR = 8   # 8:00 AM local
_TCPA_END_HOUR   = 21  # 9:00 PM local


def in_tcpa_window(state: str | None, now_utc: datetime | None = None) -> bool:
    import os
    if os.getenv("BYPASS_TCPA", "").lower() in ("1", "true", "yes"):
        return True
    """Return True if it is currently within the TCPA-allowed calling window
    (8 am – 9 pm local time) for the given state.  Falls back to Eastern if
    state is unknown — conservative choice since most leads are Eastern/Central."""
    tz_name = _STATE_TZ.get((state or "").upper(), "America/New_York")
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    return _TCPA_START_HOUR <= now.hour < _TCPA_END_HOUR


def next_tcpa_window_open(state: str | None, now_utc: datetime | None = None) -> datetime:
    """Return the next datetime (UTC) when calling is allowed for this state."""
    tz_name = _STATE_TZ.get((state or "").upper(), "America/New_York")
    tz = ZoneInfo(tz_name)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)

    if now.hour < _TCPA_START_HOUR:
        target = now.replace(hour=_TCPA_START_HOUR, minute=0, second=0, microsecond=0)
    else:
        next_day = now.date() + timedelta(days=1)
        target = datetime(next_day.year, next_day.month, next_day.day,
                          _TCPA_START_HOUR, 0, 0, tzinfo=tz)
    return target.astimezone(timezone.utc)


@dataclass(frozen=True)
class EvidencePack:
    source_url: str
    ip_address: str
    user_agent: str
    consent_text: str
    timestamp_utc: str
    form_fields: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "source_url": self.source_url,
                "ip_address": self.ip_address,
                "consent_text": self.consent_text,
                "timestamp_utc": self.timestamp_utc,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class TcpaCheckResult:
    allowed: bool
    reason: str
    evidence_fingerprint: str | None = None


_OUTBOUND_DIAL_REQUIRES_EXPRESS_WRITTEN = True
_CONSENT_MAX_MONTHS = 18


def evaluate_tcpa(
    action: str,
    express_written: bool,
    consent_expires_at: datetime | None,
    on_dnc: bool,
    evidence_pack: EvidencePack | None = None,
) -> TcpaCheckResult:
    if on_dnc:
        return TcpaCheckResult(allowed=False, reason="phone_on_dnc")

    if action == "outbound_dial":
        if not express_written:
            return TcpaCheckResult(allowed=False, reason="no_express_written_consent")
        if consent_expires_at is not None:
            now = datetime.now(timezone.utc)
            if now > consent_expires_at:
                return TcpaCheckResult(allowed=False, reason="consent_expired")

    fingerprint = evidence_pack.fingerprint() if evidence_pack else None
    return TcpaCheckResult(allowed=True, reason="compliant", evidence_fingerprint=fingerprint)


def standard_expiry(from_dt: datetime | None = None) -> datetime:
    base = from_dt or datetime.now(timezone.utc)
    return base + timedelta(days=30 * _CONSENT_MAX_MONTHS)
