"""Database integration: import pipeline, calendar concurrency, inventory,
campaign enqueue, attempt spend. Requires a real Postgres (the app's
DATABASE_URL) and EASTWOOD_INTEGRATION=1; skipped otherwise.

Each test runs in its own throwaway tenant, so it never touches other data:

  docker exec -e EASTWOOD_INTEGRATION=1 <core-container> python -m pytest tests/integration -q
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

pytestmark = pytest.mark.skipif(os.environ.get("EASTWOOD_INTEGRATION") != "1",
                                reason="set EASTWOOD_INTEGRATION=1 with a real database")

AGENCY = "Integration Test Agency"
CONSENT = (f"I agree {AGENCY} may call me using automated technology and an artificial or AI voice, "
           "and send me text messages, at the number provided.")
HEADER = "Lead ID,First Name,Last Name,Other Phone 1,State,DOB,Date Created,Product,TCPA Consent,Consent Date,Consent URL,DNC\n"


def _line(i: int, phone: str, *, state="Michigan", consent=CONSENT, dnc="") -> str:
    return (f"EXT-{i},Pat{i},Synthetic,{phone},{state},1955-03-04,2026-09-01,Final Expense,"
            f"\"{consent}\",2026-09-01 10:00:00,https://form.example/{i},{dnc}\n")


@pytest.fixture
async def env(monkeypatch):
    """Fresh engine per test (pytest-asyncio uses a new loop) + an isolated tenant."""
    import config.settings as cs
    import db.session as dbs

    tenant = f"it-{uuid.uuid4().hex[:10]}"
    s = cs.get_settings().model_copy(update={
        "default_tenant_id": tenant, "consent_authorized_sellers": AGENCY, "active_states": "MI,OH",
        "dnc_test_allowlist": "", "max_concurrent_calls": 5, "dial_calls_per_minute": 60,
    })
    monkeypatch.setattr(cs, "_settings", s)
    dbs._engine, dbs._SessionLocal = None, None
    yield tenant
    async with dbs.session_scope() as db:
        for sql in (
            "DELETE FROM spend_ledger WHERE tenant_id = :t",
            "DELETE FROM dial_attempts WHERE tenant_id = :t",
            "DELETE FROM dial_queue WHERE tenant_id = :t",
            "DELETE FROM campaigns WHERE tenant_id = :t",
            "DELETE FROM consents WHERE tenant_id = :t",
            "DELETE FROM suppressions WHERE tenant_id = :t",
            "DELETE FROM import_errors WHERE batch_id IN (SELECT id FROM import_batches WHERE tenant_id = :t)",
            "DELETE FROM import_batches WHERE tenant_id = :t",
            "DELETE FROM booking_slots WHERE tenant_id = :t",
            "DELETE FROM leads WHERE tenant_id = :t",
        ):
            await db.execute(text(sql), {"t": tenant})
    await dbs._engine.dispose()
    dbs._engine, dbs._SessionLocal = None, None


async def _count(sql: str, **p) -> int:
    from db.session import session_scope

    async with session_scope() as db:
        return int((await db.execute(text(sql), p)).scalar_one())


def _opts(tenant, **kw):
    from domain.imports import ImportOptions

    base = dict(source_name="IT file", lead_owner="IT Owner", tenant_id=tenant, chunk_size=50,
                use_authorization_ref="Assignment letter IT-1")
    base.update(kw)
    return ImportOptions(**base)


async def test_import_counts_dedupes_rejects_and_quarantines(env, tmp_path):
    from db.models import Suppression
    from db.session import session_scope
    from domain.imports import run_import

    tenant = env
    async with session_scope() as db:
        db.add(Suppression(tenant_id=tenant, phone_e164="+13135550199", reason="dnc_request"))
    rows = [_line(i, f"313555{100 + i:04d}") for i in range(1, 91)]           # 90 good
    rows += [_line(91, "3135550101")]                                          # duplicate in file
    rows += [_line(92, "12345"), _line(93, "")]                               # invalid, missing
    rows += [_line(94, "3135550199")]                                          # previously opted out
    rows += [_line(95, "3135550195", consent=CONSENT.replace(AGENCY, "Former Employer Inc"))]
    rows += [_line(96, "3135550196", dnc="Yes")]                               # source DNC
    rows += [_line(97, "3135550197", state="Ontario")]                         # unknown state
    p = tmp_path / "leads.csv"
    p.write_text(HEADER + "".join(rows))

    r = await run_import(p, _opts(tenant))
    assert r.status == "completed" and r.total_rows == 97
    assert (r.inserted, r.duplicates_in_file, r.rejected) == (93, 1, 3)
    assert r.authorized_ai_voice == 90 and r.quarantined == 3
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t", t=tenant) == 93
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t AND status = 'quarantined'", t=tenant) == 3
    assert await _count("SELECT count(*) FROM consents WHERE tenant_id = :t AND express_written", t=tenant) == 92
    assert await _count("SELECT count(*) FROM suppressions WHERE tenant_id = :t AND reason = 'source_dnc_flag'", t=tenant) == 1
    # provenance preserved
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t AND lead_owner = 'IT Owner' "
                        "AND source = 'IT file' AND acquired_at IS NOT NULL AND external_id LIKE 'EXT-%'", t=tenant) == 93
    codes = {c: n for c, n in [tuple(x) for x in (await _rows(
        "SELECT code, count(*) FROM import_errors WHERE batch_id = CAST(:b AS uuid) GROUP BY code", b=r.batch_id))]}
    assert codes["invalid_phone"] == 1 and codes["missing_phone"] == 1
    assert codes["previously_opted_out"] == 1 and codes["duplicate_in_file"] == 1 and codes["unknown_state"] == 1

    again = await run_import(p, _opts(tenant))
    assert again.status == "already_imported" and again.batch_id == r.batch_id
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t", t=tenant) == 93


async def _rows(sql: str, **p):
    from db.session import session_scope

    async with session_scope() as db:
        return (await db.execute(text(sql), p)).all()


async def test_crashed_import_resumes_exactly_once(env, tmp_path):
    from domain.imports import run_import

    tenant = env
    p = tmp_path / "resume.csv"
    p.write_text(HEADER + "".join(_line(i, f"248555{100 + i % 100:04d}") if i < 100
                                  else _line(i, f"586555{100 + i % 100:04d}") for i in range(1, 181)))
    with pytest.raises(RuntimeError, match="simulated crash"):
        await run_import(p, _opts(tenant, chunk_size=40), fail_after_chunks=2)
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t", t=tenant) == 80
    status = (await _rows("SELECT status, processed_rows FROM import_batches WHERE tenant_id = :t", t=tenant))[0]
    assert tuple(status) == ("failed", 81)

    r = await run_import(p, _opts(tenant, chunk_size=40))
    assert r.status == "completed" and r.resumed_from_row == 81
    assert r.inserted == 180 and r.duplicates_in_file == 0
    assert await _count("SELECT count(DISTINCT phone_e164) FROM leads WHERE tenant_id = :t", t=tenant) == 180


async def test_xlsx_import_matches_csv(env, tmp_path):
    from openpyxl import Workbook

    from domain.imports import run_import

    tenant = env
    wb = Workbook()
    ws = wb.active
    ws.append([h for h in HEADER.strip().split(",")])
    for i in range(1, 21):
        ws.append([f"X-{i}", f"Lee{i}", "Synthetic", 7345550100 + i, "MI", "1950-01-01", "2026-08-01",
                   "final expense", CONSENT, "2026-08-01 09:00", f"https://form.example/x{i}", None])
    p = tmp_path / "leads.xlsx"
    wb.save(p)
    r = await run_import(p, _opts(tenant))
    assert (r.file_format, r.inserted, r.authorized_ai_voice) == ("xlsx", 20, 20)


async def test_missing_lead_use_authorization_quarantines_the_batch(env, tmp_path):
    from domain.imports import run_import

    p = tmp_path / "noauth.csv"
    p.write_text(HEADER + "".join(_line(i, f"810555{100 + i:04d}") for i in range(1, 11)))
    r = await run_import(p, _opts(env, use_authorization_ref=None))
    assert (r.inserted, r.quarantined, r.authorized_ai_voice) == (10, 10, 0)
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t AND quarantine_reasons ? 'lead_use_unauthorized'",
                        t=env) == 10


async def test_calendar_never_overbooks_under_concurrency(env):
    from db.session import session_scope
    from domain.scheduling.local import LocalCalendar, SlotUnavailableError, parse_availability

    tenant = env
    tz = ZoneInfo("America/Detroit")
    cal = LocalCalendar(windows=parse_availability("mon-sun 09:00-12:00"), tz=tz, slot_minutes=30,
                        max_per_day=2, min_notice_hours=0, horizon_days=10)
    now = datetime.now(timezone.utc)
    day = (now.astimezone(tz) + timedelta(days=3)).date()
    base = datetime(day.year, day.month, day.day, 9, 0, tzinfo=tz)
    starts = [base + timedelta(minutes=30 * k) for k in range(4)]

    async def book(start):
        try:
            async with session_scope() as db:
                await cal.reserve(db, tenant, start, now=now)
            return "ok"
        except SlotUnavailableError as e:
            return e.reason
        except Exception as e:  # unique index backstop
            return type(e).__name__

    same = await asyncio.gather(*[book(starts[0]) for _ in range(25)])
    assert same.count("ok") == 1, same
    mixed = await asyncio.gather(*[book(s) for s in starts[1:] for _ in range(5)])
    assert mixed.count("ok") == 1, "max_per_day=2 including the first booking"
    assert await _count("SELECT count(*) FROM booking_slots WHERE tenant_id = :t AND NOT available", t=tenant) == 2
    async with session_scope() as db:
        assert await cal.available_times(db, tenant, now=now) and all(
            datetime.fromisoformat(t).astimezone(tz).date() != day for t in await cal.available_times(db, tenant, now=now))


async def test_inventory_campaign_enqueue_and_spend(env, tmp_path):
    from db.models import DialAttempt
    from db.session import session_scope
    from domain.attempts import close_attempt
    from domain.campaigns import create_campaign, enqueue_leads
    from domain.imports import run_import
    from domain.inventory import build_inventory

    tenant = env
    lines = [_line(i, f"313555{100 + i:04d}") for i in range(1, 31)]
    lines += [_line(31 + i, f"614555{100 + i:04d}", state="Ohio") for i in range(10)]
    lines += [_line(41 + i, f"920555{100 + i:04d}", state="Wisconsin") for i in range(5)]
    p = tmp_path / "inv.csv"
    p.write_text(HEADER + "".join(lines))
    await run_import(p, _opts(tenant))

    async with session_scope() as db:
        inv = await build_inventory(db, tenant)
    t = inv["totals"]
    assert (t["leads"], t["ai_voice_eligible"], t["area_codes"]) == (45, 45, 3)
    assert t["ai_voice_in_active_states"] == 40
    assert inv["ftc_registry"]["all_leads"]["annual_fee_usd"] == 0
    assert {r["state"]: r["leads"] for r in inv["by_state"]} == {"MI": 30, "OH": 10, "WI": 5}

    # Nothing is DNC-cleared yet: enqueue must take nobody.
    async with session_scope() as db:
        c = await create_campaign(db, {"name": "IT", "budget_usd": 10, "states": ["MI"], "max_concurrent_calls": 2}, tenant)
        r = await enqueue_leads(db, c)
    assert r["enqueued"] == 0 and r["breakdown_after_enqueue"].get("dnc_unknown") == 30

    async with session_scope() as db:
        await db.execute(text("UPDATE leads SET national_dnc_status = 'clear' WHERE tenant_id = :t"), {"t": tenant})
        c = (await db.execute(text("SELECT id FROM campaigns WHERE tenant_id = :t"), {"t": tenant})).scalar_one()
        from domain.campaigns import get_campaign

        camp = await get_campaign(db, c, tenant)
        first = await enqueue_leads(db, camp, limit=12)
        second = await enqueue_leads(db, camp)
        third = await enqueue_leads(db, camp)
    assert (first["enqueued"], second["enqueued"], third["enqueued"]) == (12, 18, 0), "MI only, never twice"

    from domain.campaigns import set_status

    async with session_scope() as db:
        camp = await get_campaign(db, c, tenant)
        await set_status(db, camp, "completed")
    assert await _count("SELECT count(*) FROM dial_queue WHERE campaign_id = :c AND status = 'pending'", c=c) == 0
    assert await _count("SELECT count(*) FROM leads WHERE tenant_id = :t AND status = 'qualified'", t=tenant) >= 30, \
        "completing a campaign releases its unworked leads"

    async with session_scope() as db:
        lead_id = (await db.execute(text("SELECT id FROM leads WHERE tenant_id = :t LIMIT 1"), {"t": tenant})).scalar_one()
        db.add(DialAttempt(tenant_id=tenant, campaign_id=c, lead_id=lead_id, session_id="it-sess", status="initiated"))
    async with session_scope() as db:
        a = await close_attempt(db, session_id="it-sess", status="completed", duration_seconds="125", answered_by="human")
    async with session_scope() as db:
        dup = await close_attempt(db, session_id="it-sess", status="completed", duration_seconds="125")
    assert a["cost_usd"] == pytest.approx(3 * 0.014 + (125 / 60) * (0.15 + 0.006), abs=0.001)
    assert dup is None, "a duplicate callback books nothing"
    assert await _count("SELECT count(*) FROM spend_ledger WHERE tenant_id = :t", t=tenant) == 3
