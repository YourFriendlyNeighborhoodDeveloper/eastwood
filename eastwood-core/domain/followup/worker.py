"""
Follow-up task runner.

Called on a timer (every 30s) from the app lifespan.
Drains FollowupTask rows where fire_at <= now and status = pending,
fires SMS or schedules a callback, then marks sent/failed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def run_due_tasks(sms_client, ai_agent_client=None) -> int:
    """
    Drain all due follow-up tasks. Returns number processed.
    Designed to be called from a background asyncio task.
    """
    from db.session import session_scope
    from repos.repos import FollowupRepo, LeadRepo
    from .sequences import render_template

    now = datetime.now(timezone.utc)
    processed = 0

    async with session_scope() as session:
        followup_repo = FollowupRepo(session)
        lead_repo = LeadRepo(session)
        tasks = await followup_repo.get_due(now, limit=100)

    for task in tasks:
        ctx = task.context_data or {}
        channel = ctx.get("_channel", "sms")
        template_key = ctx.get("_template", "missed_call_immediate")

        async with session_scope() as session:
            followup_repo = FollowupRepo(session)
            lead_repo = LeadRepo(session)

            try:
                lead = await lead_repo.get_by_id(task.lead_id)
                if not lead:
                    await followup_repo.mark_failed(task.id)
                    continue

                # Enrich context with live lead data
                full_ctx = {
                    "first_name":    lead.first_name or "there",
                    "agent_name":    "Ryan",
                    "office_phone":  "1-800-EASTWOOD",
                    **ctx,
                }

                if channel == "sms" and sms_client:
                    body = render_template(template_key, full_ctx)
                    await asyncio.to_thread(sms_client.send, lead.phone_e164, body)
                    logger.info("followup SMS sent lead=%s template=%s", task.lead_id, template_key)

                elif channel == "call" and ai_agent_client:
                    # Re-initiate an AI call with context about why we're calling back
                    call_context = {
                        "phone_e164":     lead.phone_e164,
                        "first_name":     lead.first_name,
                        "last_name":      lead.last_name,
                        "product_line":   lead.product_line,
                        "state":          lead.state,
                        "age":            lead.age,
                        "tenant_id":      task.tenant_id,
                        "followup_reason": template_key,
                        "followup_context": ctx,
                    }
                    await ai_agent_client.start_session(str(task.lead_id), call_context)
                    logger.info("followup call initiated lead=%s reason=%s", task.lead_id, template_key)

                await followup_repo.mark_sent(task.id)
                processed += 1

            except Exception as exc:
                logger.exception("followup task %s failed: %s", task.id, exc)
                await followup_repo.mark_failed(task.id)

    return processed
