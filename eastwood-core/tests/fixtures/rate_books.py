"""SYNTHETIC carrier rate books in the shapes real ones come in, generated from
the synthetic test sheet's formula so extraction can be checked against truth.
Not carrier data."""
from __future__ import annotations

from pathlib import Path

from tests.fixtures.pdfs import write_text_pdf
from tests.fixtures.rate_sheets.make_test_sheet import monthly, rate

AGES = list(range(50, 86))
CLASSES = [("M", False), ("M", True), ("F", False), ("F", True)]
NAMES = {("M", False): "Male Non-Tobacco", ("M", True): "Male Tobacco",
         ("F", False): "Female Non-Tobacco", ("F", True): "Female Tobacco"}
FACES = [5000, 10000, 15000, 20000, 25000]


def per_1000_pdf(path: Path) -> Path:
    """Annual rates per $1,000, one page per tier, the fee and modal factor in the footnote."""
    col_x = [36, 110, 230, 340, 480]
    pages = []
    for tier in ("level", "graded"):
        rows = [[f"Synthetic Test Life - {tier.title()} Benefit"], ["Annual Rates per $1,000 of Face Amount"],
                ["Age", *[NAMES[c] for c in CLASSES]]]
        rows += [[str(a), *[f"{rate(a, s, t, tier):.2f}" for s, t in CLASSES]] for a in AGES]
        rows += [["Annual policy fee: $36.00. Monthly bank draft modal factor: .0875"]]
        pages.append(rows)
    return write_text_pdf(path, pages, col_x, font_size=9, top=590, line_gap=14.5, size=(612, 792 + 20))


def two_line_header_pdf(path: Path) -> Path:
    """'Male' / 'Female' centred over their two columns, NT / T below."""
    col_x = [36, 120, 220, 320, 420]
    head = [(165, "Male"), (360, "Female")]
    rows = [["Level Benefit Rates per $1,000"], head, ["Age", "Non-Tobacco", "Tobacco", "Non-Tobacco", "Tobacco"]]
    rows += [[str(a), *[f"{rate(a, s, t, 'level'):.2f}" for s, t in CLASSES]] for a in AGES]
    return write_text_pdf(path, [rows], col_x, font_size=9, top=590, line_gap=14.5, size=(612, 812))


def grid_pdf(path: Path, *, continuation: bool = True) -> Path:
    """Monthly premiums by face amount, one table per class; each table runs
    over two pages and (optionally) the second page repeats only the header."""
    col_x = [36, 100, 170, 240, 310, 380]
    head = ["Age", *[f"${f:,}" for f in FACES]]
    pages = []
    for tier in ("level", "graded"):
        for s, t in CLASSES:
            title = f"{NAMES[(s, t)]} - {tier.title()} - Monthly Premiums"
            body = [[str(a), *[f"{monthly(a, s, t, tier, f):.2f}" for f in FACES]] for a in AGES]
            pages.append([[title], head, *body[:18]])
            pages.append(([] if continuation else [[title]]) + [head, *body[18:]])
    return write_text_pdf(path, pages, col_x, font_size=9, top=740, line_gap=14.5, size=(612, 792))


def per_1000_xlsx(path: Path) -> Path:
    """Excel rate file: merged 'Male' / 'Female' cells above NT / T."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Level"
    ws.append(["Synthetic Test Life - Level Benefit (annual per $1,000)"])
    ws.append([None, "Male", None, "Female", None])
    ws.merge_cells("B2:C2")
    ws.merge_cells("D2:E2")
    ws.append(["Age", "NT", "T", "NT", "T"])
    for a in AGES:
        ws.append([a, *[float(rate(a, s, t, "level")) for s, t in CLASSES]])
    wb.save(path)
    return path


def grid_csv(path: Path, sex: str = "F", tobacco: bool = False, tier: str = "level") -> Path:
    lines = [f"{NAMES[(sex, tobacco)]} {tier.title()} monthly premium", "Age," + ",".join(f"{f}" for f in FACES)]
    lines += [f"{a}," + ",".join(f"{monthly(a, sex, tobacco, tier, f):.2f}" for f in FACES) for a in AGES]
    path.write_text("\n".join(lines) + "\n")
    return path


STACKED_FACES = [15000, 10000, 7000, 5000, 3000]


def stacked_header_pdf(path: Path) -> Path:
    """A guaranteed-issue chart shaped like real carrier charts: face amounts on
    one header line, MALE / FEMALE under each, '$' on every premium, no tobacco
    split, and the table continued on a second page."""
    col_x = [36] + [90 + 52 * k for k in range(10)]
    faces_line = [(90 + 52 * 2 * i + 18, f"${f:,}") for i, f in enumerate(STACKED_FACES)]
    sexes = ["AGE"] + ["MALE", "FEMALE"] * 5

    def row(a):
        return [str(a)] + [f"${monthly(a, s, False, 'level', f):.2f}" for f in STACKED_FACES for s in ("M", "F")]
    p1 = [["GUARANTEED WHOLE LIFE (SYNTHETIC)"], ["Rates shown below are effective August 1, 2026."],
          ["Monthly Premium"], faces_line, sexes, *[row(a) for a in AGES[:18]]]
    p2 = [["Monthly Premium"], faces_line, sexes, *[row(a) for a in AGES[18:]]]
    return write_text_pdf(path, [p1, p2], col_x, font_size=8, top=740, line_gap=13, size=(612, 792))


def stacked_header_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Guaranteed Whole Life (synthetic) - monthly premium"])
    faces_row = [None]
    for f in STACKED_FACES:
        faces_row += [f, None]
    ws.append(faces_row)
    for i in range(len(STACKED_FACES)):
        col = 2 + 2 * i
        ws.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col + 1)
    ws.append(["Age"] + ["Male", "Female"] * 5)
    for a in AGES:
        ws.append([a] + [float(monthly(a, s, False, "level", f)) for f in STACKED_FACES for s in ("M", "F")])
    wb.save(path)
    return path
