from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as _sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_e164", "product_line", name="uq_lead_tenant_phone_product"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(String(10))
    age: Mapped[int | None] = mapped_column(Integer)
    product_line: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="raw", nullable=False)
    score: Mapped[int | None] = mapped_column(Integer)
    score_band: Mapped[str | None] = mapped_column(String(16))
    score_components: Mapped[dict | None] = mapped_column(JSONB)
    enrichment: Mapped[dict | None] = mapped_column(JSONB)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    health_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    triage_bucket: Mapped[str | None] = mapped_column(String(32))
    triage_priority: Mapped[float | None] = mapped_column(Float)
    # National DNC Registry clearance. Distinct from the suppressions table,
    # which records entity-specific DNC. "unknown" is the default and is not
    # dialable — see domain.compliance.dnc_scrub.
    national_dnc_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown", index=True
    )
    national_dnc_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    national_dnc_provider: Mapped[str | None] = mapped_column(String(64))
    national_dnc_source: Mapped[str | None] = mapped_column(String(128))
    # Structured scrub provenance. Never holds a raw SAN or registry numbers.
    national_dnc_audit: Mapped[dict | None] = mapped_column(JSONB)
    source_batch_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ap_cents: Mapped[int | None] = mapped_column(Integer)  # annual premium in cents when policy written
    # ── Provenance (bulk import) ─────────────────────────────────────────────
    # Who the lead belongs to, where it came from and when it was originally
    # generated — preserved from the source file, never inferred.
    lead_owner: Mapped[str | None] = mapped_column(String(128))
    external_id: Mapped[str | None] = mapped_column(String(128))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    # Quoting facts known from the source file (prefill; the prospect can correct).
    sex: Mapped[str | None] = mapped_column(String(1))
    tobacco: Mapped[bool | None] = mapped_column(Boolean)
    # Per-channel authorization from domain.imports.classify:
    #   {"ai_voice": bool, "sms": bool, "manual_call": bool, "evaluated_at": iso}
    # The dialer requires ai_voice=true; marketing SMS requires sms=true.
    contact_authorization: Mapped[dict | None] = mapped_column(JSONB)
    # Why the lead is not authorized for AI-voice contact (empty when it is).
    quarantine_reasons: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    consents: Mapped[list[ConsentRecord]] = relationship("ConsentRecord", back_populates="lead")
    call_records: Mapped[list[CallRecord]] = relationship("CallRecord", back_populates="lead")


class ConsentRecord(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    express_written: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_pack: Mapped[dict | None] = mapped_column(JSONB)
    source_url: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead: Mapped[Lead] = relationship("Lead", back_populates="consents")


class Suppression(Base):
    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookingSlot(Base):
    __tablename__ = "booking_slots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    appointments: Mapped[list[Appointment]] = relationship("Appointment", back_populates="slot")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), nullable=False)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("booking_slots.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text)
    # Calendly booking record. calendly_status is "not_requested" for a plain
    # local booking, "confirmed" only when Calendly created the invitee, and
    # "failed" when Calendly was wanted but could not book — the prospect is
    # never told a failed booking is confirmed.
    prospect_email: Mapped[str | None] = mapped_column(String(256))
    timezone: Mapped[str | None] = mapped_column(String(64))
    calendly_event_uri: Mapped[str | None] = mapped_column(Text)
    calendly_invitee_uri: Mapped[str | None] = mapped_column(Text)
    calendly_cancel_url: Mapped[str | None] = mapped_column(Text)
    calendly_reschedule_url: Mapped[str | None] = mapped_column(Text)
    calendly_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested", server_default="not_requested"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # attended | no_show | cancelled | rescheduled | sold
    outcome: Mapped[str | None] = mapped_column(String(16))
    reminders_sent: Mapped[list | None] = mapped_column(JSONB)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    slot: Mapped[BookingSlot] = relationship("BookingSlot", back_populates="appointments")


class CallRecord(Base):
    __tablename__ = "call_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    status: Mapped[str] = mapped_column(String(32), default="initiated")
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(32))
    recording_url: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    health_flags: Mapped[dict | None] = mapped_column(JSONB)
    carrier_placed: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead: Mapped[Lead] = relationship("Lead", back_populates="call_records")


class ObjectionRecord(Base):
    __tablename__ = "objection_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64))
    objection_type: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    rebuttal: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuoteRecord(Base):
    __tablename__ = "quote_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64))
    product_line: Mapped[str] = mapped_column(String(64), nullable=False)
    face_value: Mapped[int | None] = mapped_column(BigInteger)
    monthly_premium: Mapped[float | None] = mapped_column(Numeric(10, 2))
    carrier: Mapped[str | None] = mapped_column(String(128))
    quote_data: Mapped[dict | None] = mapped_column(JSONB)
    presented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Prospect-facing quotes (domain.quoting.prospect): a short human reference,
    # the rate source, the exact rate sheet, the conversation it was quoted in,
    # and when it was actually communicated to the prospect.
    quote_ref: Mapped[str | None] = mapped_column(String(16), index=True)
    source: Mapped[str | None] = mapped_column(String(32))
    rate_sheet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    communicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeadBatch(Base):
    """One row per purchased lead list. Ties leads to cost for ROI math."""
    __tablename__ = "lead_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupTask(Base):
    """Scheduled follow-up actions: SMS, callbacks, sequences."""
    __tablename__ = "followup_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TriageResultModel(Base):
    __tablename__ = "triage_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    needs_enrichment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drivers: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    dead_reason: Mapped[str | None] = mapped_column(String(64))
    ruleset_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scored_at: Mapped[datetime] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RulesetModel(Base):
    __tablename__ = "rulesets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    version: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    effective_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CarrierProfileModel(Base):
    """Versioned carrier underwriting profiles — source of truth for placement engine."""
    __tablename__ = "carrier_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    carrier_name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_line: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    states: Mapped[list | None] = mapped_column(JSONB)          # null = all states
    min_age: Mapped[int] = mapped_column(Integer, nullable=False)
    max_age: Mapped[int] = mapped_column(Integer, nullable=False)
    has_gi: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    level_conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    graded_conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    modified_conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    knockout_conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    required_questions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    rate_table_ref: Mapped[str | None] = mapped_column(String(128))
    priority_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DialQueueEntry(Base):
    """One row per outbound call attempt. Controls the autonomous dialer."""
    __tablename__ = "dial_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    # pending | dialing | no_answer | voicemail | busy | completed | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Provider call id (Twilio CallSid) — lets the reconciler ask the provider
    # what happened when a status callback never arrived.
    provider_call_sid: Mapped[str | None] = mapped_column(String(64))
    amd_result: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CarrierRateTable(Base):
    """
    Carrier-published rate tables — ground truth for actual premiums.
    One row = one quote cell: carrier × product × tier × state × age × tobacco × face_value.
    """
    __tablename__ = "carrier_rate_tables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    carrier_name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_line: Mapped[str] = mapped_column(String(64), nullable=False)
    rate_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str | None] = mapped_column(String(2))
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    gender: Mapped[str | None] = mapped_column(String(1))
    tobacco: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    face_value_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monthly_premium_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    expires_date: Mapped[datetime | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # Provenance of the rate sheet, e.g. "Transamerica FE rate sheet 2026-01".
    # Rows without one — and the synthetic sample rows tagged "synthetic_seed"
    # — are never quoted as carrier rates (domain.quoting.adapters.rate_table).
    source: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HandoffProfile(Base):
    """The full prospect brief assembled at booking, so the licensed agent can
    retrieve it later even if the alert SMS was truncated. SMS is the alert; this
    is the canonical store."""
    __tablename__ = "handoff_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64))
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    agent_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LeadFieldEvent(Base):
    """Immutable mutation log for every field captured on a lead — audit trail."""
    __tablename__ = "lead_field_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    capture_key: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    captured_by: Mapped[str] = mapped_column(String(64), nullable=False, default="ai_agent")
    source: Mapped[str | None] = mapped_column(String(64))       # session_id / import / api
    call_id: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboundControlEvent(Base):
    """Append-only log of campaign authorizations and halts (domain.outbound).

    The latest row per tenant is the live state. No row at all means the
    campaign has never been authorized, which is treated as halted — a fresh
    deployment contacts nobody until an operator explicitly resumes it.
    """
    __tablename__ = "outbound_control_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # halt | resume
    operator: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class ImportBatch(Base):
    """One row per imported file. The sha256 makes re-imports idempotent and
    processed_rows is the resume cursor (committed with each chunk)."""
    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint("tenant_id", "file_sha256", name="uq_import_batch_file"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(256), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    lead_owner: Mapped[str | None] = mapped_column(String(128))
    acquisition_date: Mapped[datetime | None] = mapped_column(Date)
    # Reference to the written authorization to use these leads (assignment,
    # purchase agreement). NULL quarantines every lead in the batch.
    use_authorization_ref: Mapped[str | None] = mapped_column(Text)
    column_map: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_in_file: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_existing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authorized_ai_voice: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ImportRowError(Base):
    """Per-row import problems. Row number and a code only — never the row's PII."""
    __tablename__ = "import_errors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)  # error | warning
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    field: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(String(256))


class Campaign(Base):
    """A named, budgeted outbound effort. Leads are dialed only after an
    operator enqueues them into an active campaign."""
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # draft | active | paused | budget_exhausted | completed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    budget_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    daily_budget_usd: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    calls_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    states: Mapped[list | None] = mapped_column(JSONB)
    product_lines: Mapped[list | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    halted_reason: Mapped[str | None] = mapped_column(Text)
    # sms_first (default) | voice_first
    channel_strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="sms_first")
    messages_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    daily_message_cap: Mapped[int | None] = mapped_column(Integer)
    # A prospect who asks for a call may be escalated to an AI-voice call (only
    # if also authorized for AI voice); otherwise the licensed agent is alerted.
    escalate_to_voice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DialAttempt(Base):
    """One row per call actually initiated — the reporting fact table."""
    __tablename__ = "dial_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    dial_queue_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="initiated")
    answered_by: Mapped[str | None] = mapped_column(String(32))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SpendEntry(Base):
    """Estimated outbound spend, line by line. Budgets sum this table."""
    __tablename__ = "spend_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # voice | ai | transcribe | sms
    quantity: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    unit_cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), nullable=False)
    amount_usd: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)



class RateSheet(Base):
    """A carrier rate book, imported as data and quoted only once VERIFIED.

    Verification (domain.quoting.rate_sheets.verify_rate_sheet) requires spot
    checks copied from the carrier's own quote tool or illustration to be
    reproduced to the cent. Draft, retired, expired, out-of-state and test
    sheets never price a prospect-facing quote in production.
    """
    __tablename__ = "rate_sheets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    carrier: Mapped[str] = mapped_column(String(128), nullable=False)
    product_line: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(128), nullable=False)
    states: Mapped[list] = mapped_column(JSONB, nullable=False)            # USPS codes where sold
    effective_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    expires_date: Mapped[datetime | None] = mapped_column(Date)
    rate_unit: Mapped[str] = mapped_column(String(24), nullable=False)      # see quoting.rate_sheets.RATE_UNITS
    policy_fee_annual: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    policy_fee_monthly: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)  # per_1000_monthly
    modal_factors: Mapped[dict] = mapped_column(JSONB, nullable=False)     # mode -> factor on the base premium
    min_face: Mapped[int] = mapped_column(Integer, nullable=False)
    max_face: Mapped[int] = mapped_column(Integer, nullable=False)
    face_increment: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    default_tier: Mapped[str] = mapped_column(String(24), nullable=False, default="level")
    tier_questions: Mapped[list | None] = mapped_column(JSONB)            # [{key, text, yes_tier}]
    source_document: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|verified|retired
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(128))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification: Mapped[list | None] = mapped_column(JSONB)              # spot checks and results
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateSheetRate(Base):
    __tablename__ = "rate_sheet_rates"
    __table_args__ = (
        UniqueConstraint("sheet_id", "age", "sex", "tobacco", "tier", "face", name="uq_rate_sheet_cell"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    sheet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    sex: Mapped[str] = mapped_column(String(1), nullable=False)            # M | F | U (unisex)
    tobacco: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tier: Mapped[str] = mapped_column(String(24), nullable=False)
    face: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = per-unit rate
    rate: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)


class SmsConversation(Base):
    """One SMS relationship with one lead, persisted across days."""
    __tablename__ = "sms_conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "lead_id", name="uq_sms_conversation_lead"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # pending | active | booked | declined | opted_out | handoff | dormant | closed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="outreach")
    slots: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    followups_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inbound_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outbound_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_action: Mapped[str | None] = mapped_column(String(32))
    llm_tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SmsMessage(Base):
    __tablename__ = "sms_messages"
    __table_args__ = (
        # An outbound step is sent at most once (crash/retry safe).
        UniqueConstraint("conversation_id", "step_key", name="uq_sms_step"),
        # A provider message is processed at most once (webhook retries).
        Index("ux_sms_inbound_sid", "provider_sid", unique=True, postgresql_where=_sql_text("direction = 'in'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)        # in | out
    body: Mapped[str] = mapped_column(Text, nullable=False)
    segments: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    encoding: Mapped[str] = mapped_column(String(8), nullable=False, default="GSM-7")
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")
    step_key: Mapped[str | None] = mapped_column(String(64))
    intent: Mapped[str | None] = mapped_column(String(48))
    quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    llm_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 5), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_sql_text("clock_timestamp()"), index=True)


class PolicySale(Base):
    """A policy the licensed agent wrote from an Eastwood appointment."""
    __tablename__ = "policy_sales"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    carrier: Mapped[str | None] = mapped_column(String(128))
    product_line: Mapped[str | None] = mapped_column(String(64))
    annual_premium_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    commission_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notes: Mapped[str | None] = mapped_column(Text)
