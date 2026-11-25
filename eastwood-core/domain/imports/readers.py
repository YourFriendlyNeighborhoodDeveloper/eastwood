"""Streaming readers for CSV and XLSX lead files.

Both yield (row_number, values) with spreadsheet numbering — the header is
row 1, the first data row is row 2 — so error reports point at the row an
operator sees in Excel. Memory stays flat: neither reader loads the file.
"""
from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

csv.field_size_limit(10_000_000)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def detect_format(path: Path) -> str:
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"PK\x03\x04":
        return "xlsx"
    if path.suffix.lower() in (".xls",):
        raise ValueError("Legacy .xls is not supported — save the sheet as .xlsx or .csv")
    return "csv"


def _csv_rows(path: Path) -> tuple[list[str], Iterator[tuple[int, list[Any]]]]:
    f = open(path, "r", encoding="utf-8-sig", errors="replace", newline="")
    sample = f.read(65536)
    f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(f, dialect)
    headers = next(reader, [])

    def gen() -> Iterator[tuple[int, list[Any]]]:
        try:
            for n, row in enumerate(reader, start=2):
                if any(cell.strip() for cell in row):
                    yield n, row
        finally:
            f.close()

    return [h.strip() for h in headers], gen()


def _xlsx_rows(path: Path) -> tuple[list[str], Iterator[tuple[int, list[Any]]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header_row = next(rows, ())
    headers = [str(h).strip() if h is not None else "" for h in header_row]

    def gen() -> Iterator[tuple[int, list[Any]]]:
        try:
            for n, row in enumerate(rows, start=2):
                if any(v not in (None, "") for v in row):
                    yield n, list(row)
        finally:
            wb.close()

    return headers, gen()


def open_rows(path: Path, fmt: str | None = None) -> tuple[str, list[str], Iterator[tuple[int, list[Any]]]]:
    fmt = fmt or detect_format(path)
    if fmt == "xlsx":
        headers, rows = _xlsx_rows(path)
    elif fmt == "csv":
        headers, rows = _csv_rows(path)
    else:
        raise ValueError(f"unsupported format {fmt!r}")
    return fmt, headers, rows
