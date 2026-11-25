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


async def apply_opt_out(session, tenant_id: str, phone_e164: str, *, note: str, body: str | None = None,
                        provider_sid: str | None = None, segments: int = 1) -> dict:
    """Suppress the number and stop everything already scheduled for it.

    With ``body``, the opt-out text itself is also written into the number's SMS
    conversation(s), so the transcript shows exactly when and how the person
    opted out (idempotent on the provider's message id), and its inbound cost
    is booked like any other inbound text.

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

    from db.models import SmsConversation
    if body is not None:
        await _record_opt_out_text(session, tenant_id, phone_e164, body, provider_sid, segments)
    await session.execute(
        update(SmsConversation)
        .where(SmsConversation.tenant_id == tenant_id, SmsConversation.phone_e164 == phone_e164)
        .values(status="opted_out", next_action=None, next_action_at=None)
    )

    return {
        "suppressed": True,
        "newly_suppressed": newly_suppressed,
        "leads": len(lead_ids),
        "cancelled_dials": cancelled_dials,
        "cancelled_followups": cancelled_followups,
    }


async def _record_opt_out_text(session, tenant_id: str, phone_e164: str, body: str, provider_sid: str | None,
                               segments: int) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from config.settings import get_settings
    from db.models import SmsConversation, SmsMessage
    from domain import spend
    from domain.sms.segments import encoding

    s = get_settings()
    convs = list((await session.execute(select(SmsConversation).where(
        SmsConversation.tenant_id == tenant_id, SmsConversation.phone_e164 == phone_e164))).scalars())
    for i, conv in enumerate(convs):
        cost = segments * s.cost_sms_inbound_per_segment_usd if i == 0 else 0.0   # one text, billed once
        msg = SmsMessage(conversation_id=conv.id, direction="in", body=body[:1600], segments=segments,
                         encoding=encoding(body), status="received", provider_sid=(provider_sid or None) if i == 0 else None,
                         intent="opt_out", cost_usd=cost)
        try:
            async with session.begin_nested():
                session.add(msg)
                await session.flush()
        except IntegrityError:
            return                      # this opt-out text was already recorded (webhook retry)
        conv.inbound_count = (conv.inbound_count or 0) + 1
        if cost:
            await spend.add_lines(session, tenant_id=tenant_id, campaign_id=conv.campaign_id,
                                  reference=f"sms-in:{msg.id}", lines=[("sms_in", float(segments),
                                                                         s.cost_sms_inbound_per_segment_usd)])
