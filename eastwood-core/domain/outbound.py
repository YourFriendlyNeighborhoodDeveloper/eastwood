"""Outbound campaign control: the arm switch, campaign authorization, and the
emergency halt.

Prospect-facing contact (dials, follow-up SMS and callbacks, consent requests,
booking confirmations) is permitted only when BOTH keys are turned:

  1. Deployment key — OUTBOUND_ENABLED=true in the environment. Off by default,
     so a stack booted from any config that does not say otherwise contacts
     nobody.
  2. Runtime key — the latest outbound_control_events row for the tenant is a
     ``resume``. A deployment with no rows has never been authorized and is
     treated as halted. Resuming records who authorized the campaign and why;
     halting (the emergency stop) records the same and takes effect on the
     dialer's next pass (≤3s) and the follow-up worker's next pass (≤30s).

Halting does not hang up calls already in progress — those finish normally.

Internal notifications to the licensed agent (handoff briefs, outcome SMS,
live-transfer alerts) are not prospect contact and are not gated here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from config.settings import get_settings
from db.models import OutboundControlEvent

logger = logging.getLogger(__name__)

HALT = "halt"
RESUME = "resume"

# Reasons outbound_permitted() may return.
PERMITTED = "permitted"
DISARMED = "outbound_disabled"          # OUTBOUND_ENABLED is false
NEVER_AUTHORIZED = "never_authorized"   # no control event recorded yet
HALTED = "halted"                       # latest event is a halt


class OutboundNotArmedError(RuntimeError):
    """Resume refused: the deployment has OUTBOUND_ENABLED=false."""


@dataclass(frozen=True)
class OutboundState:
    permitted: bool
    reason: str
    armed: bool
    last_action: str | None
    operator: str | None
    note: str | None
    changed_at: datetime | None

    def as_dict(self) -> dict:
        return {
            "permitted": self.permitted,
            "reason": self.reason,
            "outbound_enabled": self.armed,
            "last_action": self.last_action,
            "operator": self.operator,
            "note": self.note,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }


async def _latest_event(session, tenant_id: str) -> OutboundControlEvent | None:
    result = await session.execute(
        select(OutboundControlEvent)
        .where(OutboundControlEvent.tenant_id == tenant_id)
        .order_by(OutboundControlEvent.created_at.desc(), OutboundControlEvent.id.desc())
        .limit(1)
    )
    return result.scalars().first()


def _state(armed: bool, event: OutboundControlEvent | None) -> OutboundState:
    if not armed:
        reason = DISARMED
    elif event is None:
        reason = NEVER_AUTHORIZED
    elif event.action != RESUME:
        reason = HALTED
    else:
        reason = PERMITTED
    return OutboundState(
        permitted=reason == PERMITTED,
        reason=reason,
        armed=armed,
        last_action=event.action if event else None,
        operator=event.operator if event else None,
        note=event.reason if event else None,
        changed_at=event.created_at if event else None,
    )


async def get_outbound_state(session, tenant_id: str | None = None) -> OutboundState:
    settings = get_settings()
    tenant_id = tenant_id or settings.default_tenant_id
    event = await _latest_event(session, tenant_id)
    state = _state(settings.outbound_enabled, event)
    _publish_gauge(state)
    return state


async def outbound_permitted(session, tenant_id: str | None = None) -> tuple[bool, str]:
    """The single campaign-level gate every prospect-facing send checks first."""
    state = await get_outbound_state(session, tenant_id)
    return state.permitted, state.reason


async def _record(session, tenant_id: str, action: str, operator: str, reason: str) -> None:
    session.add(
        OutboundControlEvent(
            tenant_id=tenant_id, action=action, operator=operator, reason=reason
        )
    )
    await session.flush()


async def halt_outbound(session, *, operator: str, reason: str, tenant_id: str | None = None) -> OutboundState:
    """Emergency stop. Always allowed, whatever the current state."""
    settings = get_settings()
    tenant_id = tenant_id or settings.default_tenant_id
    await _record(session, tenant_id, HALT, operator, reason)
    logger.warning("OUTBOUND HALTED tenant=%s operator=%s reason=%s", tenant_id, operator, reason)
    return await get_outbound_state(session, tenant_id)


async def resume_outbound(session, *, operator: str, reason: str, tenant_id: str | None = None) -> OutboundState:
    """Authorize the campaign. Refused unless the deployment is armed."""
    settings = get_settings()
    tenant_id = tenant_id or settings.default_tenant_id
    if not settings.outbound_enabled:
        raise OutboundNotArmedError(
            "OUTBOUND_ENABLED is false for this deployment; outbound cannot be "
            "authorized at runtime until it is armed in the environment."
        )
    await _record(session, tenant_id, RESUME, operator, reason)
    logger.warning("OUTBOUND AUTHORIZED tenant=%s operator=%s reason=%s", tenant_id, operator, reason)
    return await get_outbound_state(session, tenant_id)


def _publish_gauge(state: OutboundState) -> None:
    try:
        from metrics.metrics import outbound_permitted_gauge

        outbound_permitted_gauge.set(1 if state.permitted else 0)
    except Exception:  # metrics must never break the gate
        logger.debug("outbound gauge update failed", exc_info=True)


def record_contact_decision(channel: str, result: str) -> None:
    try:
        from metrics.metrics import outbound_contact_decisions_total

        outbound_contact_decisions_total.labels(channel=channel, result=result).inc()
    except Exception:
        logger.debug("outbound decision metric failed", exc_info=True)
