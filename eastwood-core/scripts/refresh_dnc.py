#!/usr/bin/env python3
"""
Refresh the National DNC Registry dataset.

Downloads the registered numbers for every subscribed area code using the
official FTC automated-download interface, validates and checksums each file,
and atomically replaces the active dataset. On any failure the previous valid
dataset is left untouched.

Run on a schedule more often than DNC_MAX_DATA_AGE_DAYS (default 31), or a
dataset will age out and stop clearing numbers.

Usage:
  python scripts/refresh_dnc.py

Configuration (environment):
  DNC_SAN, DNC_REGISTRY_USERNAME, DNC_REGISTRY_PASSWORD
  DNC_AREA_CODES               e.g. "313,248,586"
  DNC_REGISTRY_DOWNLOAD_URL    documented FTC automated-download endpoint
  DNC_DATA_DIRECTORY           default data/private/dnc_registry
"""
from __future__ import annotations

import asyncio
import sys

# Make the project importable when run as a script.
import pathlib as _pathlib

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from domain.compliance import refresh_from_settings  # noqa: E402


async def _main() -> int:
    settings = get_settings()
    print("Refreshing National DNC Registry dataset…")
    print(f"  area codes : {settings.dnc_area_codes or '(none configured)'}")
    print(f"  data dir   : {settings.dnc_data_directory}")

    try:
        result = await refresh_from_settings(settings)
    except ValueError as exc:
        # Misconfiguration (no SAN / no download URL) — never a stack trace of secrets.
        print(f"  refused: {exc}")
        return 2

    if result.ok:
        print(
            f"  ok: {len(result.area_codes)} area codes, {result.total_numbers} numbers, "
            f"downloaded_at={result.downloaded_at.isoformat() if result.downloaded_at else '-'}"
        )
        return 0

    print(f"  failed: {result.error}")
    print("  the previous valid dataset (if any) was preserved.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
