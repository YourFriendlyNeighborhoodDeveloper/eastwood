"""Rate book -> rate sheet -> verified -> prospect quote, against a real
database (EASTWOOD_INTEGRATION=1). Each test runs in its own tenant."""
from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(os.environ.get("EASTWOOD_INTEGRATION") != "1",
                                reason="set EASTWOOD_INTEGRATION=1 with a real database")
TODAY = date(2026, 12, 1)


@pytest.fixture
async def env(monkeypatch):
    import config.settings as cs
    import db.session as dbs

    tenant = f"it-rb-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={
        "default_tenant_id": tenant, "allow_test_rate_sheets": True, "app_env": "development"}))
    dbs._engine, dbs._SessionLocal = None, None
    yield tenant, dbs
    async with dbs.session_scope() as db:
        for sql in ("DELETE FROM quote_records WHERE lead_id IN (SELECT id FROM leads WHERE tenant_id = :t)",
                    "DELETE FROM leads WHERE tenant_id = :t",
                    "DELETE FROM rate_sheet_rates WHERE sheet_id IN (SELECT id FROM rate_sheets WHERE tenant_id = :t)",
                    "DELETE FROM rate_sheets WHERE tenant_id = :t"):
            await db.execute(text(sql), {"t": tenant})
    await dbs._engine.dispose()
    dbs._engine, dbs._SessionLocal = None, None


def _meta(template: dict, **fill) -> dict:
    m = {k: v for k, v in template.items() if not k.startswith("_")}
    m.update(carrier="Synthetic Test Life (NOT A REAL CARRIER)", product_line="final_expense", plan_name="Grid FE",
             states=["MI", "OH"], effective_date="2026-01-01", expires_date="2027-07-01", is_test=True)
    for q in m.get("tier_questions") or []:
        q["text"] = "In the past 2 years, have you had cancer, a heart attack or a stroke?"
    m.update(fill)
    return m


async def _lead(db, tenant):
    from db.models import Lead

    lead = Lead(tenant_id=tenant, phone_e164=f"+1313555{uuid.uuid4().int % 10000:04d}", first_name="Pat",
                state="MI", product_line="final_expense", status="qualified", national_dnc_status="clear",
                contact_authorization={"sms": True}, quarantine_reasons=[])
    db.add(lead)
    await db.flush()
    return lead.id


async def test_premium_grid_book_to_verified_quote(env, tmp_path):
    from domain.quoting import rate_book as rb
    from domain.quoting.prospect import quote_for_prospect
    from domain.quoting.rate_sheets import RateSheetError, import_rate_sheet, verify_rate_sheet
    from tests.fixtures import rate_books as books
    from tests.fixtures.rate_sheets.make_test_sheet import monthly

    tenant, dbs = env
    src = books.grid_pdf(tmp_path / "book.pdf")
    ex = rb.extract(src, unit="flat_monthly")
    rates = rb.write_rates_csv(ex, tmp_path / "rates.csv", "flat_monthly")
    meta = _meta(rb.meta_template(ex, src, "flat_monthly"))
    cases = rb.spot_check_cases(ex.cells, "flat_monthly")
    for c in cases:                       # the operator prices each case in the carrier's own tool
        c["expected"] = str(monthly(int(c["age"]), c["sex"], c["tobacco"] == "Y", c["tier"], int(c["face"])))

    async with dbs.session_scope() as db:
        sheet = await import_rate_sheet(db, rates, json.loads(json.dumps(meta)), tenant)
        with pytest.raises(RateSheetError, match="every rate class"):
            await verify_rate_sheet(db, sheet.id, checks=cases[:3], verified_by="it", tenant_id=tenant)
        res = await verify_rate_sheet(db, sheet.id, checks=cases, verified_by="it", tenant_id=tenant)
        assert res["passed"] and res["status"] == "verified"
        lead_id = await _lead(db, tenant)

    async with dbs.session_scope() as db:
        r = await quote_for_prospect(db, tenant_id=tenant, lead_id=lead_id, product_line="final_expense",
                                     state="MI", age=67, sex="F", tobacco=False, desired_face=12000, today=TODAY)
        # $12,000 is not printed in the grid: the options are the printed amounts that cover it and up
        assert [o.face for o in r.options] == [15000, 20000, 25000]
        assert [o.monthly_premium for o in r.options] == [monthly(67, "F", False, "level", f) for f in (15000, 20000, 25000)]
        assert all(o.quote_ref.startswith("Q") for o in r.options)
        r2 = await quote_for_prospect(db, tenant_id=tenant, lead_id=lead_id, product_line="final_expense",
                                      state="TX", age=67, sex="F", tobacco=False, desired_face=12000, today=TODAY)
        assert not r2.options and any("not sold in TX" in x for x in r2.unavailable_reasons)


async def test_monthly_per_1000_book_quotes_exactly(env, tmp_path):
    from domain.quoting.prospect import quote_for_prospect
    from domain.quoting.rate_sheets import import_rate_sheet, verify_rate_sheet

    tenant, dbs = env
    p = tmp_path / "rates.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["age", "sex", "tobacco", "tier", "rate"])
        for age in range(60, 76):
            for sex, k in (("M", Decimal("1.18")), ("F", Decimal(1))):
                for tob, kt in ((False, Decimal(1)), (True, Decimal("1.4"))):
                    w.writerow([age, sex, "Y" if tob else "N", "level", (Decimal("1.50") + Decimal("0.11") * (age - 60)) * k * kt])
    meta = {"carrier": "Synthetic Monthly Life (NOT A REAL CARRIER)", "product_line": "final_expense",
            "plan_name": "Monthly FE", "states": ["MI"], "effective_date": "2026-01-01", "rate_unit": "per_1000_monthly",
            "policy_fee_monthly": "3.00", "modal_factors": {"monthly": "1"}, "min_face": 5000, "max_face": 30000,
            "face_increment": 1000, "source_document": "synthetic", "is_test": True}

    def expected(age, sex, tob, face):
        rate = (Decimal("1.50") + Decimal("0.11") * (age - 60)) * (Decimal("1.18") if sex == "M" else 1) * (Decimal("1.4") if tob else 1)
        rate = rate.quantize(Decimal("0.0001"))             # what the rate column holds
        return (rate * face / 1000 + Decimal("3.00")).quantize(Decimal("0.01"))

    checks = [{"age": a, "sex": s, "tobacco": "Y" if t else "N", "tier": "level", "face": f, "mode": "monthly",
               "expected": str(expected(a, s, t, f))}
              for a, s, t, f in ((60, "M", False, 10000), (75, "F", True, 25000), (68, "F", False, 7000), (66, "M", True, 12000))]
    async with dbs.session_scope() as db:
        sheet = await import_rate_sheet(db, p, meta, tenant)
        assert (await verify_rate_sheet(db, sheet.id, checks=checks, verified_by="it", tenant_id=tenant))["passed"]
        lead_id = await _lead(db, tenant)
    async with dbs.session_scope() as db:
        r = await quote_for_prospect(db, tenant_id=tenant, lead_id=lead_id, product_line="final_expense",
                                     state="MI", age=70, sex="M", tobacco=False, desired_face=10000, today=TODAY)
        assert [(o.face, o.monthly_premium) for o in r.options] == \
            [(f, expected(70, "M", False, f)) for f in (10000, 15000, 20000)]
