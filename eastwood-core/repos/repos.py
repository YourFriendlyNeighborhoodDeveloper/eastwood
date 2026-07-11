from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Appointment,
    BookingSlot,
    CallRecord,
    CarrierProfileModel,
    CarrierRateTable,
    ConsentRecord,
    DialQueueEntry,
    FollowupTask,
    Lead,
    LeadBatch,
    LeadFieldEvent,
    ObjectionRecord,
    QuoteRecord,
    RulesetModel,
    Suppression,
    TriageResultModel,
)


class LeadRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_raw(self, tenant_id: str, payload: dict[str, Any]) -> Lead:
        phone_e164 = payload.get("phone_e164")
        product_line = payload.get("product_line")

        stmt = (
            insert(Lead)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                phone_e164=phone_e164,
                first_name=payload.get("first_name"),
                last_name=payload.get("last_name"),
                email=payload.get("email"),
                state=payload.get("state"),
                zip_code=payload.get("zip_code"),
                age=payload.get("age"),
                product_line=product_line,
                source=payload.get("source"),
                status="raw",
                raw_payload=payload,
            )
            .on_conflict_do_update(
                constraint="uq_lead_tenant_phone_product",
                set_={
                    "first_name": payload.get("first_name"),
                    "last_name": payload.get("last_name"),
                    "email": payload.get("email"),
                    "state": payload.get("state"),
                    "zip_code": payload.get("zip_code"),
                    "age": payload.get("age"),
                    "source": payload.get("source"),
                    "raw_payload": payload,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            .returning(Lead)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one()

    async def get_by_id(self, lead_id: uuid.UUID) -> Lead | None:
        result = await self._s.execute(select(Lead).where(Lead.id == lead_id))
        return result.scalar_one_or_none()

    async def update_score(
        self, lead_id: uuid.UUID, score: int, band: str, components: dict
    ) -> None:
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                score=score,
                score_band=band,
                score_components=components,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def update_status(self, lead_id: uuid.UUID, status: str) -> None:
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )

    async def merge_enrichment(self, lead_id: uuid.UUID, data: dict) -> None:
        result = await self._s.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if lead:
            existing = lead.enrichment or {}
            lead.enrichment = {**existing, **data}

    async def update_triage(self, lead_id: uuid.UUID, bucket: str, priority: float) -> None:
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(triage_bucket=bucket, triage_priority=priority, updated_at=datetime.now(timezone.utc))
        )

    async def set_national_dnc(
        self,
        lead_id: uuid.UUID,
        status: str,
        provider: str | None = None,
        checked_at: datetime | None = None,
        source: str | None = None,
        audit: dict | None = None,
    ) -> None:
        """Record a National DNC Registry clearance decision and its provenance."""
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                national_dnc_status=status,
                national_dnc_provider=provider,
                national_dnc_checked_at=checked_at,
                national_dnc_source=source,
                national_dnc_audit=audit,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def update_health_data(self, lead_id: uuid.UUID, health_data: dict) -> None:
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(health_data=health_data, updated_at=datetime.now(timezone.utc))
        )

    async def update_age(self, lead_id: uuid.UUID, age: int) -> None:
        await self._s.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(age=age, updated_at=datetime.now(timezone.utc))
        )


class ConsentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, lead_id: uuid.UUID, tenant_id: str, data: dict) -> ConsentRecord:
        record = ConsentRecord(
            lead_id=lead_id,
            tenant_id=tenant_id,
            consent_type=data.get("consent_type", "tcpa"),
            express_written=data.get("express_written", False),
            evidence_pack=data.get("evidence_pack"),
            source_url=data.get("source_url"),
            ip_address=data.get("ip_address"),
            user_agent=data.get("user_agent"),
            expires_at=data.get("expires_at"),
        )
        self._s.add(record)
        await self._s.flush()
        return record

    async def get_latest(self, lead_id: uuid.UUID) -> ConsentRecord | None:
        result = await self._s.execute(
            select(ConsentRecord)
            .where(ConsentRecord.lead_id == lead_id)
            .order_by(ConsentRecord.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class SuppressionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def is_suppressed(self, tenant_id: str, phone_e164: str) -> bool:
        result = await self._s.execute(
            select(Suppression).where(
                Suppression.tenant_id == tenant_id,
                Suppression.phone_e164 == phone_e164,
            )
        )
        return result.scalar_one_or_none() is not None

    async def add(self, tenant_id: str, phone_e164: str, reason: str, notes: str | None = None) -> None:
        self._s.add(
            Suppression(tenant_id=tenant_id, phone_e164=phone_e164, reason=reason, notes=notes)
        )
        await self._s.flush()


class BookingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_available_slots(self, tenant_id: str, after: datetime) -> list[BookingSlot]:
        result = await self._s.execute(
            select(BookingSlot).where(
                BookingSlot.tenant_id == tenant_id,
                BookingSlot.available == True,  # noqa: E712
                BookingSlot.starts_at >= after,
            )
        )
        return list(result.scalars())

    async def book_slot(self, slot_id: uuid.UUID, lead_id: uuid.UUID, tenant_id: str) -> Appointment:
        await self._s.execute(
            update(BookingSlot).where(BookingSlot.id == slot_id).values(available=False)
        )
        appt = Appointment(lead_id=lead_id, slot_id=slot_id, tenant_id=tenant_id)
        self._s.add(appt)
        await self._s.flush()
        return appt


class CallRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, lead_id: uuid.UUID, tenant_id: str, session_id: str | None = None) -> CallRecord:
        record = CallRecord(lead_id=lead_id, tenant_id=tenant_id, session_id=session_id)
        self._s.add(record)
        await self._s.flush()
        return record

    async def update_outcome(
        self, call_id: uuid.UUID, status: str, outcome: str | None, duration: int | None
    ) -> None:
        await self._s.execute(
            update(CallRecord)
            .where(CallRecord.id == call_id)
            .values(status=status, outcome=outcome, duration_seconds=duration, ended_at=datetime.now(timezone.utc))
        )

    async def recent_count(self, lead_id: uuid.UUID, within_days: int) -> int:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
        result = await self._s.execute(
            select(CallRecord).where(
                CallRecord.lead_id == lead_id,
                CallRecord.created_at >= cutoff,
            )
        )
        return len(result.scalars().all())


class ObjectionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, lead_id: uuid.UUID, session_id: str, objection_type: str, text: str | None = None) -> ObjectionRecord:
        record = ObjectionRecord(
            lead_id=lead_id,
            session_id=session_id,
            objection_type=objection_type,
            text=text,
        )
        self._s.add(record)
        await self._s.flush()
        return record


class QuoteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, lead_id: uuid.UUID, session_id: str, data: dict) -> QuoteRecord:
        record = QuoteRecord(
            lead_id=lead_id,
            session_id=session_id,
            product_line=data.get("product_line", ""),
            face_value=data.get("face_value"),
            monthly_premium=data.get("monthly_premium"),
            carrier=data.get("carrier"),
            quote_data=data,
        )
        self._s.add(record)
        await self._s.flush()
        return record


class TriageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(self, lead_id: uuid.UUID, result: Any) -> TriageResultModel:
        record = TriageResultModel(
            lead_id=lead_id,
            bucket=result.bucket,
            severity_score=result.severity_score,
            priority=result.priority,
            confidence=result.confidence,
            needs_enrichment=result.needs_enrichment,
            drivers=result.drivers,
            dead_reason=result.dead_reason,
            ruleset_version=result.ruleset_version,
            scored_at=result.scored_at,
        )
        self._s.add(record)
        await self._s.flush()
        return record


class RulesetRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_active(self) -> RulesetModel | None:
        result = await self._s.execute(
            select(RulesetModel)
            .where(RulesetModel.is_active == True)  # noqa: E712
            .order_by(RulesetModel.effective_date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class BatchRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        batch_id: str,
        tenant_id: str,
        vendor_name: str,
        source_tag: str,
        cost_cents: int,
        notes: str | None = None,
    ) -> LeadBatch:
        batch = LeadBatch(
            id=batch_id,
            tenant_id=tenant_id,
            vendor_name=vendor_name,
            source_tag=source_tag,
            cost_cents=cost_cents,
            notes=notes,
        )
        self._s.add(batch)
        await self._s.flush()
        return batch

    async def get(self, batch_id: str) -> LeadBatch | None:
        result = await self._s.execute(
            select(LeadBatch).where(LeadBatch.id == batch_id)
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(self, tenant_id: str) -> list[LeadBatch]:
        result = await self._s.execute(
            select(LeadBatch)
            .where(LeadBatch.tenant_id == tenant_id)
            .order_by(LeadBatch.created_at.desc())
        )
        return list(result.scalars())

    async def roi_stats(self, batch_id: str) -> dict:
        """Return raw aggregates for one batch — domain layer does the math."""
        from sqlalchemy import case, func as sqlfunc
        result = await self._s.execute(
            select(
                sqlfunc.count(Lead.id).label("lead_count"),
                sqlfunc.sum(
                    case((Lead.ap_cents.isnot(None), 1), else_=0)
                ).label("closed"),
                sqlfunc.coalesce(sqlfunc.sum(Lead.ap_cents), 0).label("total_ap_cents"),
            ).where(Lead.source_batch_id == batch_id)
        )
        row = result.one()

        # contacted = at least one call record exists for the lead
        contacted_result = await self._s.execute(
            select(sqlfunc.count(sqlfunc.distinct(CallRecord.lead_id))).where(
                CallRecord.lead_id.in_(
                    select(Lead.id).where(Lead.source_batch_id == batch_id)
                )
            )
        )
        contacted = contacted_result.scalar_one() or 0

        # qualified = at least one quote record
        qualified_result = await self._s.execute(
            select(sqlfunc.count(sqlfunc.distinct(QuoteRecord.lead_id))).where(
                QuoteRecord.lead_id.in_(
                    select(Lead.id).where(Lead.source_batch_id == batch_id)
                )
            )
        )
        qualified = qualified_result.scalar_one() or 0

        return {
            "lead_count":    row.lead_count or 0,
            "contacted":     contacted,
            "qualified":     qualified,
            "closed":        row.closed or 0,
            "total_ap_cents": int(row.total_ap_cents or 0),
        }

    async def source_roi_summary(self, tenant_id: str) -> list[dict]:
        """Cross-batch ROI by vendor/source — used for spend decisions."""
        from sqlalchemy import case, func as sqlfunc
        result = await self._s.execute(
            select(
                LeadBatch.vendor_name,
                LeadBatch.source_tag,
                sqlfunc.count(LeadBatch.id).label("batch_count"),
                sqlfunc.sum(LeadBatch.cost_cents).label("total_cost_cents"),
                sqlfunc.count(Lead.id).label("lead_count"),
                sqlfunc.sum(
                    case((Lead.ap_cents.isnot(None), 1), else_=0)
                ).label("closed"),
                sqlfunc.coalesce(sqlfunc.sum(Lead.ap_cents), 0).label("total_ap_cents"),
            )
            .join(Lead, Lead.source_batch_id == LeadBatch.id, isouter=True)
            .where(LeadBatch.tenant_id == tenant_id)
            .group_by(LeadBatch.vendor_name, LeadBatch.source_tag)
            .order_by(sqlfunc.coalesce(sqlfunc.sum(Lead.ap_cents), 0).desc())
        )
        rows = result.all()
        out = []
        for r in rows:
            cost = int(r.total_cost_cents or 0)
            ap = int(r.total_ap_cents or 0)
            out.append({
                "vendor_name":     r.vendor_name,
                "source_tag":      r.source_tag,
                "batch_count":     r.batch_count,
                "total_cost_cents": cost,
                "lead_count":      r.lead_count or 0,
                "closed":          r.closed or 0,
                "total_ap_cents":  ap,
                "roi_multiplier":  round(ap / cost, 2) if cost > 0 else 0.0,
            })
        return out


class FollowupRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        lead_id: uuid.UUID,
        tenant_id: str,
        sequence_type: str,
        fire_at: datetime,
        context_data: dict | None = None,
    ) -> FollowupTask:
        task = FollowupTask(
            lead_id=lead_id,
            tenant_id=tenant_id,
            sequence_type=sequence_type,
            fire_at=fire_at,
            context_data=context_data,
        )
        self._s.add(task)
        await self._s.flush()
        return task

    async def get_due(self, now: datetime, limit: int = 50) -> list[FollowupTask]:
        result = await self._s.execute(
            select(FollowupTask)
            .where(
                FollowupTask.status == "pending",
                FollowupTask.fire_at <= now,
            )
            .order_by(FollowupTask.fire_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_sent(self, task_id: uuid.UUID) -> None:
        await self._s.execute(
            update(FollowupTask)
            .where(FollowupTask.id == task_id)
            .values(status="sent", attempt_count=FollowupTask.attempt_count + 1)
        )

    async def mark_failed(self, task_id: uuid.UUID) -> None:
        await self._s.execute(
            update(FollowupTask)
            .where(FollowupTask.id == task_id)
            .values(status="failed", attempt_count=FollowupTask.attempt_count + 1)
        )

    async def cancel_pending_for_lead(self, lead_id: uuid.UUID, sequence_type: str | None = None) -> None:
        """Cancel all pending tasks for a lead — e.g. when they convert."""
        q = update(FollowupTask).where(
            FollowupTask.lead_id == lead_id,
            FollowupTask.status == "pending",
        )
        if sequence_type:
            q = q.where(FollowupTask.sequence_type == sequence_type)
        await self._s.execute(q.values(status="cancelled"))


class DialQueueRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def enqueue(
        self,
        lead_id: uuid.UUID,
        tenant_id: str,
        priority: float,
        scheduled_at: datetime,
        max_attempts: int = 5,
    ) -> DialQueueEntry:
        entry = DialQueueEntry(
            lead_id=lead_id,
            tenant_id=tenant_id,
            priority=priority,
            scheduled_at=scheduled_at,
            max_attempts=max_attempts,
        )
        self._s.add(entry)
        await self._s.flush()
        return entry

    async def get_due(self, now: datetime, limit: int = 10) -> list[DialQueueEntry]:
        result = await self._s.execute(
            select(DialQueueEntry)
            .where(
                DialQueueEntry.status == "pending",
                DialQueueEntry.scheduled_at <= now,
            )
            .order_by(DialQueueEntry.priority.desc(), DialQueueEntry.scheduled_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def count_active(self) -> int:
        from sqlalchemy import func as sqlfunc
        result = await self._s.execute(
            select(sqlfunc.count(DialQueueEntry.id)).where(DialQueueEntry.status == "dialing")
        )
        return result.scalar_one() or 0

    async def get_by_session(self, session_id: str) -> DialQueueEntry | None:
        result = await self._s.execute(
            select(DialQueueEntry)
            .where(DialQueueEntry.session_id == session_id)
            .order_by(DialQueueEntry.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_dialing(self, entry_id: uuid.UUID, session_id: str) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(status="dialing", session_id=session_id, updated_at=datetime.now(timezone.utc))
        )

    async def mark_completed(self, entry_id: uuid.UUID, amd_result: str | None = None) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(status="completed", amd_result=amd_result, updated_at=datetime.now(timezone.utc))
        )

    async def mark_no_answer(self, entry_id: uuid.UUID) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(status="no_answer", updated_at=datetime.now(timezone.utc))
        )

    async def mark_voicemail(self, entry_id: uuid.UUID) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(status="voicemail", amd_result="machine", updated_at=datetime.now(timezone.utc))
        )

    async def reschedule(
        self, entry_id: uuid.UUID, new_scheduled_at: datetime, new_attempt_count: int
    ) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(
                status="pending",
                scheduled_at=new_scheduled_at,
                attempt_count=new_attempt_count,
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def update_session_id(self, entry_id: uuid.UUID, session_id: str) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(session_id=session_id, updated_at=datetime.now(timezone.utc))
        )

    async def mark_terminal(self, entry_id: uuid.UUID, status: str) -> None:
        """Mark a dial_queue entry at a terminal status, freeing the concurrency slot."""
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )

    async def mark_terminal_if_dialing(self, entry_id: uuid.UUID, status: str) -> bool:
        """Conditionally mark terminal only if still 'dialing'. Returns True iff this
        call performed the transition — the persisted idempotency gate for duplicate
        Twilio callbacks."""
        result = await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id, DialQueueEntry.status == "dialing")
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        return (result.rowcount or 0) > 0

    async def reschedule_if_dialing(
        self, entry_id: uuid.UUID, new_scheduled_at: datetime, new_attempt_count: int
    ) -> bool:
        """Conditionally reschedule a retry only if still 'dialing'. Returns True iff
        this call scheduled it, so a duplicate callback cannot move next_attempt_at
        or increment the attempt a second time."""
        result = await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.id == entry_id, DialQueueEntry.status == "dialing")
            .values(
                status="pending",
                scheduled_at=new_scheduled_at,
                attempt_count=new_attempt_count,
                updated_at=datetime.now(timezone.utc),
            )
        )
        return (result.rowcount or 0) > 0

    async def reap_stuck_dialing(self, older_than_minutes: int = 10) -> int:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        result = await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.status == "dialing")
            .where(DialQueueEntry.updated_at < cutoff)
            .values(status="failed", updated_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    async def cancel_for_lead(self, lead_id: uuid.UUID) -> None:
        await self._s.execute(
            update(DialQueueEntry)
            .where(DialQueueEntry.lead_id == lead_id, DialQueueEntry.status == "pending")
            .values(status="cancelled", updated_at=datetime.now(timezone.utc))
        )

    async def batch_progress(self, batch_id: str) -> dict:
        """Funnel stats for every lead in a batch — how far did each get?"""
        from sqlalchemy import func as sqlfunc
        result = await self._s.execute(
            select(
                DialQueueEntry.status,
                sqlfunc.count(DialQueueEntry.id).label("count"),
            )
            .join(Lead, Lead.id == DialQueueEntry.lead_id)
            .where(Lead.source_batch_id == batch_id)
            .group_by(DialQueueEntry.status)
        )
        by_status = {row.status: row.count for row in result}

        # also get total leads in batch and how many have no queue entry yet
        total_result = await self._s.execute(
            select(sqlfunc.count(Lead.id)).where(Lead.source_batch_id == batch_id)
        )
        total = total_result.scalar_one() or 0

        queued_result = await self._s.execute(
            select(sqlfunc.count(sqlfunc.distinct(DialQueueEntry.lead_id)))
            .join(Lead, Lead.id == DialQueueEntry.lead_id)
            .where(Lead.source_batch_id == batch_id)
        )
        queued = queued_result.scalar_one() or 0

        # appointment / booking count
        appt_result = await self._s.execute(
            select(sqlfunc.count(Appointment.id))
            .join(Lead, Lead.id == Appointment.lead_id)
            .where(Lead.source_batch_id == batch_id)
        )
        appointments = appt_result.scalar_one() or 0

        return {
            "total_leads":   total,
            "queued":        queued,
            "not_queued":    total - queued,
            "pending":       by_status.get("pending", 0),
            "dialing":       by_status.get("dialing", 0),
            "completed":     by_status.get("completed", 0),
            "no_answer":     by_status.get("no_answer", 0),
            "voicemail":     by_status.get("voicemail", 0),
            "busy":          by_status.get("busy", 0),
            "cancelled":     by_status.get("cancelled", 0),
            "appointments":  appointments,
        }


class CarrierProfileRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_active_for_product(self, product_line: str) -> list[CarrierProfileModel]:
        result = await self._s.execute(
            select(CarrierProfileModel)
            .where(
                CarrierProfileModel.product_line == product_line,
                CarrierProfileModel.is_active == True,  # noqa: E712
            )
            .order_by(CarrierProfileModel.priority_weight.desc())
        )
        return list(result.scalars())

    async def get_all_active(self) -> list[CarrierProfileModel]:
        result = await self._s.execute(
            select(CarrierProfileModel).where(CarrierProfileModel.is_active == True)  # noqa: E712
        )
        return list(result.scalars())

    async def count(self) -> int:
        from sqlalchemy import func as sqlfunc
        result = await self._s.execute(
            select(sqlfunc.count(CarrierProfileModel.id))
        )
        return result.scalar_one() or 0

    async def bulk_insert(self, profiles: list[dict]) -> None:
        for p in profiles:
            self._s.add(CarrierProfileModel(**p))
        await self._s.flush()


class RateTableRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def has_rows(self, product_line: str) -> bool:
        from sqlalchemy import func as sqlfunc
        result = await self._s.execute(
            select(sqlfunc.count(CarrierRateTable.id)).where(
                CarrierRateTable.product_line == product_line,
                CarrierRateTable.is_active == True,  # noqa: E712
            )
        )
        return (result.scalar_one() or 0) > 0

    async def bulk_insert(self, rows: list[dict]) -> int:
        for r in rows:
            self._s.add(CarrierRateTable(**r))
        await self._s.flush()
        return len(rows)


class LeadFieldEventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        lead_id: uuid.UUID,
        field_name: str,
        capture_key: str,
        new_value: Any,
        old_value: Any = None,
        captured_by: str = "ai_agent",
        source: str | None = None,
        call_id: str | None = None,
        confidence: float | None = None,
    ) -> LeadFieldEvent:
        event = LeadFieldEvent(
            lead_id=lead_id,
            field_name=field_name,
            capture_key=capture_key,
            old_value={"v": old_value} if old_value is not None else None,
            new_value={"v": new_value},
            captured_by=captured_by,
            source=source,
            call_id=call_id,
            confidence=confidence,
        )
        self._s.add(event)
        await self._s.flush()
        return event

    async def history(self, lead_id: uuid.UUID, field_name: str | None = None) -> list[LeadFieldEvent]:
        q = select(LeadFieldEvent).where(LeadFieldEvent.lead_id == lead_id)
        if field_name:
            q = q.where(LeadFieldEvent.field_name == field_name)
        result = await self._s.execute(q.order_by(LeadFieldEvent.created_at))
        return list(result.scalars())
