#!/usr/bin/env python3
"""
Standalone CSV scorer — no DB, no Kafka, no async.

Usage:
  python scripts/run_csv.py leads.csv [output.csv]

Input CSV columns (all optional except lead_id or phone):
  lead_id, phone_e164, source, state, age, tobacco,
  conditions (pipe-separated codes), medications (pipe-separated codes)

Output CSV adds columns:
  bucket, severity_score, priority, confidence, needs_enrichment, drivers, dead_reason, ruleset_version
"""
from __future__ import annotations

import csv
import json
import pathlib
import sys
import uuid

# Ensure the project root is importable
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "domain"))
sys.path.insert(0, str(_ROOT))

from triage.engine import triage_from_lead_dict
from triage.ruleset import DEFAULT_RULESET

_RULESET_JSON = _ROOT / "domain" / "triage" / "ruleset_v0_1.json"
RULESET = json.loads(_RULESET_JSON.read_text()) if _RULESET_JSON.exists() else DEFAULT_RULESET

OUTPUT_FIELDS = ["bucket", "severity_score", "priority", "confidence", "needs_enrichment", "drivers", "dead_reason", "ruleset_version"]


def _parse_row(row: dict) -> tuple[dict, dict]:
    lead = {
        "lead_id": row.get("lead_id") or str(uuid.uuid4()),
        "source":  row.get("source", "csv_import"),
        "state":   row.get("state") or None,
        "age":     int(row["age"]) if row.get("age") else None,
        "on_dnc":  row.get("on_dnc", "").lower() in ("true", "1", "yes"),
        "express_written_consent": row.get("express_written_consent", "").lower() in ("true", "1", "yes"),
    }
    conditions = [
        {"code": c.strip(), "raw_text": c.strip()}
        for c in row.get("conditions", "").split("|")
        if c.strip()
    ]
    medications = [
        {"code": m.strip(), "raw_text": m.strip()}
        for m in row.get("medications", "").split("|")
        if m.strip()
    ]
    tobacco = row.get("tobacco", "unknown").strip().lower() or "unknown"
    health_data = {
        "tobacco": tobacco,
        "conditions": conditions,
        "medications": medications,
    }
    return lead, health_data


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    input_path = pathlib.Path(args[0])
    output_path = pathlib.Path(args[1]) if len(args) > 1 else input_path.with_suffix(".scored.csv")

    rows = list(csv.DictReader(input_path.open()))
    if not rows:
        print("empty input — nothing to score")
        sys.exit(0)

    out_fieldnames = list(rows[0].keys()) + OUTPUT_FIELDS
    results: list[dict] = []

    for row in rows:
        lead, health_data = _parse_row(row)
        try:
            r = triage_from_lead_dict(lead=lead, health_data=health_data, ruleset=RULESET)
            out_row = {
                **row,
                "bucket":           r.bucket,
                "severity_score":   r.severity_score,
                "priority":         r.priority,
                "confidence":       r.confidence,
                "needs_enrichment": r.needs_enrichment,
                "drivers":          " | ".join(r.drivers),
                "dead_reason":      r.dead_reason or "",
                "ruleset_version":  r.ruleset_version,
            }
        except Exception as exc:
            out_row = {**row, "bucket": "ERROR", "severity_score": "", "priority": "",
                       "confidence": "", "needs_enrichment": "", "drivers": str(exc),
                       "dead_reason": "", "ruleset_version": ""}
        results.append(out_row)

    results.sort(key=lambda r: float(r["priority"] or 0), reverse=True)

    with output_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(results)

    buckets: dict[str, int] = {}
    for r in results:
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1

    print(f"scored {len(results)} leads → {output_path}")
    for bucket, count in sorted(buckets.items()):
        print(f"  {bucket}: {count}")


if __name__ == "__main__":
    main()
