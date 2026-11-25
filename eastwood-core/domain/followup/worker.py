"""
Follow-up task runner.

Called on a timer (every 30s) from the app lifespan. Drains FollowupTask rows
where fire_at <= now and status = pending.

Every task is prospect-facing, so each one passes the same gates as a dial,
checked at send time rather than when the task was scheduled:

  1. Campaign gate (domain.outbound) — disarmed, never authorized, or halted:
     nothing is touched; tasks stay pending until the campaign is authorized.
  2. Per-lead eligibility (domain.dialing.check_dial_eligibility) — National
     DNC clearance, entity suppression / opt-out, terminal lead status. A task
     for an ineligible lead is closed as ``blocked`` and never sent.
  3. TCPA calling window — outside 8am-9pm in the lead's state, the task is
     rescheduled to the next window instead of sent.

Channels:
  sms  — rendered template sent through Twilio. With no SMS client configured
         the task is closed as ``skipped`` (it was never sent; it is not
         marked ``sent``).
  call — queued into dial_queue, so a callback goes through the dialer's own
         concurrency cap, retry ladder and final eligibility re-check, and
         actually places the call. (Previously it only opened an ai-agent
         session, which never dialed.)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)


async def run_due_tasks(sms_client, ai_agent_client=None) -> int:
    """
    Drain all due follow-up tasks. Returns the number sent or queued.
    Designed to be called from a background asyncio task.

    ``ai_agent_client`` is accepted for backward compatibility and unused:
    callbacks are placed through the dial queue.
    """
    from config.settings import get_settings
    from db.session import session_scope
    from domain.dialing import check_dial_eligibility
    from domain.outbound import outbound_permitted, record_contact_decision
    from domain.tcpa import in_tcpa_window, next_tcpa_window_open
    from domain.sms import policy as sms_policy
    from repos.repos import DialQueueRepo, FollowupRepo, LeadRepo
    from .sequences import PRICE_TEMPLATES, render_template, unfilled_placeholders

    settings = get_settings()
    now = datetime.now(timezone.utc)
    processed = 0

    async with session_scope() as session:
        permitted, why = await outbound_permitted(session)
        if not permitted:
            # Leave every task pending; nothing prospect-facing goes out.
            return 0
        tasks = await FollowupRepo(session).get_due(now, limit=100, tenant_id=settings.default_tenant_id)

    for task in tasks:
        ctx = task.context_data or {}
        channel = ctx.get("_channel", "sms")
        template_key = ctx.get("_template", "missed_call_immediate")
        metric_channel = "callback" if channel == "call" else "sms"

        async with session_scope() as session:
            followup_repo = FollowupRepo(session)
            lead_repo = LeadRepo(session)

            try:
                lead = await lead_repo.get_by_id(task.lead_id)
                if not lead:
                    await followup_repo.mark_failed(task.id)
                    continue

                # Texts need SMS authorization; callbacks are AI-voice calls.
                eligible, reason = await check_dial_eligibility(
                    session, lead, channel="ai_voice" if channel == "call" else "sms"
                )
                if not eligible:
                    await followup_repo.mark_status(task.id, "blocked")
                    record_contact_decision(metric_channel, "blocked")
                    logger.warning(
                        "followup blocked lead=%s template=%s reason=%s",
                        task.lead_id, template_key, reason,
                    )
                    continue

                if not in_tcpa_window(lead.state, now):
                    next_open = next_tcpa_window_open(lead.state, now)
                    await followup_repo.reschedule(task.id, next_open)
                    record_contact_decision(metric_channel, "deferred")
                    logger.info(
                        "followup deferred to TCPA window lead=%s template=%s at=%s",
                        task.lead_id, template_key, next_open.isoformat(),
                    )
                    continue

                if channel == "call":
                    dq_repo = DialQueueRepo(session)
                    if not await dq_repo.has_open_entry(lead.id):
                        await dq_repo.enqueue(
                            lead_id=lead.id,
                            tenant_id=task.tenant_id,
                            priority=float(lead.triage_priority or 0.0),
                            scheduled_at=now,
                            max_attempts=settings.dial_max_attempts,
                            # stays inside the campaign (and budget) that reached them
                            campaign_id=await dq_repo.last_campaign_for_lead(lead.id),
                        )
                    await followup_repo.mark_sent(task.id)
                    record_contact_decision("callback", "queued")
                    logger.info("followup callback queued for dialing lead=%s reason=%s", task.lead_id, template_key)
                    processed += 1
                    continue

                if not sms_client:
                    await followup_repo.mark_status(task.id, "skipped")
                    record_contact_decision("sms", "skipped_no_provider")
                    logger.info(
                        "SMS client not configured — followup skipped lead=%s template=%s",
                        task.lead_id, template_key,
                    )
                    continue

                full_ctx = {
                    "first_name": lead.first_name or "there",
                    "agency":     settings.agency_display_name,
                    "agent_name": settings.agent_display_name,
                    **{k: v for k, v in ctx.items() if v is not None},
                }
                allowed: set[Decimal] = set()
                if template_key in PRICE_TEMPLATES:
                    quote = await _verified_quote(session, lead.id, ctx.get("quote_id"))
                    if quote is None:
                        template_key = f"{template_key}_noprice"
                    else:
                        face = int(quote.face_value) // 100          # quote_records.face_value is in cents
                        full_ctx.update(quote_amount=f"{Decimal(str(quote.monthly_premium)):.2f}",
                                        face_value=f"{face:,}",
                                        carrier=quote.carrier or full_ctx.get("carrier"),
                                        quote_label=f"preliminary quote {quote.quote_ref or ''}".strip())
                        allowed = {Decimal(str(quote.monthly_premium)), Decimal(face)}
                body = render_template(template_key, full_ctx)
                problems = sms_policy.validate(body, allowed_amounts=allowed, first_message=False,
                                               agency=settings.agency_display_name)
                problems += [f"unfilled:{p}" for p in unfilled_placeholders(body)]
                if problems:
                    await followup_repo.mark_status(task.id, "blocked")
                    record_contact_decision("sms", "blocked")
                    logger.warning("followup SMS blocked by text policy lead=%s template=%s problems=%s",
                                   task.lead_id, template_key, problems)
                    continue
                from domain import spend as _spend

                campaign_id = await DialQueueRepo(session).last_campaign_for_lead(lead.id)
                sms_cost = _spend.sms_segments(body) * settings.cost_sms_per_segment_usd
                if not await _spend.sms_within_budget(session, tenant_id=task.tenant_id,
                                                      campaign_id=campaign_id, cost=sms_cost, now=now):
                    # Over the daily ceiling or the campaign budget: try again
                    # tomorrow (a raised budget lets it through).
                    await followup_repo.reschedule(
                        task.id, _spend.next_day_start(now, settings.calendly_default_timezone))
                    record_contact_decision("sms", "budget_deferred")
                    continue
                await asyncio.to_thread(sms_client.send, lead.phone_e164, body)
                await followup_repo.mark_sent(task.id)
                await _spend.record_sms(session, tenant_id=task.tenant_id, campaign_id=campaign_id,
                                        body=body, reference=f"followup:{task.id}")
                record_contact_decision("sms", "sent")
                logger.info("followup SMS sent lead=%s template=%s", task.lead_id, template_key)
                processed += 1

            except Exception as exc:
                logger.exception("followup task %s failed: %s", task.id, exc)
                await followup_repo.mark_failed(task.id)

    return processed


async def _verified_quote(session, lead_id, quote_id):
    """The stored quote behind a follow-up's figures, only when it came from a
    VERIFIED carrier rate sheet (source "rate_sheet"). Estimates and test
    sheets get the no-price version of the text instead."""
    if not quote_id:
        return None
    from sqlalchemy import select

    from db.models import QuoteRecord

    try:
        qid = uuid.UUID(str(quote_id))
    except ValueError:
        return None
    q = (await session.execute(
        select(QuoteRecord).where(QuoteRecord.id == qid, QuoteRecord.lead_id == lead_id)
    )).scalar_one_or_none()
    if q is None or q.source != "rate_sheet" or q.monthly_premium is None or q.face_value is None:
        return None
    return q
