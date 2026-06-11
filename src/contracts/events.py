from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

class LeadEvent(BaseModel):
    event_id: str
    ts: str
    source: str
    payload: Dict[str, Any]

class ConsentEvent(BaseModel):
    event_id: str
    ts: str
    lead_id: str
    evidence: Dict[str, Any]

class TouchEvent(BaseModel):
    event_id: str
    ts: str
    lead_id: str
    channel: str
    result: str
    meta: Dict[str, Any] = Field(default_factory=dict)

class SuppressionEvent(BaseModel):
    event_id: str
    ts: str
    kind: str
    value: str
    meta: Dict[str, Any] = Field(default_factory=dict)

class DlqEvent(BaseModel):
    event_id: str
    ts: str
    topic: str
    reason: str
    payload: Dict[str, Any]
    error: Optional[str] = None