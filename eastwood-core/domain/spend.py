"""Outbound spend: estimation, the ledger, and the budget gate.

Costs are ESTIMATES from configured unit prices (COST_* settings) applied to
what the telephony provider reports — billable call minutes (rounded up per
call, as Twilio bills), AI and transcription minutes, and SMS segments. Check
them against real invoices and adjust the unit prices.

The budget gate runs before every dial. It reserves the cost of a
full-length call (Settings.call_reserve_usd: MAX_CALL_MINUTES at the unit
prices) for each call in flight plus the one about to start. Because the
provider ends every call at MAX_CALL_MINUTES, no call can cost more than its
reserve, so the ceilings are hard limits (on estimated spend). Texts are
checked individually before sending (sms_within_budget).

  exhausted  the remaining budget cannot cover even one call → campaign:
             outbound is HALTED (domain.outbound) and the campaign marked
             budget_exhausted; daily: nothing more is dialed until tomorrow
  wait       room exists once in-flight calls settle → skip this tick
  ok         dial
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from config.settings import get_settings
from db.models import SpendEntry

logger = logging.getLogger(__name__)

SMS_SEGMENT_CHARS = 153  # concatenated GSM-7 segment


def call_cost_lines(duration_seconds: int | None, settings=None) -> list[tuple[str, float, float]]:
    """(kind, quantity, unit_cost) lines for one finished call. Unanswered → none."""
    s = settings or get_settings()
    secs = int(duration_seconds or 0)
    if secs <= 0:
        return []
    minutes = secs / 60.0
    return [
        ("voice", float(math.ceil(minutes)), s.cost_voice_per_min_usd),
        ("ai", round(minutes, 4), s.cost_ai_per_min_usd),
        ("transcribe", round(minutes, 4), s.cost_transcribe_per_min_usd),
    ]


def sms_segments(body: str) -> int:
    return max(1, math.ceil(len(body or "") / SMS_SEGMENT_CHARS))


def day_start_utc(now: datetime, tz_name: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


async def add_lines(session, *, tenant_id: str, campaign_id, reference: str | None,
                    lines: list[tuple[str, float, float]]) -> float:
    total = 0.0
    for kind, qty, unit in lines:
        amount = round(qty * unit, 4)
        total += amount
        session.add(SpendEntry(tenant_id=tenant_id, campaign_id=campaign_id, kind=kind, quantity=qty,
                               unit_cost_usd=unit, amount_usd=amount, reference=(reference or "")[:128]))
    if lines:
        await session.flush()
        try:
            from metrics.metrics import spend_usd_total

            for kind, qty, unit in lines:
                spend_usd_total.labels(kind=kind).inc(round(qty * unit, 4))
        except Exception:  # metrics never break the ledger
            logger.debug("spend metric update failed", exc_info=True)
    return round(total, 4)


async def record_sms(session, *, tenant_id: str, campaign_id, body: str, reference: str | None) -> float:
    s = get_settings()
    return await add_lines(session, tenant_id=tenant_id, campaign_id=campaign_id, reference=reference,
                           lines=[("sms", float(sms_segments(body)), s.cost_sms_per_segment_usd)])


async def spent(session, *, tenant_id: str, since: datetime | None = None, campaign_id=None) -> float:
    q = select(func.coalesce(func.sum(SpendEntry.amount_usd), 0)).where(SpendEntry.tenant_id == tenant_id)
    if since is not None:
        q = q.where(SpendEntry.created_at >= since)
    if campaign_id is not None:
        q = q.where(SpendEntry.campaign_id == campaign_id)
    return float((await session.execute(q)).scalar_one())


@dataclass(frozen=True)
class BudgetDecision:
    verdict: str      # ok | wait | daily_exhausted | campaign_exhausted
    detail: str = ""


async def check_budget(session, *, tenant_id: str, campaign, in_flight_total: int,
                       in_flight_campaign: int, now: datetime | None = None) -> BudgetDecision:
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    reserve = s.call_reserve_usd()

    today = await spent(session, tenant_id=tenant_id, since=day_start_utc(now, s.calendly_default_timezone))
    daily_limit = s.outbound_daily_spend_limit_usd
    if daily_limit - today < reserve:
        return BudgetDecision("daily_exhausted", f"spent ${today:.2f} of ${daily_limit:.2f} today")
    if daily_limit - today - in_flight_total * reserve < reserve:
        return BudgetDecision("wait", "daily budget reserved by calls in flight")

    if campaign is not None:
        c_total = await spent(session, tenant_id=tenant_id, campaign_id=campaign.id)
        budget = float(campaign.budget_usd)
        if budget - c_total < reserve:
            return BudgetDecision("campaign_exhausted", f"campaign spent ${c_total:.2f} of ${budget:.2f}")
        if budget - c_total - in_flight_campaign * reserve < reserve:
            return BudgetDecision("wait", "campaign budget reserved by calls in flight")
        if campaign.daily_budget_usd is not None:
            c_today = await spent(session, tenant_id=tenant_id, campaign_id=campaign.id,
                                  since=day_start_utc(now, s.calendly_default_timezone))
            c_daily = float(campaign.daily_budget_usd)
            if c_daily - c_today < reserve:
                return BudgetDecision("daily_exhausted", f"campaign spent ${c_today:.2f} of ${c_daily:.2f} today")
            if c_daily - c_today - in_flight_campaign * reserve < reserve:
                return BudgetDecision("wait", "campaign daily budget reserved by calls in flight")
    return BudgetDecision("ok")


def next_day_start(now: datetime, tz_name: str) -> datetime:
    return day_start_utc(now, tz_name) + timedelta(days=1)


async def sms_within_budget(session, *, tenant_id: str, campaign_id, cost: float,
                            now: datetime | None = None) -> bool:
    """Whether one more text fits under the daily ceiling and its campaign's budget."""
    from db.models import Campaign

    s = get_settings()
    now = now or datetime.now(timezone.utc)
    today = await spent(session, tenant_id=tenant_id, since=day_start_utc(now, s.calendly_default_timezone))
    if today + cost > s.outbound_daily_spend_limit_usd:
        return False
    if campaign_id is not None:
        campaign = (await session.execute(select(Campaign).where(Campaign.id == campaign_id))).scalars().first()
        if campaign is not None:
            c_total = await spent(session, tenant_id=tenant_id, campaign_id=campaign_id)
            if c_total + cost > float(campaign.budget_usd):
                return False
    return True
