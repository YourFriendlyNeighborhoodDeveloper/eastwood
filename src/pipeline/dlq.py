from typing import Any, Dict, Optional

from ..core.clock import now_iso
from ..core.ids import new_id
from ..contracts.events import DlqEvent


def to_dlq(topic: str, reason: str, payload: Dict[str, Any], error: Optional[str] = None) -> Dict[str, Any]:
    ev = DlqEvent(
        event_id=new_id(),
        ts=now_iso(),
        topic=topic,
        reason=reason,
        payload=payload,
        error=error,
    )
    return ev.model_dump()