"""
Terminal Twilio status handling: the retry ladder and immediate session cleanup.

A call that never reaches a prospect produces no call_ended event (the ai-agent
only publishes that once the media stream binds). So Twilio's status callback is
the authoritative terminal signal for unanswered calls, and it is what schedules
retries. Connected calls keep their outcome-based follow-up via call_ended.

Everything here is idempotent against duplicate callbacks using persisted state
— the dial_queue row's status — never process-local memory. The transition out
of "dialing" is a conditional UPDATE guarded on status='dialing', so exactly one
callback per attempt schedules a retry, frees the slot, and moves next_attempt_at.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Hours before the next retry, indexed by the attempt just completed.
RETRY_OFFSETS_HOURS = [4, 24, 32, 56, 80]

# Twilio CallStatus → dial_queue terminal status.
TWILIO_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "no-answer": "no_answer",
    "busy":      "busy",
    "failed":    "failed",
    "canceled":  "cancelled",
}

# Statuses that mean the call never reached a person and should be retried.
RETRYABLE_STATUSES = frozenset({"no_answer", "busy", "failed"})

# Lead statuses that are terminal for dialing — never retry or dial these.
NON_DIALABLE_LEAD_STATUSES = frozenset({
    "suppressed", "compliance_blocked", "dead", "converted",
    "do_not_call", "appointment_set", "booked",
})


async def check_dial_eligibility(session, lead) -> tuple[bool, str]:
    """Every hard gate a lead must pass to be dialed. Shared by retry scheduling
    (Twilio callback) and retry execution (process_dial_queue), so both agree.

    Returns (eligible, reason). TCPA windowing is handled separately by the
    dialer — it reschedules rather than refuses.
    """
    from domain.compliance import UNKNOWN, is_dialable
    from repos.repos import SuppressionRepo

    national = getattr(lead, "national_dnc_status", None) or UNKNOWN
    if not is_dialable(national):
        return False, f"national_dnc:{national}"

    lead_status = getattr(lead, "status", "") or ""
    if lead_status in NON_DIALABLE_LEAD_STATUSES:
        return False, f"lead_status:{lead_status}"

    tenant_id = getattr(lead, "tenant_id", None) or "default"
    if await SuppressionRepo(session).is_suppressed(tenant_id, lead.phone_e164):
        return False, "suppressed"

    return True, "eligible"


async def handle_terminal_status(
    session,
    session_id: str,
    call_status: str,
    *,
    ai_agent,
    now: datetime | None = None,
) -> dict:
    """Process one Twilio terminal status callback.

    Frees the concurrency slot immediately, schedules a retry for a retryable
    status when the lead is still eligible and attempts remain, and closes the
    ai-agent session — in that order, so a slow or failing end_session can never
    block the slot release or the retry persistence.
    """
    from repos.repos import DialQueueRepo, LeadRepo

    now = now or datetime.now(timezone.utc)
    normalized = TWILIO_STATUS_MAP.get(call_status)
    if normalized is None:
        return {"received": True, "handled": False}

    dq = DialQueueRepo(session)
    entry = await dq.get_by_session(session_id)

    result: dict = {"received": True, "status": normalized, "retry_scheduled": False}

    if entry is not None:
        if normalized in RETRYABLE_STATUSES:
            result.update(await _schedule_retry(session, dq, entry, normalized, now))
        else:
            # completed / cancelled: free the slot, never retry from here.
            await dq.mark_terminal_if_dialing(entry.id, normalized)

        # Committed before end_session so the slot is freed and any retry is
        # persisted regardless of what happens next.
        await session.commit()

    # Close the ai-agent session immediately — do not wait for the 600s reaper.
    # Idempotent on the agent side, and its failure must not undo the work above.
    result["end_session_called"] = True
    try:
        await ai_agent.end_session(session_id)
    except Exception:
        result["end_session_ok"] = False
        logger.warning("end_session failed for %s (slot already freed)", session_id, exc_info=True)
    else:
        result["end_session_ok"] = True

    return result


async def _schedule_retry(session, dq, entry, normalized: str, now: datetime) -> dict:
    from repos.repos import LeadRepo

    lead = await LeadRepo(session).get_by_id(entry.lead_id)
    if lead is None:
        await dq.mark_terminal_if_dialing(entry.id, normalized)
        return {"retry_scheduled": False, "reason": "lead_missing"}

    eligible, reason = await check_dial_eligibility(session, lead)
    if not eligible:
        # Do not retry a suppressed / blocked / terminal lead, and free the slot.
        await dq.mark_terminal_if_dialing(entry.id, "no_retry")
        logger.info("no retry for lead %s (%s)", lead.id, reason)
        return {"retry_scheduled": False, "reason": reason}

    if entry.attempt_count >= entry.max_attempts:
        await dq.mark_terminal_if_dialing(entry.id, normalized)
        logger.info("lead %s exhausted %d attempts", lead.id, entry.max_attempts)
        return {"retry_scheduled": False, "reason": "max_attempts"}

    offset_hours = RETRY_OFFSETS_HOURS[min(entry.attempt_count, len(RETRY_OFFSETS_HOURS) - 1)]
    retry_at = now + timedelta(hours=offset_hours)
    next_attempt = entry.attempt_count + 1

    # The idempotency gate: only the callback that finds the row still 'dialing'
    # schedules the retry. Duplicates find it 'pending' and no-op.
    claimed = await dq.reschedule_if_dialing(entry.id, retry_at, next_attempt)
    if not claimed:
        return {"retry_scheduled": False, "reason": "duplicate"}

    logger.info(
        "lead %s rescheduled (attempt %d/%d) at %s (%s, +%dh)",
        lead.id, next_attempt, entry.max_attempts, retry_at.isoformat(), normalized, offset_hours,
    )
    return {
        "retry_scheduled": True,
        "attempt": next_attempt,
        "offset_hours": offset_hours,
        "retry_at": retry_at.isoformat(),
    }
