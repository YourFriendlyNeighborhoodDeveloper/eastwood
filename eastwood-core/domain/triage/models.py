from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Sex(str, Enum):
    M = "M"
    F = "F"
    UNKNOWN = "UNKNOWN"


class TobaccoStatus(str, Enum):
    NONE = "none"
    CURRENT = "current"
    FORMER = "former"
    UNKNOWN = "unknown"


class Condition(BaseModel):
    code: str
    raw_text: Optional[str] = None
    diagnosed_on: Optional[date] = None
    last_event_on: Optional[date] = None   # last hospitalization / treatment / episode
    controlled: Optional[bool] = None
    note: Optional[str] = None


class Medication(BaseModel):
    code: str
    raw_text: Optional[str] = None


class CurrentPolicy(BaseModel):
    carrier: Optional[str] = None
    product: Optional[str] = None
    face_amount: Optional[int] = None
    monthly_premium: Optional[float] = None
    issue_date: Optional[date] = None  # for contestability guard


class Compliance(BaseModel):
    on_dnc: Optional[bool] = None
    has_express_written_consent: bool = False
    consent_timestamp: Optional[date] = None
    known_litigator: bool = False
    phone_valid: Optional[bool] = None
    phone_type: Optional[str] = None       # mobile | landline | voip
    is_the_insured: Optional[bool] = None


class TriageInput(BaseModel):
    lead_id: str
    source: str
    generated_on: Optional[date] = None
    contact_attempts: int = 0
    state: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[date] = None
    sex: Sex = Sex.UNKNOWN
    height_in: Optional[int] = None
    weight_lb: Optional[int] = None
    tobacco: TobaccoStatus = TobaccoStatus.UNKNOWN
    conditions: list[Condition] = []
    medications: list[Medication] = []
    current_policy: Optional[CurrentPolicy] = None
    compliance: Compliance = Compliance()
    enrichment: dict = {}


class TriageResult(BaseModel):
    lead_id: str
    bucket: str                  # CALL_NOW | WORKABLE | DEAD
    severity_score: float        # 0..1, lower = cleaner
    priority: float              # sort key within bucket
    confidence: float            # 0..1 data completeness
    needs_enrichment: bool
    drivers: list[str]           # human-readable "why" — required for trust
    dead_reason: Optional[str] = None
    ruleset_version: str
    scored_at: date
