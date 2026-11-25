"""Campaigns: a named, budgeted, explicitly-enqueued outbound effort.

Importing leads never dials them. An operator creates a campaign (budget,
daily budget, concurrency, rate, states, products), enqueues the leads it may
call, and activates it. The dialer then only takes queue entries whose campaign
is active — and still applies every per-lead gate at dial time.

Enqueue selects leads that are, at that moment: authorized for AI-voice
contact, National-DNC clear/exempt, not suppressed, in the campaign's states
(which must be within ACTIVE_STATES), of the campaign's products, not already
queued, and in status imported/qualified. Everything else is counted by reason.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from config.settings import get_settings
from db.models import Campaign
from domain.tcpa import supported_states

DIALABLE_SOURCE_STATUSES = ("imported", "qualified")


class CampaignError(ValueError):
    pass


def validate_campaign(data: dict) -> dict:
    s = get_settings()
    name = str(data.get("name") or "").strip()
    if not name:
        raise CampaignError("name is required")
    try:
        budget = float(data["budget_usd"])
    except (KeyError, TypeError, ValueError):
        raise CampaignError("budget_usd is required") from None
    if budget <= 0:
        raise CampaignError("budget_usd must be positive")
    daily = data.get("daily_budget_usd")
    daily = float(daily) if daily not in (None, "") else None
    if daily is not None and daily <= 0:
        raise CampaignError("daily_budget_usd must be positive")
    concurrent = int(data.get("max_concurrent_calls") or 1)
    if not 1 <= concurrent <= s.max_concurrent_calls:
        raise CampaignError(f"max_concurrent_calls must be 1..{s.max_concurrent_calls} (MAX_CONCURRENT_CALLS)")
    cpm = int(data.get("calls_per_minute") or min(10, s.dial_calls_per_minute))
    if not 1 <= cpm <= s.dial_calls_per_minute:
        raise CampaignError(f"calls_per_minute must be 1..{s.dial_calls_per_minute} (DIAL_CALLS_PER_MINUTE)")
    states = [str(x).strip().upper() for x in (data.get("states") or s.active_states_list())]
    unknown = sorted(set(states) - supported_states())
    if unknown:
        raise CampaignError(f"unsupported states: {', '.join(unknown)}")
    outside = sorted(set(states) - set(s.active_states_list()))
    if outside:
        raise CampaignError(f"states outside ACTIVE_STATES: {', '.join(outside)}")
    products = [str(x).strip() for x in (data.get("product_lines") or []) if str(x).strip()] or None
    strategy = str(data.get("channel_strategy") or "sms_first").strip().lower()
    if strategy not in ("sms_first", "voice_first"):
        raise CampaignError("channel_strategy must be sms_first or voice_first")
    if strategy == "sms_first":
        # Imogen quotes final expense / life by text; Medicare needs a CMS-compliant script.
        products = [p for p in (products or ["final_expense", "life"]) if p in ("final_expense", "life")] or None
        if not products:
            raise CampaignError("sms_first campaigns quote final_expense or life only")
    mpm = int(data.get("messages_per_minute") or 20)
    if not 1 <= mpm <= s.sms_messages_per_minute:
        raise CampaignError(f"messages_per_minute must be 1..{s.sms_messages_per_minute} (SMS_MESSAGES_PER_MINUTE)")
    cap = data.get("daily_message_cap")
    cap = int(cap) if cap not in (None, "") else None
    return {"name": name[:128], "budget_usd": budget, "daily_budget_usd": daily,
            "max_concurrent_calls": concurrent, "calls_per_minute": cpm, "states": states,
            "product_lines": products, "notes": data.get("notes"), "channel_strategy": strategy,
            "messages_per_minute": mpm, "daily_message_cap": cap,
            "escalate_to_voice": bool(data.get("escalate_to_voice", False))}


async def create_campaign(session, data: dict, tenant_id: str) -> Campaign:
    c = Campaign(tenant_id=tenant_id, status="draft", **validate_campaign(data))
    session.add(c)
    await session.flush()
    return c


async def get_campaign(session, campaign_id, tenant_id: str) -> Campaign | None:
    try:
        cid = uuid.UUID(str(campaign_id))
    except ValueError:
        return None
    return (await session.execute(
        select(Campaign).where(Campaign.id == cid, Campaign.tenant_id == tenant_id)
    )).scalars().first()


_ELIGIBLE = """
    l.tenant_id = :t
    AND l.status = ANY(:statuses)
    AND coalesce((l.contact_authorization->>'ai_voice')::boolean, false)
    AND l.national_dnc_status IN ('clear', 'exempt')
    AND l.state = ANY(:states)
    AND (CAST(:products AS text[]) IS NULL OR l.product_line = ANY(CAST(:products AS text[])))
    AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.tenant_id = l.tenant_id AND s.phone_e164 = l.phone_e164)
    AND NOT EXISTS (SELECT 1 FROM dial_queue d WHERE d.lead_id = l.id AND d.status IN ('pending', 'dialing'))
"""


_SMS_ELIGIBLE = """
    l.tenant_id = :t
    AND l.status = ANY(:statuses)
    AND coalesce((l.contact_authorization->>'sms')::boolean, false)
    AND l.national_dnc_status IN ('clear', 'exempt')
    AND l.state = ANY(:states)
    AND (CAST(:products AS text[]) IS NULL OR coalesce(l.product_line, 'final_expense') = ANY(CAST(:products AS text[])))
    AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.tenant_id = l.tenant_id AND s.phone_e164 = l.phone_e164)
    AND NOT EXISTS (SELECT 1 FROM sms_conversations c WHERE c.tenant_id = l.tenant_id AND c.lead_id = l.id)
"""


async def enqueue_sms(session, campaign: Campaign, *, limit: int | None = None) -> dict:
    """SMS-first: open a (pending) conversation for every lead this campaign may
    text. The engine sends the opener under all gates; nothing is sent here."""
    params = {"t": campaign.tenant_id, "statuses": list(DIALABLE_SOURCE_STATUSES),
              "states": campaign.states or [], "products": campaign.product_lines, "cid": campaign.id,
              "lim": limit if limit and limit > 0 else None}
    inserted = (await session.execute(text(f"""
        WITH picked AS (
            SELECT l.id, l.tenant_id, l.phone_e164 FROM leads l WHERE {_SMS_ELIGIBLE}
            ORDER BY l.triage_priority DESC NULLS LAST, l.score DESC NULLS LAST, l.acquired_at DESC NULLS LAST
            LIMIT :lim
        ), ins AS (
            INSERT INTO sms_conversations (id, tenant_id, lead_id, campaign_id, phone_e164, status, stage, slots,
                                           followups_sent, inbound_count, outbound_count, next_action,
                                           next_action_at, llm_tokens_in, llm_tokens_out, cost_usd,
                                           created_at, updated_at)
            SELECT gen_random_uuid(), tenant_id, id, :cid, phone_e164, 'pending', 'outreach', '{{}}'::jsonb,
                   0, 0, 0, 'outreach', now(), 0, 0, 0, now(), now()
            FROM picked
            ON CONFLICT (tenant_id, lead_id) DO NOTHING
            RETURNING lead_id
        )
        UPDATE leads SET status = 'in_conversation', updated_at = now() WHERE id IN (SELECT lead_id FROM ins)
        RETURNING id
    """), params)).rowcount
    blocked = (await session.execute(text("""
        SELECT CASE
          WHEN EXISTS (SELECT 1 FROM sms_conversations c WHERE c.lead_id = l.id) THEN 'in_conversation'
          WHEN NOT coalesce((l.contact_authorization->>'sms')::boolean, false) THEN 'not_authorized_sms'
          WHEN l.state IS NULL OR NOT (l.state = ANY(:states)) THEN 'outside_campaign_states'
          WHEN EXISTS (SELECT 1 FROM suppressions s WHERE s.tenant_id = l.tenant_id
                       AND s.phone_e164 = l.phone_e164) THEN 'suppressed'
          WHEN l.national_dnc_status NOT IN ('clear','exempt') THEN 'dnc_' || l.national_dnc_status
          WHEN NOT (l.status = ANY(:statuses)) THEN 'status_' || l.status
          ELSE 'over_limit_or_product' END AS reason, count(*)
        FROM leads l WHERE l.tenant_id = :t GROUP BY 1 ORDER BY 2 DESC
    """), params)).all()
    return {"channel": "sms", "enqueued": inserted, "breakdown_after_enqueue": {r: n for r, n in blocked}}


async def enqueue_leads(session, campaign: Campaign, *, limit: int | None = None) -> dict:
    s = get_settings()
    params = {"t": campaign.tenant_id, "statuses": list(DIALABLE_SOURCE_STATUSES),
              "states": campaign.states or [], "products": campaign.product_lines,
              "cid": campaign.id, "max_attempts": s.dial_max_attempts,
              "lim": limit if limit and limit > 0 else None}
    inserted = (await session.execute(text(f"""
        WITH picked AS (
            SELECT l.id, l.tenant_id, coalesce(l.triage_priority, 0) AS priority
            FROM leads l WHERE {_ELIGIBLE}
            ORDER BY l.triage_priority DESC NULLS LAST, l.score DESC NULLS LAST, l.acquired_at DESC NULLS LAST
            LIMIT :lim
        ), ins AS (
            INSERT INTO dial_queue (id, lead_id, tenant_id, scheduled_at, priority, attempt_count,
                                    max_attempts, status, campaign_id, created_at, updated_at)
            SELECT gen_random_uuid(), id, tenant_id, now(), priority, 0, :max_attempts, 'pending', :cid, now(), now()
            FROM picked
            RETURNING lead_id
        )
        UPDATE leads SET status = 'queued', updated_at = now() WHERE id IN (SELECT lead_id FROM ins)
        RETURNING id
    """), params)).rowcount

    # Where every lead stands afterwards (each counted under its first reason).
    blocked = (await session.execute(text("""
        SELECT CASE
          WHEN l.status = 'queued' OR EXISTS (SELECT 1 FROM dial_queue d WHERE d.lead_id = l.id
                                              AND d.status IN ('pending','dialing')) THEN 'already_queued'
          WHEN l.status = 'quarantined' OR NOT coalesce((l.contact_authorization->>'ai_voice')::boolean, false)
               THEN 'not_authorized_ai_voice'
          WHEN NOT (l.state = ANY(:states)) OR l.state IS NULL THEN 'outside_campaign_states'
          WHEN CAST(:products AS text[]) IS NOT NULL AND (l.product_line IS NULL
               OR NOT l.product_line = ANY(CAST(:products AS text[]))) THEN 'outside_campaign_products'
          WHEN EXISTS (SELECT 1 FROM suppressions s WHERE s.tenant_id = l.tenant_id
                       AND s.phone_e164 = l.phone_e164) THEN 'suppressed'
          WHEN l.national_dnc_status NOT IN ('clear','exempt') THEN 'dnc_' || l.national_dnc_status
          WHEN NOT (l.status = ANY(:statuses)) THEN 'status_' || l.status
          ELSE 'over_limit' END AS reason, count(*) AS leads
        FROM leads l WHERE l.tenant_id = :t GROUP BY 1 ORDER BY 2 DESC
    """), params)).all()
    return {"enqueued": inserted, "breakdown_after_enqueue": {r: n for r, n in blocked}}


async def set_status(session, campaign: Campaign, status: str, reason: str | None = None) -> Campaign:
    if status not in ("active", "paused", "completed"):
        raise CampaignError("status must be active, paused or completed")
    if status == "active" and campaign.status == "budget_exhausted":
        raise CampaignError("campaign budget is exhausted; create a new campaign or raise the budget")
    campaign.status = status
    if status == "completed":
        # Release unworked leads so another campaign can take them.
        await session.execute(text("""
            WITH released AS (
                UPDATE dial_queue SET status = 'cancelled', updated_at = now()
                WHERE campaign_id = :c AND status = 'pending' RETURNING lead_id
            )
            UPDATE leads SET status = 'qualified', updated_at = now()
            WHERE id IN (SELECT lead_id FROM released) AND status = 'queued'
        """), {"c": campaign.id})
    if status == "active" and campaign.activated_at is None:
        campaign.activated_at = datetime.now(timezone.utc)
    if reason:
        campaign.halted_reason = reason
    await session.flush()
    return campaign


async def raise_budget(session, campaign: Campaign, budget_usd: float) -> Campaign:
    if budget_usd <= float(campaign.budget_usd):
        raise CampaignError("new budget must exceed the current budget")
    campaign.budget_usd = budget_usd
    if campaign.status == "budget_exhausted":
        campaign.status = "paused"
    await session.flush()
    return campaign


async def campaign_stats(session, campaign: Campaign) -> dict:
    from domain.spend import spent

    q = (await session.execute(text("""
        SELECT status, count(*) FROM dial_queue WHERE campaign_id = :c GROUP BY status
    """), {"c": campaign.id})).all()
    total_spent = await spent(session, tenant_id=campaign.tenant_id, campaign_id=campaign.id)
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "status": campaign.status,
        "channel_strategy": campaign.channel_strategy,
        "messages_per_minute": campaign.messages_per_minute,
        "daily_message_cap": campaign.daily_message_cap,
        "escalate_to_voice": campaign.escalate_to_voice,
        "budget_usd": float(campaign.budget_usd),
        "daily_budget_usd": float(campaign.daily_budget_usd) if campaign.daily_budget_usd is not None else None,
        "spent_usd": round(total_spent, 2),
        "remaining_usd": round(float(campaign.budget_usd) - total_spent, 2),
        "max_concurrent_calls": campaign.max_concurrent_calls,
        "calls_per_minute": campaign.calls_per_minute,
        "states": campaign.states,
        "product_lines": campaign.product_lines,
        "queue": {st: n for st, n in q},
        "halted_reason": campaign.halted_reason,
    }
