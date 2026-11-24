"""Inbound SMS opt-out: a texted STOP revokes consent for calls AND texts.

Twilio's own opt-out handling only stops further *messages* from the sending
number; it never tells Eastwood. Without this, a prospect who replied STOP to a
follow-up could still be auto-dialed. FCC rules require honoring a revocation
made by any reasonable means, explicitly including replies such as "stop",
"quit", "end", "revoke", "opt out", "cancel" or "unsubscribe".

Matching deliberately errs toward suppression: a false positive costs one
lead; a false negative is a per-call violation. Re-subscribing (START/UNSTOP)
is never automatic here — an operator must review and remove the suppression.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Any message that is exactly one of these (case/punctuation-insensitive).
_EXACT_KEYWORDS = frozenset({
    "stop", "stopall", "stop all", "unsubscribe", "cancel", "end", "quit",
    "revoke", "optout", "opt out", "opt-out", "remove", "remove me",
})

# Any message containing one of these phrases, wherever it appears.
_PHRASES = (
    "stop calling", "stop texting", "stop contacting", "stop messaging",
    "do not call", "dont call", "don't call", "do not text", "dont text",
    "don't text", "do not contact", "remove me", "take me off", "opt out",
    "opt-out", "optout", "unsubscribe", "revoke",
)


def _normalize(body: str) -> str:
    text = (body or "").lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_opt_out(body: str) -> bool:
    text = _normalize(body)
    if not text:
        return False
    if text in _EXACT_KEYWORDS or text.split(" ", 1)[0] in {"stop", "stopall", "unsubscribe"}:
        return True
    return any(p in text for p in _PHRASES)


async def apply_opt_out(session, tenant_id: str, phone_e164: str, *, note: str) -> dict:
    """Suppress the number and stop everything already scheduled for it.

    Returns counts only — never the number — so callers can log the result.
    """
    from sqlalchemy import select, update

    from db.models import DialQueueEntry, FollowupTask, Lead
    from repos.repos import SuppressionRepo

    suppressions = SuppressionRepo(session)
    newly_suppressed = False
    if not await suppressions.is_suppressed(tenant_id, phone_e164):
        await suppressions.add(tenant_id, phone_e164, reason="sms_opt_out", notes=note)
        newly_suppressed = True

    lead_ids = list(
        (
            await session.execute(
                select(Lead.id).where(Lead.tenant_id == tenant_id, Lead.phone_e164 == phone_e164)
            )
        ).scalars()
    )
    cancelled_dials = cancelled_followups = 0
    if lead_ids:
        await session.execute(update(Lead).where(Lead.id.in_(lead_ids)).values(status="suppressed"))
        r = await session.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.lead_id.in_(lead_ids), DialQueueEntry.status == "pending")
            .values(status="cancelled")
        )
        cancelled_dials = r.rowcount or 0
        r = await session.execute(
            update(FollowupTask)
            .where(FollowupTask.lead_id.in_(lead_ids), FollowupTask.status == "pending")
            .values(status="cancelled")
        )
        cancelled_followups = r.rowcount or 0

    return {
        "suppressed": True,
        "newly_suppressed": newly_suppressed,
        "leads": len(lead_ids),
        "cancelled_dials": cancelled_dials,
        "cancelled_followups": cancelled_followups,
    }
