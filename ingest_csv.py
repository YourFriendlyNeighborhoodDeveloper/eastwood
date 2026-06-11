#!/usr/bin/env python3
"""
Ingest leads from a CSV file into Eastwood.

Usage:
  # Vendor CSV with explicit opt-in column:
  python ingest_csv.py leads.csv

  # Vendor CSV without opt-in column — operator attests to consent source:
  python ingest_csv.py leads.csv --assume-consent-source integrity_import

  # Override which column holds the opt-in value:
  python ingest_csv.py leads.csv --consent-column "TCPA Consent"
"""

import argparse
import csv
from datetime import datetime, timezone

import requests

API_URL = "http://localhost:18080/api/leads/ingest"

# Columns checked (in order) for opt-in/consent value
_CONSENT_COLUMNS = [
    "consent", "opt_in", "optin", "tcpa_consent", "tcpa", "express_consent",
    "Consent", "Opt In", "TCPA Consent", "Express Consent",
]

# Columns checked (in order) for a consent/opt-in timestamp
_CONSENT_TS_COLUMNS = [
    "consent_timestamp", "opt_in_date", "consent_date", "tcpa_timestamp",
    "Consent Date", "Opt In Date", "TCPA Date",
]

# Phone columns tried in priority order
_PHONE_COLUMNS = ["Other Phone 1", "Mobile", "Home", "Work"]


def extract_phone(row: dict) -> str | None:
    for key in _PHONE_COLUMNS:
        val = row.get(key, "").strip()
        if val:
            return val if val.startswith("+") else f"+1{val}"
    return None


def extract_consent(row: dict, consent_column: str | None) -> tuple[bool, str | None]:
    """Return (has_consent, raw_value)."""
    cols = [consent_column] if consent_column else _CONSENT_COLUMNS
    for col in cols:
        raw = row.get(col, "").strip()
        if raw:
            lowered = raw.lower()
            consented = lowered in {"1", "true", "yes", "y", "opted in", "opt in"}
            return consented, raw
    return False, None


def extract_consent_timestamp(row: dict) -> str | None:
    for col in _CONSENT_TS_COLUMNS:
        val = row.get(col, "").strip()
        if val:
            return val
    return None


def ingest_csv(
    csv_path: str,
    assume_consent_source: str | None,
    consent_column: str | None,
) -> None:
    import_ts = datetime.now(timezone.utc).isoformat()

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        total = 0
        accepted = 0
        failed = 0
        skipped = 0
        consented_count = 0

        for row in reader:
            first_name = row.get("First Name", "").strip()
            last_name  = row.get("Last Name",  "").strip()
            age_str    = row.get("Age",         "").strip()
            state      = row.get("State",       "").strip()
            email      = row.get("Email",       "").strip()
            phone      = extract_phone(row)

            if not (phone and state):
                print(f"  ⊘  skipping row — missing phone or state")
                skipped += 1
                continue

            try:
                age = int(age_str) if age_str else None
            except ValueError:
                age = None

            lead: dict = {
                "phone":  phone,
                "state":  state,
                "source": f"csv_import:{assume_consent_source or 'unknown'}",
            }
            if first_name: lead["first_name"] = first_name
            if last_name:  lead["last_name"]  = last_name
            if age:        lead["age"]         = age
            if email:      lead["email"]       = email

            # ── Consent resolution ─────────────────────────────────────────
            # Priority: explicit column in CSV > --assume-consent-source flag
            csv_consented, _ = extract_consent(row, consent_column)
            consent_ts = extract_consent_timestamp(row) or import_ts

            if csv_consented:
                # Explicit opt-in value found in the CSV
                lead["express_written_consent"] = True
                lead["consent_source"]          = assume_consent_source or "csv_column"
                lead["consent_timestamp"]       = consent_ts
                lead["consent_raw_row"]         = dict(row)
                consented_count += 1
            elif assume_consent_source:
                # No column found but operator attested at the command line
                lead["express_written_consent"] = True
                lead["consent_source"]          = assume_consent_source
                lead["consent_timestamp"]       = consent_ts
                lead["consent_raw_row"]         = dict(row)
                consented_count += 1
            # else: no consent — lead ingested without consent flag

            try:
                resp = requests.post(API_URL, json=lead, timeout=10)
                # API returns 202 on success
                if resp.status_code in (200, 202):
                    data = resp.json()
                    if data.get("status") == "accepted":
                        accepted += 1
                        consent_tag = " [consent]" if lead.get("express_written_consent") else ""
                        print(f"  ✓  {first_name} {last_name} ({phone}){consent_tag}")
                    else:
                        failed += 1
                        print(f"  ✗  {first_name} {last_name}: {data.get('error', 'rejected')}")
                else:
                    failed += 1
                    try:
                        detail = resp.json().get("detail", resp.text[:80])
                    except Exception:
                        detail = resp.text[:80]
                    print(f"  ✗  {first_name} {last_name}: HTTP {resp.status_code} — {detail}")
            except Exception as e:
                failed += 1
                print(f"  ✗  {first_name} {last_name}: {e}")

            total += 1

    print(f"\n{'─'*52}")
    print(f"  Total   {total:>6}")
    print(f"  Accepted{accepted:>6}  ({consented_count} with consent record)")
    print(f"  Skipped {skipped:>6}  (missing phone / state)")
    print(f"  Failed  {failed:>6}")
    print(f"{'─'*52}")
    if not assume_consent_source and consented_count == 0:
        print(
            "\n  NOTE: no consent records were created.\n"
            "  If these leads carry vendor opt-in, re-run with:\n"
            "    --assume-consent-source <vendor_name>\n"
            "  e.g.  --assume-consent-source integrity_import\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest leads from a CSV file into Eastwood.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("csv_path", help="Path to the CSV file")
    parser.add_argument(
        "--assume-consent-source",
        metavar="SOURCE",
        default=None,
        help=(
            "Operator attestation: treat every row as consented and tag the consent "
            "record with this source name (e.g. 'integrity_import'). "
            "If the CSV also has an explicit opt-in column, the column value takes "
            "precedence and this label is used as the source name."
        ),
    )
    parser.add_argument(
        "--consent-column",
        metavar="COLUMN",
        default=None,
        help="Name of the CSV column that holds the opt-in/consent flag.",
    )
    args = parser.parse_args()

    print(f"Eastwood CSV Importer")
    print(f"File   : {args.csv_path}")
    print(f"Consent: {args.assume_consent_source or '(from CSV column only)'}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    ingest_csv(
        csv_path=args.csv_path,
        assume_consent_source=args.assume_consent_source,
        consent_column=args.consent_column,
    )
