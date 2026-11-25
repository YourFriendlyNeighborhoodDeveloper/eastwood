"""Turn a carrier's own rate book (PDF, XLSX or CSV) into an importable rate sheet.

Carrier rate books come in two common shapes:

  per-$1,000 tables  Age | Male Non-Tobacco | Male Tobacco | Female Non-Tobacco | Female Tobacco
                     (one table per tier; unisex books have just Non-Tobacco | Tobacco)
  premium grids      Age | $5,000 | $10,000 | $15,000 ...  (one table per sex, tobacco
                     class and tier, named in the title above it)

extract() reads every table, works out which column is which rate class from
the headers and the table's title (or from defaults the operator passes),
and returns the cells plus a review report: which tables were read as what,
the ages covered, and anything suspicious (gaps, rates that fall with age,
premiums that fall with face amount, cells read twice with different values).

What it deliberately does NOT do:
  * fill in a policy fee, modal factor or rounding rule. Those come from the
    rate book's own instructions; the meta template leaves them empty, with
    any lines of the document that mention them as hints.
  * read scanned rate books. OCR misreads digits, and a misread cell that no
    spot check happens to cover would quote someone a wrong premium. A rate
    book without a text layer is refused; ask the carrier for the digital
    version (PDF with text, XLSX or CSV).

`scripts/rates.py verify` remains the gate: spot checks copied from the
carrier's own quote tool must reproduce to the cent, covering every rate class,
before the sheet quotes anyone.
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .rate_sheets import GRID_UNITS, MONTHLY_BASED, RATE_UNITS

_AGE = re.compile(r"^(issue\s*)?age\b", re.I)
_FACE = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.00)?\s*(k)?$", re.I)
_TIERS = (("guaranteed", re.compile(r"\bguaranteed\b|\bg\.?i\.?\b", re.I)),
          ("graded", re.compile(r"\bgraded\b", re.I)),
          ("modified", re.compile(r"\bmodified\b", re.I)),
          ("level", re.compile(r"\blevel\b|\bimmediate\b", re.I)))


class RateBookError(ValueError):
    pass


@dataclass
class Table:
    source: str                 # "page 3" / "sheet Rates"
    title: str                  # text just above the table
    header: list[str]
    rows: list[list[str]]


@dataclass
class Extraction:
    cells: list[dict] = field(default_factory=list)       # age, sex, tobacco, tier, rate[, face]
    tables: list[dict] = field(default_factory=list)      # what each table was read as
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hints: dict = field(default_factory=dict)             # document lines about fees / modes / dates
    faces: list[int] = field(default_factory=list)


# ── classification ────────────────────────────────────────────────────────────

def _sex(text: str) -> str | None:
    t = f" {text.lower()} "
    if re.search(r"\b(female|females|women|woman)\b", t):
        return "F"
    if re.search(r"\b(male|males|men|man)\b", t):
        return "M"
    tokens = re.split(r"[^a-z]+", text.lower())
    if "f" in tokens:
        return "F"
    if "m" in tokens:
        return "M"
    return None


def _tobacco(text: str) -> bool | None:
    t = f" {text.lower()} "
    if re.search(r"non[\s-]*(tobacco|smoker|smoking|nicotine)|\bnonsmoker\b|\bnon-?tob\b", t):
        return False
    if re.search(r"\b(tobacco|smoker|smoking|nicotine|tob)\b", t):
        return True
    return None


def _code(text: str) -> tuple[str | None, bool | None]:
    """Compact class codes: MNT, M-NT, F/T, FNS, MS, NT, T, NS, S."""
    t = re.sub(r"[^a-z]", "", text.lower())
    m = re.fullmatch(r"(m|f)?(nt|ns|n|t|s)", t)
    if not m:
        return None, None
    sex = {"m": "M", "f": "F"}.get(m.group(1) or "")
    return sex, m.group(2) in ("t", "s")


def column_class(text: str) -> tuple[str | None, bool | None]:
    """(sex, tobacco) a per-$1,000 column header states; None where it states nothing."""
    sex, tob = _sex(text), _tobacco(text)
    if tob is None or sex is None:
        for piece in [text, *re.split(r"[\s,;]+", text)]:      # "MNT", or "Male NT" token by token
            csex, ctob = _code(piece)
            sex = sex or csex
            if tob is None and ctob is not None:
                tob = ctob
    return sex, tob


def parse_label(text: str) -> tuple[int | None, str | None, bool | None]:
    """(face, sex, tobacco) a column label states, e.g. "$15,000 Male", "Female NT", "MNT"."""
    face = None
    for tok in re.split(r"\s+", text.strip()):
        face = face or face_of(tok)
    rest = " ".join(t for t in re.split(r"\s+", text.strip()) if face_of(t) is None)
    sex, tob = column_class(rest)
    return face, sex, tob


_LABEL_TOKENS = {"male", "males", "female", "females", "men", "women", "m", "f", "tobacco", "non-tobacco",
                 "nontobacco", "non", "smoker", "non-smoker", "nonsmoker", "nicotine", "tob", "nt", "ns", "t", "s",
                 "-", "/", "&"}


def _is_label_word(text: str) -> bool:
    """A stacked header cell holds only labels ("$15,000", "Male", "Non-Tobacco", "MNT"), never a
    sentence: a title such as "Male Non-Tobacco - Level - Monthly Premiums" is not a header."""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    return bool(tokens) and all(face_of(t) is not None or t.lower() in _LABEL_TOKENS or _code(t)[1] is not None
                                for t in tokens)


def face_of(text: str) -> int | None:
    m = _FACE.match(text.strip())
    if not m:
        return None
    value = int(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
    return value if value >= 1000 else None


def tier_of(text: str) -> str | None:
    for name, rx in _TIERS:
        if rx.search(text or ""):
            return name
    return None


def number(text: str) -> Decimal | None:
    t = (text or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if t in ("", "-", "--", "—", "–", "n/a", "N/A", "NA", "*"):
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return v if v > 0 else None


def _age(text: str) -> int | None:
    t = (text or "").strip()
    return int(t) if t.isdigit() and 0 < int(t) < 120 else None


# ── readers ───────────────────────────────────────────────────────────────────

def _grid_tables(rows: list[list[str]], source: str) -> list[Table]:
    """Tables in a sheet of rows (XLSX / CSV): a header row starting with Age,
    data rows starting with an age, the non-empty rows above as the title."""
    tables, title_rows, i = [], [], 0
    while i < len(rows):
        r = [str(c).strip() if c is not None else "" for c in rows[i]]
        first = next((c for c in r if c), "")
        if _AGE.match(first) and sum(1 for c in r if c) >= 2:
            start = r.index(first)
            header = r[start:]
            above = [str(c).strip() if c is not None else "" for c in rows[i - 1]][start:] if i else []
            if above and not (above[0] or "").strip() and any(a for a in above[1:]) \
                    and all(_is_label_word(a) for a in above[1:] if a):
                filled, last = [], ""
                for a in above[1:]:
                    last = a or last                         # merged cells repeat across their span
                    filled.append(last)
                header = [header[0]] + [f"{a} {h}".strip() for a, h in zip(filled, header[1:])]
                if title_rows:
                    title_rows.pop()
            data = []
            i += 1
            while i < len(rows):
                d = [str(c).strip() if c is not None else "" for c in rows[i]][start:]
                if d and _age(d[0]) is not None:
                    data.append(d + [""] * (len(header) - len(d)))
                    i += 1
                else:
                    break
            tables.append(Table(source, " ".join(title_rows[-2:]), header, data))
            title_rows = []
            continue
        if first:
            title_rows.append(" ".join(c for c in r if c))
        i += 1
    return tables


def _pdf_tables(path: Path) -> tuple[list[Table], str]:
    import pdfplumber

    from domain.imports.pdf_reader import _column_for, _group_words, _text_lines

    tables: list[Table] = []
    text_all = []
    with pdfplumber.open(str(path)) as pdf:
        if not any((p.extract_text() or "").strip() for p in pdf.pages[: min(3, len(pdf.pages))]):
            raise RateBookError("this PDF has no text layer (a scan). OCR is not accurate enough for premiums: "
                                "get the carrier's digital rate book (PDF with text, XLSX or CSV)")
        for n, page in enumerate(pdf.pages, start=1):
            text_all.append(page.extract_text() or "")
            lines = _text_lines(page)
            sizes = sorted(c["size"] for c in page.chars) or [10.0]
            gap = 0.6 * sizes[len(sizes) // 2]
            title_lines: list[list[dict]] = []
            i = 0
            while i < len(lines):
                cells = _group_words(lines[i], gap)
                ages = [k for k, c in enumerate(cells) if _AGE.match(c["text"])]
                if ages and ages[0] == 0 and len(cells) >= 2:
                    bounds = [c["x0"] - gap / 2 for c in cells[1:]]
                    while title_lines and len(ages) == 1:
                        above = _group_words(title_lines[-1], gap)
                        if not above or not all(_is_label_word(w["text"]) for w in above):
                            break
                        for c in cells[1:]:          # each column takes the nearest label above it
                            mid = (c["x0"] + c["x1"]) / 2
                            w = min(above, key=lambda w: abs((w["x0"] + w["x1"]) / 2 - mid))
                            c["text"] = f"{w['text']} {c['text']}"
                        title_lines.pop()
                    data: list[list[str]] = []
                    i += 1
                    while i < len(lines):
                        vals: list[list[str]] = [[] for _ in cells]
                        for w in lines[i]:
                            vals[_column_for(w["x0"], w["x1"], bounds)].append(w["text"])
                        row = [" ".join(v) for v in vals]
                        if _age(row[0]) is None:
                            break
                        data.append(row)
                        i += 1
                    # one header line may hold several side-by-side tables, each starting with Age
                    for k, start in enumerate(ages):
                        end = ages[k + 1] if k + 1 < len(ages) else len(cells)
                        x0 = cells[start]["x0"] - gap
                        x1 = cells[end]["x0"] - gap if end < len(cells) else float("inf")
                        title = " ".join(w["text"] for line in title_lines[-2:] for w in line
                                         if x0 <= w["x0"] < x1)
                        if len(ages) == 1:
                            title = " ".join(w["text"] for line in title_lines[-2:] for w in line)
                        tables.append(Table(f"page {n}", title, [c["text"] for c in cells[start:end]],
                                            [r[start:end] for r in data]))
                    title_lines = []
                    continue
                if lines[i]:
                    title_lines.append(lines[i])
                i += 1
    return tables, "\n".join(text_all)


def read_tables(path: Path) -> tuple[list[Table], str]:
    """Every rate table in the file, and the document's text (for hints)."""
    path = Path(path)
    with open(path, "rb") as f:
        magic = f.read(5)
    if magic == b"%PDF-":
        return _pdf_tables(path)
    if magic[:4] == b"PK\x03\x04":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        tables, text = [], []
        try:
            for ws in wb.worksheets:
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                text.extend(" ".join(str(c) for c in r if c is not None) for r in rows)
                tables.extend(_grid_tables(rows, f"sheet {ws.title}"))
        finally:
            wb.close()
        return tables, "\n".join(text)
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    return _grid_tables(rows, "csv"), "\n".join(",".join(r) for r in rows)


# ── extraction ────────────────────────────────────────────────────────────────

_HINTS = {
    "policy_fee": re.compile(r"(policy|annual|monthly|admin\w*)\s+fee[^\n]{0,80}", re.I),
    "modal": re.compile(r"(modal|mode|monthly|quarterly|semi-?annual|bank\s+draft|PAC|EFT)[^\n]{0,40}"
                        r"(?<![\d.])0?\.\d{3,4}\b[^\n]{0,40}", re.I),
    "effective": re.compile(r"effective[^\n]{0,60}", re.I),
    "states": re.compile(r"(not\s+available|available)\s+in[^\n]{0,80}", re.I),
}


def extract(path: Path, *, unit: str, default_tier: str | None = None, default_sex: str | None = None,
            default_tobacco: bool | None = None) -> Extraction:
    if unit not in RATE_UNITS:
        raise RateBookError(f"--unit must be one of {RATE_UNITS}")
    tables, text = read_tables(path)
    ex = Extraction()
    for name, rx in _HINTS.items():
        found = list(dict.fromkeys(m.group(0).strip() for m in rx.finditer(text)))[:6]
        if found:
            ex.hints[name] = found
    if not tables:
        ex.errors.append("no rate table found (expected a header row starting with 'Age')")
        return ex
    grid_expected = unit in GRID_UNITS
    seen: dict[tuple, Decimal] = {}
    prev = None
    for t_no, t in enumerate(tables, start=1):
        label = f"table {t_no} ({t.source}{': ' + t.title if t.title else ''})"
        cols = t.header[1:]
        labels = [parse_label(c) for c in cols]
        is_grid = any(f is not None for f, _, _ in labels)
        if is_grid != grid_expected:
            ex.errors.append(f"{label}: columns look like {'face amounts' if is_grid else 'rate classes'}, "
                             f"but --unit {unit} expects {'face amounts' if grid_expected else 'rate classes'}")
            continue
        t_sex, t_tob, t_tier = _sex(t.title), _tobacco(t.title), tier_of(t.title)
        if is_grid and t_sex is None and t_tob is None and t_tier is None and prev and prev[0] == t.header:
            t_sex, t_tob, t_tier = prev[1]          # a table continued on the next page, same header
            ex.warnings.append(f"{label}: no class in its title; read as a continuation of the table before it")
        tier = t_tier or (default_tier or "level")
        classes = []
        unresolved = []
        for c, (c_face, c_sex, c_tob) in zip(cols, labels):
            if is_grid and c_face is None:
                if c.strip():
                    ex.warnings.append(f"{label}: column {c!r} has no face amount; ignored")
                classes.append(None)
                continue
            sex = c_sex or t_sex or default_sex
            tob = c_tob if c_tob is not None else (t_tob if t_tob is not None else default_tobacco)
            if tob is None or (is_grid and sex is None and default_sex is None):
                unresolved.append(c)
            if tob is None:
                classes.append(None)
                continue
            classes.append((sex or "U", tob, c_face or 0))   # no sex stated anywhere: a unisex rate
        if is_grid and unresolved:
            ex.errors.append(f"{label}: cannot tell the class of column(s) {', '.join(map(repr, unresolved))} "
                             f"from the headers or the title {t.title!r}; pass --sex/--tobacco or split the file")
            continue
        if not is_grid:
            for c in unresolved:
                if c.strip():
                    ex.warnings.append(f"{label}: column {c!r} states no tobacco class; ignored")
        if is_grid:
            prev = (t.header, (t_sex, t_tob, t_tier))
        read = 0
        for row in t.rows:
            age = _age(row[0])
            for col, cls in zip(row[1:], classes):
                if cls is None:
                    continue
                rate = number(col)
                if rate is None:
                    if col.strip() and col.strip() not in ("-", "--", "—", "–", "N/A", "n/a", "NA", "*"):
                        ex.warnings.append(f"{label}: age {age}: unreadable cell {col!r}")
                    continue
                sex, tob_class, face = cls
                for tob in ((False, True) if tob_class == "any" else (tob_class,)):
                    key = (age, sex, tob, tier, face)
                    if key in seen:
                        if seen[key] != rate:
                            ex.errors.append(f"{label}: age {age} {sex} {'T' if tob else 'NT'} {tier}"
                                             f"{f' ${face:,}' if face else ''} read twice: {seen[key]} and {rate}")
                        continue
                    seen[key] = rate
                    ex.cells.append({"age": age, "sex": sex, "tobacco": tob, "tier": tier, "rate": rate, "face": face})
                    read += 1
        ex.tables.append({"table": t_no, "source": t.source, "title": t.title, "tier": tier,
                          "layout": "grid" if is_grid else "per_1000",
                          "columns": [f"{c} -> {_col_label(k)}" for c, k in zip(cols, classes)],
                          "ages": [min(_age(r[0]) for r in t.rows), max(_age(r[0]) for r in t.rows)] if t.rows else None,
                          "cells": read})
    _sanity(ex)
    ex.faces = sorted({c["face"] for c in ex.cells if c["face"]})
    return ex


def _col_label(k) -> str:
    if not k:
        return "ignored"
    sex, tob, face = k
    tob_text = "tobacco and non-tobacco (same rate)" if tob == "any" else ("tobacco" if tob else "non-tobacco")
    return f"{sex} {tob_text}" + (f" ${face:,}" if face else "")


def _sanity(ex: Extraction) -> None:
    series = defaultdict(list)
    for c in ex.cells:
        series[(c["sex"], c["tobacco"], c["tier"], c["face"])].append((c["age"], c["rate"]))
    for (sex, tob, tier, face), pts in series.items():
        pts.sort()
        name = f"{sex} {'T' if tob else 'NT'} {tier}{f' ${face:,}' if face else ''}"
        ages = [a for a, _ in pts]
        gaps = sorted(set(range(ages[0], ages[-1] + 1)) - set(ages))
        if gaps:
            ex.warnings.append(f"{name}: no rate for ages {_ranges(gaps)} (those ages will not be quoted)")
        falls = [a2 for (a1, r1), (a2, r2) in zip(pts, pts[1:]) if r2 < r1]
        if falls:
            ex.warnings.append(f"{name}: rate falls with age at {_ranges(falls)} — check the extraction there")
    by_age = defaultdict(list)
    for c in ex.cells:
        if c["face"]:
            by_age[(c["age"], c["sex"], c["tobacco"], c["tier"])].append((c["face"], c["rate"]))
    for (age, sex, tob, tier), pts in by_age.items():
        pts.sort()
        if any(r2 < r1 for (_, r1), (_, r2) in zip(pts, pts[1:])):
            ex.warnings.append(f"age {age} {sex} {'T' if tob else 'NT'} {tier}: premium falls as the face amount rises")


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


# ── outputs ───────────────────────────────────────────────────────────────────

def write_rates_csv(ex: Extraction, out: Path, unit: str) -> Path:
    grid = unit in GRID_UNITS
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["age", "sex", "tobacco", "tier", "rate"] + (["face"] if grid else []))
        for c in sorted(ex.cells, key=lambda c: (c["tier"], c["sex"], c["tobacco"], c["face"], c["age"])):
            w.writerow([c["age"], c["sex"], "Y" if c["tobacco"] else "N", c["tier"], c["rate"]]
                       + ([c["face"]] if grid else []))
    return out


def meta_template(ex: Extraction, source: Path, unit: str) -> dict:
    """Everything the rate book must supply stays empty; only what the unit
    defines (a monthly-based figure has monthly factor 1) or the table itself
    shows (its face amounts and tiers) is filled in."""
    sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()[:16]
    tiers = list(dict.fromkeys(c["tier"] for c in ex.cells)) or ["level"]
    meta = {
        "carrier": None, "product_line": None, "plan_name": None, "states": [],
        "effective_date": None, "expires_date": None, "rate_unit": unit,
        "policy_fee_annual": None if unit == "per_1000_annual" else 0,
        "policy_fee_monthly": None if unit == "per_1000_monthly" else 0,
        "modal_factors": {"monthly": "1" if unit in MONTHLY_BASED else None},
        "rounding": "final",
        "min_face": None, "max_face": None, "face_increment": None,
        "default_tier": "level" if "level" in tiers else tiers[0],
        "tier_questions": [],
        "source_document": f"{Path(source).name} (sha256 {sha})",
        "is_test": False,
        "_fill_in": "Every null comes from the rate book's own instructions (policy fee, modal factors, "
                    "rounding, face limits, states, dates, the questions that place a prospect in each tier). "
                    "Remove this key and _hints before importing.",
        "_hints": ex.hints,
    }
    if ex.faces:
        steps = [b - a for a, b in zip(ex.faces, ex.faces[1:])]
        meta.update(min_face=ex.faces[0], max_face=ex.faces[-1],
                    face_increment=math.gcd(*steps) if steps else ex.faces[0])
    if len(tiers) > 1:
        meta["tier_questions"] = [{"key": f"{t}_question", "text": None, "yes_tier": t}
                                  for t in tiers if t != meta["default_tier"]]
    return meta


def report_text(ex: Extraction) -> str:
    lines = [f"cells read: {len(ex.cells)}"]
    for t in ex.tables:
        lines.append(f"  table {t['table']} [{t['source']}] {t['layout']} tier={t['tier']} ages={t['ages']} "
                     f"cells={t['cells']} title={t['title']!r}")
        lines.extend(f"      {c}" for c in t["columns"])
    lines += [f"ERROR: {e}" for e in ex.errors] + [f"warning: {w}" for w in ex.warnings]
    if ex.hints:
        lines.append("lines in the document that may hold the fee / modal factors / dates / states:")
        for k, v in ex.hints.items():
            lines.extend(f"  {k}: {x}" for x in v)
    return "\n".join(lines)


def spot_check_cases(cells: list[dict], unit: str, *, faces: list[int] | None = None,
                     min_face: int = 5000, max_face: int = 25000) -> list[dict]:
    """A spread of cases for the operator to price in the carrier's own quote
    tool: every rate class at a young, a middle and an old age, every tier,
    small and large face amounts. `expected` is left for the operator."""
    if not cells:
        return []
    out, used = [], set()
    classes = sorted({(c["sex"], c["tobacco"], c["tier"]) for c in cells})
    for k, (sex, tob, tier) in enumerate(classes):
        ages = sorted({c["age"] for c in cells if (c["sex"], c["tobacco"], c["tier"]) == (sex, tob, tier)})
        pick = [ages[0], ages[len(ages) // 2], ages[-1]] if len(ages) >= 3 else ages
        for j, age in enumerate(pick):
            if unit in GRID_UNITS:
                fs = sorted({c["face"] for c in cells if (c["sex"], c["tobacco"], c["tier"], c["age"]) == (sex, tob, tier, age)})
                face = fs[(j + k) % len(fs)] if fs else None
            else:
                span = [min_face, (min_face + max_face) // 2 // 1000 * 1000, max_face]
                face = span[(j + k) % 3]
            key = (age, sex, tob, tier, face)
            if face is None or key in used:
                continue
            used.add(key)
            out.append({"age": age, "sex": sex if sex != "U" else "F", "tobacco": "Y" if tob else "N", "tier": tier,
                        "face": face, "mode": "monthly", "expected": ""})
    return out
