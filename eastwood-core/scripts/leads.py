#!/usr/bin/env python3
"""Operator CLI for lead inventory. Contacts no one.

Run inside the eastwood-core container (files under ./data/private/imports on
the host are mounted read-only at /app/imports):

  docker compose exec eastwood-core python -m scripts.leads import /app/imports/leads.xlsx \\
      --source "Former employer book" --owner "Your Agency" --acquired 2026-11-20 \\
      --use-authorization "Written lead assignment dated 2026-11-18 (ref ABC-123)" \\
      [--map "Other Phone 1=phone"] [--chunk 500]
  ... python -m scripts.leads report <batch_id>          # counters + issue codes (JSON)
  ... python -m scripts.leads errors <batch_id> > errors.csv  # row, severity, code (no PII)
  ... python -m scripts.leads inventory [--json] [--batch <batch_id>]
  ... python -m scripts.leads scrub                       # National DNC for 'unknown' leads
  ... python -m scripts.leads qualify                     # triage + score imported leads

Re-running `import` on the same file is a no-op once complete, and resumes from
the last committed row if a previous run stopped part-way.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _overrides(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--map expects 'Header=field', got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="leads")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    imp.add_argument("--source", required=True, help="where these leads came from")
    imp.add_argument("--owner", help="current owner of the leads (your agency)")
    imp.add_argument("--acquired", type=date.fromisoformat, help="date you acquired the file (YYYY-MM-DD)")
    imp.add_argument("--use-authorization", dest="use_auth",
                     help="reference to the written authorization to use these leads; omit = all quarantined")
    imp.add_argument("--map", action="append", default=[], help="'Header=field' override (repeatable)")
    imp.add_argument("--chunk", type=int, default=500)
    rep = sub.add_parser("report")
    rep.add_argument("batch_id")
    err = sub.add_parser("errors")
    err.add_argument("batch_id")
    inv = sub.add_parser("inventory")
    inv.add_argument("--json", action="store_true")
    inv.add_argument("--batch")
    sub.add_parser("scrub")
    sub.add_parser("qualify")
    a = ap.parse_args(argv)

    if a.cmd == "import":
        from domain.imports import ImportOptions, run_import

        result = await run_import(a.file, ImportOptions(
            source_name=a.source, lead_owner=a.owner, acquisition_date=a.acquired,
            use_authorization_ref=a.use_auth, column_overrides=_overrides(a.map), chunk_size=a.chunk,
        ))
        print(json.dumps(result.as_dict(), indent=2, default=str))
    elif a.cmd == "report":
        from domain.imports import batch_report

        print(json.dumps(await batch_report(a.batch_id), indent=2, default=str))
    elif a.cmd == "errors":
        from domain.imports import errors_csv

        sys.stdout.write(await errors_csv(a.batch_id))
    elif a.cmd == "inventory":
        from db.session import session_scope
        from domain.inventory import build_inventory, inventory_markdown

        async with session_scope() as s:
            inv = await build_inventory(s, import_batch_id=a.batch)
        print(json.dumps(inv, indent=2, default=str) if a.json else inventory_markdown(inv))
    elif a.cmd == "scrub":
        from domain.lead_ops import scrub_unknown

        print(json.dumps(await scrub_unknown(), indent=2, default=str))
    elif a.cmd == "qualify":
        from domain.lead_ops import qualify_imported

        print(json.dumps(await qualify_imported(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
