"""Close a dial attempt when Twilio reports its terminal status, and book its spend.

Idempotent: the attempt row is closed with a conditional UPDATE (ended_at IS
NULL), so only the first status callback for a call records spend; duplicates
change nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text, update

from db.models import DialAttempt
from domain.spend import add_lines, call_cost_lines

logger = logging.getLogger(__name__)


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


async def close_attempt(session, *, session_id: str, status: str, duration_seconds=None,
                        answered_by: str | None = None, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    duration = _int(duration_seconds) or 0
    row = (await session.execute(
        update(DialAttempt)
        .where(DialAttempt.session_id == session_id, DialAttempt.ended_at.is_(None))
        .values(status=status, answered_by=(answered_by or None), duration_seconds=duration, ended_at=now)
        .returning(DialAttempt.id, DialAttempt.tenant_id, DialAttempt.campaign_id)
    )).first()
    if row is None:
        return None
    attempt_id, tenant_id, campaign_id = row
    lines = call_cost_lines(duration) if status == "completed" else []
    cost = await add_lines(session, tenant_id=tenant_id, campaign_id=campaign_id, reference=session_id, lines=lines)
    if cost:
        await session.execute(update(DialAttempt).where(DialAttempt.id == attempt_id).values(cost_usd=cost))
    return {"attempt_id": str(attempt_id), "cost_usd": cost, "duration_seconds": duration}


async def reap_open_attempts(session, older_than_minutes: int = 15) -> int:
    """Attempts never closed by a callback (setup failure, lost webhook)."""
    result = await session.execute(text(
        "UPDATE dial_attempts SET status = 'reaped', ended_at = now() "
        "WHERE ended_at IS NULL AND started_at < now() - make_interval(mins => :m)"
    ), {"m": older_than_minutes})
    return result.rowcount or 0
