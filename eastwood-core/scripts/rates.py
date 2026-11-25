#!/usr/bin/env python3
"""Carrier rate sheets: extract, import, verify, list, retire, quote. Contacts no one.

  python -m scripts.rates extract ratebook.pdf --unit per_1000_annual --out ./mysheet
        reads the carrier's own rate book (PDF with text, XLSX or CSV) into
        ./mysheet/rates.csv + meta.template.json + report.txt for review
  python -m scripts.rates checks-template ./mysheet/rates.csv --unit per_1000_annual --out ./mysheet/checks.csv
        the cases to price in the carrier's own quote tool (fill in "expected")
  python -m scripts.rates import rates.csv --meta meta.json
  python -m scripts.rates verify <sheet_id> --checks checks.csv --verified-by "Name"
  python -m scripts.rates list
  python -m scripts.rates retire <sheet_id>
  python -m scripts.rates quote --state MI --age 67 --sex F --tobacco N --face 15000 [--tier level]

rates.csv columns: age, sex (M/F/U), tobacco (Y/N), tier, rate[, face]
meta.json: carrier, product_line, plan_name, states, effective_date[, expires_date], rate_unit
           (per_1000_annual | per_1000_monthly | flat_monthly | flat_annual),
           policy_fee_annual or policy_fee_monthly, modal_factors {mode: factor},
           rounding (final | annual_then_modal), min_face, max_face, face_increment,
           default_tier[, tier_questions], source_document
checks.csv: age, sex, tobacco, tier, face, mode, expected — copied from the CARRIER's own
            quote tool or illustration. Verification requires every check to match to the cent,
            and at least one check for every rate class (sex x tobacco) and every tier.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _main(argv) -> int:
    ap = argparse.ArgumentParser(prog="rates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import"); i.add_argument("csv", type=Path); i.add_argument("--meta", type=Path, required=True)
    v = sub.add_parser("verify"); v.add_argument("sheet_id"); v.add_argument("--checks", type=Path, required=True)
    v.add_argument("--verified-by", required=True)
    sub.add_parser("list")
    r = sub.add_parser("retire"); r.add_argument("sheet_id")
    q = sub.add_parser("quote")
    for flag in ("--state", "--sex", "--tobacco", "--tier", "--product"):
        q.add_argument(flag)
    q.add_argument("--age", type=int, required=True); q.add_argument("--face", type=int, required=True)
    q.add_argument("--mode", default="monthly")
    e = sub.add_parser("extract", help="carrier rate book (PDF/XLSX/CSV) -> rates.csv + meta template + report")
    e.add_argument("file", type=Path); e.add_argument("--unit", required=True, choices=["per_1000_annual",
                   "per_1000_monthly", "flat_monthly", "flat_annual"], help="what the book's numbers are")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--tier", help="tier for tables whose title names none (default: level)")
    e.add_argument("--sex", choices=["M", "F", "U"], help="sex for grid tables whose title names none")
    e.add_argument("--tobacco", choices=["Y", "N", "any"],
                   help="tobacco class for tables that name none; 'any' = the book's rates do not depend on "
                        "tobacco (e.g. guaranteed issue): the same rate is stored for both classes")
    ct = sub.add_parser("checks-template", help="spot-check cases to price in the carrier's quote tool")
    ct.add_argument("rates", type=Path); ct.add_argument("--unit", required=True)
    ct.add_argument("--out", type=Path, required=True)
    ct.add_argument("--min-face", type=int, default=5000); ct.add_argument("--max-face", type=int, default=25000)
    a = ap.parse_args(argv)

    if a.cmd == "extract":
        from domain.quoting import rate_book as rb

        ex = rb.extract(a.file, unit=a.unit, default_tier=a.tier, default_sex=a.sex,
                        default_tobacco={"Y": True, "N": False, "any": "any"}.get(a.tobacco))
        a.out.mkdir(parents=True, exist_ok=True)
        report = rb.report_text(ex)
        (a.out / "report.txt").write_text(report + "\n")
        print(report)
        if ex.errors or not ex.cells:
            print(f"\nNOT WRITTEN: fix the errors above (see {a.out / 'report.txt'}).")
            return 2
        rb.write_rates_csv(ex, a.out / "rates.csv", a.unit)
        (a.out / "meta.template.json").write_text(json.dumps(rb.meta_template(ex, a.file, a.unit), indent=2,
                                                             default=str) + "\n")
        print(f"\nWrote {a.out / 'rates.csv'} ({len(ex.cells)} rates) and {a.out / 'meta.template.json'}.\n"
              "Next: review the report, fill every null in the meta from the rate book, save it as meta.json,\n"
              "then: checks-template -> price those cases in the carrier's quote tool -> import -> verify.")
        return 0
    if a.cmd == "checks-template":
        from domain.quoting import rate_book as rb
        from domain.quoting import rate_sheets as rs

        rows = rs.read_rate_rows(a.rates, a.unit)
        cases = rb.spot_check_cases(rows, a.unit, min_face=a.min_face, max_face=a.max_face)
        with open(a.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["age", "sex", "tobacco", "tier", "face", "mode", "expected"])
            w.writeheader(); w.writerows(cases)
        print(f"Wrote {len(cases)} cases to {a.out}: price each in the carrier's own quote tool and fill in"
              " 'expected' (to the cent), then run verify.")
        return 0

    from sqlalchemy import select

    from config.settings import get_settings
    from db.models import RateSheet
    from db.session import session_scope
    from domain.quoting import rate_sheets as rs
    from domain.quoting.prospect import allow_test_sheets

    tenant = get_settings().default_tenant_id
    async with session_scope() as db:
        if a.cmd == "import":
            sheet = await rs.import_rate_sheet(db, a.csv, json.loads(a.meta.read_text()), tenant)
            print(json.dumps({"sheet_id": str(sheet.id), "status": sheet.status, "is_test": sheet.is_test}))
        elif a.cmd == "verify":
            checks = list(csv.DictReader(open(a.checks, newline="")))
            print(json.dumps(await rs.verify_rate_sheet(db, a.sheet_id, checks=checks, verified_by=a.verified_by,
                                                        tenant_id=tenant), indent=2))
        elif a.cmd == "list":
            for s in (await db.execute(select(RateSheet).where(RateSheet.tenant_id == tenant))).scalars():
                print(json.dumps({"id": str(s.id), "carrier": s.carrier, "plan": s.plan_name, "status": s.status,
                                  "is_test": s.is_test, "states": s.states, "effective": str(s.effective_date),
                                  "expires": str(s.expires_date), "verified_by": s.verified_by}))
        elif a.cmd == "retire":
            await rs.retire_rate_sheet(db, a.sheet_id, tenant)
            print("retired")
        elif a.cmd == "quote":
            quotes, reasons = await rs.quote_from_sheets(
                db, tenant_id=tenant, product_line=a.product or "final_expense", state=a.state, age=a.age,
                sex=(a.sex or "").upper()[:1] or None, tobacco=None if a.tobacco is None else rs.parse_tobacco(a.tobacco),
                face=a.face, tier=a.tier, mode=a.mode, allow_test=allow_test_sheets())
            print(json.dumps({"quotes": [q.provenance() for q in quotes], "reasons": reasons}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
