"""Bulk lead import: idempotent, resumable, duplicate-aware, fully reported.

Guarantees
- Idempotent: a file is identified by its SHA-256. Re-importing a completed
  file is a no-op that returns the original batch.
- Resumable: rows are processed in chunks; each chunk's inserts AND the batch
  cursor (processed_rows) commit in one transaction. After a crash, running
  the same file again continues after the last committed row — no row is
  inserted twice, none is skipped.
- Exclusive: a Postgres advisory lock keyed on the file stops two processes
  importing the same file at once.
- Duplicate-aware: one lead per phone number per tenant. Repeats inside the
  file and numbers already in the database are reported, not inserted.
  Previously opted-out numbers are never re-imported.
- Provenance: owner, source, acquisition date, external id and the consent
  evidence are stored with every lead; the original row is kept in raw_payload.
- Authorization: every lead is classified per channel
  (domain.imports.classify). Leads not authorized for AI-voice contact are
  imported with status "quarantined" and can never be dialed.
- Reporting: per-row errors and warnings (row number + code, never PII) in
  import_errors, plus batch counters.

Nothing here contacts anyone or calls a third-party API.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import get_settings
from db.models import ConsentRecord, ImportBatch, ImportRowError, Lead, Suppression
from db.session import _get_engine, session_scope
from domain.tcpa import supported_states

from .classify import ConsentEvidence, classify
from .mapping import NormalizedRow, RowIssue, build_column_map, describe_column_map, normalize_row
from .readers import OCR_CONFIDENCE_COLUMN, file_sha256, open_rows

logger = logging.getLogger(__name__)

QUARANTINED = "quarantined"
IMPORTED = "imported"


class ImportConfigError(ValueError):
    """The file cannot be imported as configured (e.g. no phone column)."""


class ImportInProgressError(RuntimeError):
    """Another process holds the lock for this file."""


@dataclass
class ImportOptions:
    source_name: str
    lead_owner: str | None = None
    acquisition_date: date | None = None
    # Reference to the written authorization to use these leads. Without one,
    # every lead in the batch is quarantined (lead_use_unauthorized).
    use_authorization_ref: str | None = None
    column_overrides: dict[str, str] | None = None
    tenant_id: str | None = None
    chunk_size: int = 500
    # Name to record for the batch (dashboard uploads are stored under their
    # content hash; this keeps the operator's original file name).
    file_label: str | None = None


@dataclass
class ImportResult:
    batch_id: str
    status: str
    file_format: str
    total_rows: int = 0
    inserted: int = 0
    duplicates_in_file: int = 0
    duplicates_existing: int = 0
    rejected: int = 0
    quarantined: int = 0
    authorized_ai_voice: int = 0
    resumed_from_row: int = 0
    elapsed_seconds: float = 0.0
    rows_per_second: float = 0.0
    column_map: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _batch_result(batch: ImportBatch, status: str, **extra: Any) -> ImportResult:
    return ImportResult(
        batch_id=str(batch.id),
        status=status,
        file_format=batch.file_format,
        total_rows=batch.total_rows,
        inserted=batch.inserted,
        duplicates_in_file=batch.duplicates_in_file,
        duplicates_existing=batch.duplicates_existing,
        rejected=batch.rejected,
        quarantined=batch.quarantined,
        authorized_ai_voice=batch.authorized_ai_voice,
        column_map=batch.column_map or {},
        **extra,
    )


async def run_import(path: Path | str, opts: ImportOptions, *, fail_after_chunks: int | None = None) -> ImportResult:
    """Import (or resume importing) one file. ``fail_after_chunks`` is a test hook
    that raises after that many committed chunks, to prove resumability."""
    path = Path(path)
    settings = get_settings()
    tenant_id = opts.tenant_id or settings.default_tenant_id
    started = time.perf_counter()

    # Hashing and opening run in a worker thread: a scanned PDF is OCR'd here
    # (seconds per page) and must not stall webhooks or the SMS engine.
    sha = await asyncio.to_thread(file_sha256, path)
    fmt, headers, rows = await asyncio.to_thread(open_rows, path)
    mapping = build_column_map(headers, opts.column_overrides)
    if "phone" not in mapping.values():
        raise ImportConfigError(
            "No phone column recognized. Headers: "
            + ", ".join(h for h in headers if h)
            + ". Map one explicitly, e.g. --map 'Other Phone 1=phone'."
        )
    column_map = describe_column_map(headers, mapping)

    lock_conn = await _get_engine().connect()
    try:
        locked = (await lock_conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": f"import:{tenant_id}:{sha}"}
        )).scalar_one()
        if not locked:
            raise ImportInProgressError(f"{path.name} is already being imported by another process")

        async with session_scope() as s:
            batch = (await s.execute(
                select(ImportBatch).where(ImportBatch.tenant_id == tenant_id, ImportBatch.file_sha256 == sha)
            )).scalars().first()
            if batch is not None and batch.status == "completed":
                logger.info("import %s already completed (batch %s) — no-op", path.name, batch.id)
                return _batch_result(batch, "already_imported")
            if batch is None:
                batch = ImportBatch(
                    tenant_id=tenant_id,
                    file_name=(opts.file_label or path.name)[:256],
                    file_sha256=sha,
                    file_format=fmt,
                    source_name=opts.source_name[:128],
                    lead_owner=opts.lead_owner,
                    acquisition_date=opts.acquisition_date,
                    use_authorization_ref=opts.use_authorization_ref,
                    column_map=column_map,
                    status="processing",
                )
                s.add(batch)
                await s.flush()
            else:
                batch.status = "processing"
                batch.error = None
            batch_id, cursor = batch.id, batch.processed_rows
            use_authorized = bool((batch.use_authorization_ref or "").strip())

        ctx = _ChunkContext(
            tenant_id=tenant_id,
            batch_id=batch_id,
            source_name=opts.source_name[:128],
            lead_owner=opts.lead_owner,
            acquisition_date=opts.acquisition_date,
            use_authorized=use_authorized,
            sellers=settings.consent_sellers_list(),
            max_age_days=settings.consent_max_age_days,
            states=supported_states(),
            headers=headers,
            mapping=mapping,
            ocr_min_confidence=settings.ocr_min_confidence,
        )

        total_rows = 0
        chunks_done = 0
        chunk: list[tuple[int, list[Any]]] = []
        try:
            for row_number, values in rows:
                total_rows += 1
                if row_number <= cursor:
                    continue  # committed by an earlier run
                chunk.append((row_number, values))
                if len(chunk) >= opts.chunk_size:
                    await _process_chunk(ctx, chunk)
                    chunk = []
                    chunks_done += 1
                    if fail_after_chunks is not None and chunks_done >= fail_after_chunks:
                        raise RuntimeError("simulated crash (test hook)")
            if chunk:
                await _process_chunk(ctx, chunk)
        except Exception as exc:
            async with session_scope() as s:
                await s.execute(
                    update(ImportBatch).where(ImportBatch.id == batch_id)
                    .values(status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
                )
            raise

        async with session_scope() as s:
            await s.execute(
                update(ImportBatch).where(ImportBatch.id == batch_id)
                .values(status="completed", total_rows=total_rows, completed_at=datetime.now(timezone.utc))
            )
            batch = (await s.execute(select(ImportBatch).where(ImportBatch.id == batch_id))).scalars().one()
            elapsed = time.perf_counter() - started
            processed_now = max(total_rows - cursor_rows(cursor), 0)
            result = _batch_result(
                batch, "completed",
                resumed_from_row=cursor,
                elapsed_seconds=round(elapsed, 2),
                rows_per_second=round(processed_now / elapsed, 1) if elapsed else 0.0,
            )
        logger.info("import %s completed: %s", path.name, result.as_dict())
        return result
    finally:
        try:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"import:{tenant_id}:{sha}"})
        finally:
            await lock_conn.close()


def cursor_rows(cursor: int) -> int:
    """Data rows already committed when the cursor is a spreadsheet row number."""
    return max(cursor - 1, 0)


@dataclass
class _ChunkContext:
    tenant_id: str
    batch_id: uuid.UUID
    source_name: str
    lead_owner: str | None
    acquisition_date: date | None
    use_authorized: bool
    sellers: list[str]
    max_age_days: int
    states: frozenset[str]
    headers: list[str]
    mapping: dict[int, str]
    ocr_min_confidence: float = 85.0


async def _process_chunk(ctx: _ChunkContext, chunk: list[tuple[int, list[Any]]]) -> None:
    now = datetime.now(timezone.utc)
    normalized: list[NormalizedRow] = [
        normalize_row(n, ctx.headers, values, ctx.mapping, today=now.date()) for n, values in chunk
    ]
    errors: list[dict] = []
    counters = {"inserted": 0, "duplicates_in_file": 0, "duplicates_existing": 0,
                "rejected": 0, "quarantined": 0, "authorized_ai_voice": 0}

    def issue(row: int, i: RowIssue) -> None:
        errors.append({"id": uuid.uuid4(), "batch_id": ctx.batch_id, "row_number": row,
                       "severity": i.severity, "code": i.code, "field": i.field,
                       "detail": (i.detail or None)})

    async with session_scope() as s:
        valid: list[NormalizedRow] = []
        for r in normalized:
            for i in r.issues:
                issue(r.row_number, i)
            if r.rejected:
                counters["rejected"] += 1
            else:
                valid.append(r)

        phones = sorted({r.phone_e164 for r in valid if r.phone_e164})
        existing: dict[str, uuid.UUID | None] = {}
        suppressed: set[str] = set()
        if phones:
            for phone, batch in (await s.execute(
                select(Lead.phone_e164, Lead.import_batch_id)
                .where(Lead.tenant_id == ctx.tenant_id, Lead.phone_e164.in_(phones))
            )).all():
                existing.setdefault(phone, batch)
            suppressed = set((await s.execute(
                select(Suppression.phone_e164)
                .where(Suppression.tenant_id == ctx.tenant_id, Suppression.phone_e164.in_(phones))
            )).scalars())

        leads: list[dict] = []
        consents: list[dict] = []
        new_suppressions: list[dict] = []
        seen: set[str] = set()
        for r in valid:
            p = r.phone_e164
            if p in suppressed:
                counters["rejected"] += 1
                issue(r.row_number, RowIssue("error", "previously_opted_out", "phone",
                                             "number is on the suppression list"))
                continue
            if p in seen or (p in existing and existing[p] == ctx.batch_id):
                counters["duplicates_in_file"] += 1
                issue(r.row_number, RowIssue("warning", "duplicate_in_file", "phone",
                                             "same number earlier in this file"))
                continue
            if p in existing:
                counters["duplicates_existing"] += 1
                issue(r.row_number, RowIssue("warning", "duplicate_existing", "phone",
                                             "number already in Eastwood"))
                continue
            seen.add(p)

            f = r.fields
            evidence = ConsentEvidence(
                text=f["consent_text"], captured_at=f["consent_captured_at"], url=f["consent_url"],
                certificate=f["consent_certificate"], source=f["source_detail"] or ctx.source_name,
                ip=f["consent_ip"],
            ) if f["consent_text"] else None
            auth = classify(
                use_authorized=ctx.use_authorized, consent=evidence, sellers=ctx.sellers,
                state=f["state"], supported_states=ctx.states, source_dnc=f["source_dnc"],
                max_age_days=ctx.max_age_days, now=now, phone_region=f["phone_region"],
            )
            ocr_conf = r.raw.get(OCR_CONFIDENCE_COLUMN)
            if ocr_conf is not None and float(ocr_conf) < ctx.ocr_min_confidence:
                # A misread digit is a stranger: nothing from a low-confidence
                # scan is contacted until a human has checked it.
                auth.ai_voice = auth.sms = auth.manual_call = False
                auth.reasons = list(dict.fromkeys(auth.reasons + ["ocr_low_confidence"]))
                issue(r.row_number, RowIssue("warning", "low_ocr_confidence", "phone",
                                             f"OCR confidence {ocr_conf} below {ctx.ocr_min_confidence:g}"))
            acquired_at = f["acquired_at"] or (
                datetime(ctx.acquisition_date.year, ctx.acquisition_date.month, ctx.acquisition_date.day,
                         tzinfo=timezone.utc) if ctx.acquisition_date else None
            )
            lead_id = uuid.uuid4()
            status = IMPORTED if auth.ai_voice else QUARANTINED
            counters["quarantined" if status == QUARANTINED else "authorized_ai_voice"] += 1
            leads.append({
                "id": lead_id,
                "tenant_id": ctx.tenant_id,
                "phone_e164": p,
                "first_name": f["first_name"],
                "last_name": f["last_name"],
                "email": f["email"],
                "state": f["state"],
                "zip_code": f["zip_code"],
                "age": f["age"],
                "sex": f["sex"],
                "tobacco": f["tobacco"],
                "product_line": f["product_line"],
                "source": ctx.source_name,
                "status": status,
                # Current owner (the importing agent/agency) wins; the source
                # file's owner/agent column is preserved under raw_payload._import.
                "lead_owner": ctx.lead_owner or f["lead_owner"],
                "external_id": f["external_id"],
                "acquired_at": acquired_at,
                "import_batch_id": ctx.batch_id,
                "contact_authorization": auth.as_json(now),
                "quarantine_reasons": auth.reasons,
                "raw_payload": {**r.raw, "_import": {
                    "batch_id": str(ctx.batch_id), "row": r.row_number,
                    "source_detail": f["source_detail"], "product_raw": f["product_raw"],
                    "source_owner": f["lead_owner"],
                }},
            })
            if evidence is not None:
                consents.append({
                    "id": uuid.uuid4(),
                    "lead_id": lead_id,
                    "tenant_id": ctx.tenant_id,
                    "consent_type": "tcpa_import",
                    "express_written": auth.consent_status == "documented",
                    "source_url": f["consent_url"],
                    "ip_address": f["consent_ip"],
                    "evidence_pack": {
                        "text": f["consent_text"],
                        "captured_at": f["consent_captured_at"].isoformat() if f["consent_captured_at"] else None,
                        "url": f["consent_url"],
                        "certificate": f["consent_certificate"],
                        "source": evidence.source,
                        "seller_named": auth.seller_named,
                        "covers": {"ai_voice": auth.ai_voice, "sms": auth.sms},
                        "import_batch_id": str(ctx.batch_id),
                        "row": r.row_number,
                    },
                })
            if f["source_dnc"]:
                new_suppressions.append({"id": uuid.uuid4(), "tenant_id": ctx.tenant_id, "phone_e164": p,
                                         "reason": "source_dnc_flag",
                                         "notes": f"import batch {ctx.batch_id} row {r.row_number}"})

        if leads:
            inserted = set((await s.execute(
                pg_insert(Lead).values(leads).on_conflict_do_nothing().returning(Lead.id)
            )).scalars())
            lost = [l for l in leads if l["id"] not in inserted]
            for l in lost:  # a concurrent writer took the number between check and insert
                counters["duplicates_existing"] += 1
                counters["quarantined" if l["status"] == QUARANTINED else "authorized_ai_voice"] -= 1
                issue(l["raw_payload"]["_import"]["row"], RowIssue("warning", "duplicate_existing", "phone"))
            counters["inserted"] += len(inserted)
            consents = [c for c in consents if c["lead_id"] in inserted]
            if consents:
                await s.execute(pg_insert(ConsentRecord).values(consents))
        if new_suppressions:
            await s.execute(pg_insert(Suppression).values(new_suppressions))
        if errors:
            await s.execute(pg_insert(ImportRowError).values(errors))

        await s.execute(
            update(ImportBatch).where(ImportBatch.id == ctx.batch_id).values(
                processed_rows=chunk[-1][0],
                **{k: getattr(ImportBatch, k) + v for k, v in counters.items()},
            )
        )


async def batch_report(batch_id: uuid.UUID | str) -> dict | None:
    async with session_scope() as s:
        batch = (await s.execute(select(ImportBatch).where(ImportBatch.id == uuid.UUID(str(batch_id))))).scalars().first()
        if batch is None:
            return None
        codes = (await s.execute(
            select(ImportRowError.severity, ImportRowError.code, func.count())
            .where(ImportRowError.batch_id == batch.id)
            .group_by(ImportRowError.severity, ImportRowError.code)
            .order_by(func.count().desc())
        )).all()
        reasons = (await s.execute(text(
            "SELECT r AS reason, count(*) FROM leads, jsonb_array_elements_text(quarantine_reasons) r "
            "WHERE import_batch_id = :b GROUP BY r ORDER BY 2 DESC"
        ), {"b": batch.id})).all()
        return {
            "batch_id": str(batch.id),
            "file_name": batch.file_name,
            "file_format": batch.file_format,
            "status": batch.status,
            "source_name": batch.source_name,
            "lead_owner": batch.lead_owner,
            "acquisition_date": batch.acquisition_date.isoformat() if batch.acquisition_date else None,
            "use_authorization_ref_present": bool(batch.use_authorization_ref),
            "total_rows": batch.total_rows,
            "processed_rows_cursor": batch.processed_rows,
            "inserted": batch.inserted,
            "duplicates_in_file": batch.duplicates_in_file,
            "duplicates_existing": batch.duplicates_existing,
            "rejected": batch.rejected,
            "quarantined": batch.quarantined,
            "authorized_ai_voice": batch.authorized_ai_voice,
            "issues": [{"severity": sev, "code": code, "count": n} for sev, code, n in codes],
            "quarantine_reasons": {r: n for r, n in reasons},
            "column_map": batch.column_map,
            "error": batch.error,
        }


async def errors_csv(batch_id: uuid.UUID | str) -> str:
    import csv
    import io

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["row", "severity", "code", "field", "detail"])
    async with session_scope() as s:
        for e in (await s.execute(
            select(ImportRowError).where(ImportRowError.batch_id == uuid.UUID(str(batch_id)))
            .order_by(ImportRowError.row_number)
        )).scalars():
            w.writerow([e.row_number, e.severity, e.code, e.field or "", e.detail or ""])
    return out.getvalue()
