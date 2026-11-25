"""Reconcile calls whose terminal status callback never arrived.

Twilio delivers a status callback once. If eastwood-core is down or restarting
at that moment (a deploy, a crash), the dial_queue row stays "dialing" and holds
a concurrency slot; with MAX_CONCURRENT_CALLS=1 that stops the campaign until
the 10-minute reaper runs. This asks the provider directly for any call that
has been "dialing" longer than ``older_than_seconds`` and, when the provider
says it ended, runs the same idempotent terminal path the callback would have:
slot release, retry ladder, session close, attempt + spend booking.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import DialQueueEntry
from db.session import session_scope

from .retry import TWILIO_STATUS_MAP, handle_terminal_status

logger = logging.getLogger(__name__)


async def reconcile_stuck_calls(brain, *, older_than_seconds: int = 90, limit: int = 50,
                                now: datetime | None = None) -> dict:
    client = getattr(brain, "_sms", None)
    if client is None or not hasattr(client, "fetch_call"):
        return {"checked": 0, "reconciled": 0}
    now = now or datetime.now(timezone.utc)
    async with session_scope() as s:
        rows = (await s.execute(
            select(DialQueueEntry.session_id, DialQueueEntry.provider_call_sid)
            .where(DialQueueEntry.status == "dialing",
                   DialQueueEntry.updated_at < now - timedelta(seconds=older_than_seconds),
                   DialQueueEntry.provider_call_sid.is_not(None))
            .limit(limit)
        )).all()

    from domain.attempts import close_attempt

    reconciled = 0
    for session_id, sid in rows:
        try:
            state = await asyncio.to_thread(client.fetch_call, sid)
        except Exception:
            logger.warning("could not fetch provider state for call %s", sid, exc_info=True)
            continue
        status = state.get("status")
        normalized = TWILIO_STATUS_MAP.get(status)
        if normalized is None:
            continue  # still queued / ringing / in progress
        async with session_scope() as s:
            await handle_terminal_status(s, session_id, status, ai_agent=brain._ai_agent)
        async with session_scope() as s:
            await close_attempt(s, session_id=session_id, status=normalized,
                                duration_seconds=state.get("duration"), answered_by=state.get("answered_by"))
        reconciled += 1
        logger.info("reconciled call %s (session %s) as %s from provider state", sid, session_id, status)
    return {"checked": len(rows), "reconciled": reconciled}
