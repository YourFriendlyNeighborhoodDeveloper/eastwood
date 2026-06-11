"""
Follow-up scheduler.

Call schedule_for_outcome() right after a call ends or an agent event fires.
It creates FollowupTask rows in the DB — the worker drains them on a timer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from repos.repos import FollowupRepo
from .sequences import SEQUENCES

logger = logging.getLogger(__name__)


async def schedule_for_outcome(
    lead_id,
    tenant_id: str,
    outcome: str,
    context_data: dict,
    followup_repo: FollowupRepo,
) -> int:
    """
    Map a call outcome to a follow-up sequence and create the tasks.
    Returns number of tasks scheduled.

    outcome values (match CallRecord.outcome + agent event outcomes):
        no_answer | voicemail | interested_busy | quoted_not_closed |
        price_objection | spouse_objection | application_not_completed |
        payment_failed | sold | dnc | not_interested
    """
    sequence_map: dict[str, str] = {
        "no_answer":                 "missed_call",
        "voicemail":                 "voicemail_left",
        "interested_busy":           "interested_busy",
        "quoted_not_closed":         "quoted_not_closed",
        "price_objection":           "quoted_price_objection",
        "spouse_objection":          "quoted_spouse_objection",
        "application_not_completed": "application_not_completed",
        "payment_failed":            "payment_failed",
        "callback":                  "no_contact",
    }

    # Terminal outcomes get no follow-up
    terminal = {"sold", "dnc", "not_interested", "wrong_number"}
    if outcome in terminal:
        return 0

    sequence_key = sequence_map.get(outcome)
    if not sequence_key or sequence_key not in SEQUENCES:
        logger.debug("no sequence for outcome=%s", outcome)
        return 0

    steps = SEQUENCES[sequence_key]
    now = datetime.now(timezone.utc)
    count = 0

    for step in steps:
        fire_at = now + step.delay
        await followup_repo.create(
            lead_id=lead_id,
            tenant_id=tenant_id,
            sequence_type=f"{sequence_key}:{step.template_key}:{step.channel}",
            fire_at=fire_at,
            context_data={**context_data, "_channel": step.channel, "_template": step.template_key},
        )
        count += 1

    logger.info("scheduled %d follow-up tasks for lead %s outcome=%s", count, lead_id, outcome)
    return count
