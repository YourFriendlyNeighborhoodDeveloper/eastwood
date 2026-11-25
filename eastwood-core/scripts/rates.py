#!/usr/bin/env python3
"""Carrier rate sheets: import, verify, list, retire, quote. Contacts no one.

  python -m scripts.rates import rates.csv --meta meta.json
  python -m scripts.rates verify <sheet_id> --checks checks.csv --verified-by "Name"
  python -m scripts.rates list
  python -m scripts.rates retire <sheet_id>
  python -m scripts.rates quote --state MI --age 67 --sex F --tobacco N --face 15000 [--tier level]

rates.csv columns: age, sex (M/F/U), tobacco (Y/N), tier, rate[, face]
meta.json: carrier, product_line, plan_name, states, effective_date[, expires_date], rate_unit
           (per_1000_annual | flat_monthly), policy_fee_annual, modal_factors {mode: factor},
           rounding (final | annual_then_modal), min_face, max_face, face_increment,
           default_tier[, tier_questions], source_document
checks.csv: age, sex, tobacco, tier, face, mode, expected — copied from the CARRIER's own
            quote tool or illustration. Verification requires every check to match to the cent.
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
    a = ap.parse_args(argv)

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
