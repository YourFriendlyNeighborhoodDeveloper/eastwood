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
from datetime import datetime, timezone

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
    from repos.repos import DialQueueRepo, FollowupRepo, LeadRepo
    from .sequences import render_template

    settings = get_settings()
    now = datetime.now(timezone.utc)
    processed = 0

    async with session_scope() as session:
        permitted, why = await outbound_permitted(session)
        if not permitted:
            # Leave every task pending; nothing prospect-facing goes out.
            return 0
        tasks = await FollowupRepo(session).get_due(now, limit=100)

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

                eligible, reason = await check_dial_eligibility(session, lead)
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
                    "first_name":    lead.first_name or "there",
                    "agent_name":    "Ryan",
                    "office_phone":  "1-800-EASTWOOD",
                    **ctx,
                }
                body = render_template(template_key, full_ctx)
                await asyncio.to_thread(sms_client.send, lead.phone_e164, body)
                await followup_repo.mark_sent(task.id)
                record_contact_decision("sms", "sent")
                logger.info("followup SMS sent lead=%s template=%s", task.lead_id, template_key)
                processed += 1

            except Exception as exc:
                logger.exception("followup task %s failed: %s", task.id, exc)
                await followup_repo.mark_failed(task.id)

    return processed
