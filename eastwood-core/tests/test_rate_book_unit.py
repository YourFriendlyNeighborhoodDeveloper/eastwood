"""Carrier rate books (PDF / XLSX / CSV) -> importable rate sheets, checked
against the synthetic formula they were generated from. Plus the new rate
units and their validation."""
from __future__ import annotations

from decimal import Decimal

import pytest

from domain.quoting import rate_book as rb
from domain.quoting.rate_sheets import RateSheetError, _validate_meta, compute_premium, read_rate_rows
from tests.fixtures import rate_books as books
from tests.fixtures.rate_sheets.make_test_sheet import monthly, rate


def _truth(c):
    return monthly(c["age"], c["sex"], c["tobacco"], c["tier"], c["face"]) if c["face"] else \
        rate(c["age"], c["sex"], c["tobacco"], c["tier"])


def _exact(ex):
    return [c for c in ex.cells if c["rate"] != _truth(c)]


def test_per_1000_pdf_two_tiers_reads_every_cell_exactly(tmp_path):
    ex = rb.extract(books.per_1000_pdf(tmp_path / "b.pdf"), unit="per_1000_annual")
    assert not ex.errors and _exact(ex) == []
    assert len(ex.cells) == 36 * 4 * 2 and {t["tier"] for t in ex.tables} == {"level", "graded"}
    assert any("36.00" in h for h in ex.hints["policy_fee"]) and any(".0875" in h for h in ex.hints["modal"])


def test_two_line_centred_header_assigns_each_column_its_class(tmp_path):
    ex = rb.extract(books.two_line_header_pdf(tmp_path / "b.pdf"), unit="per_1000_annual")
    assert not ex.errors and _exact(ex) == [] and len(ex.cells) == 36 * 4
    assert {(c["sex"], c["tobacco"]) for c in ex.cells} == {("M", False), ("M", True), ("F", False), ("F", True)}


def test_premium_grid_with_continued_tables(tmp_path):
    ex = rb.extract(books.grid_pdf(tmp_path / "b.pdf"), unit="flat_monthly")
    assert not ex.errors and _exact(ex) == []
    assert len(ex.cells) == 36 * 5 * 4 * 2 and ex.faces == books.FACES
    assert sum("continuation" in w for w in ex.warnings) == 8


def test_xlsx_with_merged_class_cells(tmp_path):
    ex = rb.extract(books.per_1000_xlsx(tmp_path / "b.xlsx"), unit="per_1000_annual")
    assert not ex.errors and not ex.warnings and _exact(ex) == [] and len(ex.cells) == 36 * 4


def test_csv_grid_takes_its_class_from_the_title(tmp_path):
    ex = rb.extract(books.grid_csv(tmp_path / "b.csv", sex="M", tobacco=True, tier="graded"), unit="flat_monthly")
    assert not ex.errors and _exact(ex) == []
    assert {(c["sex"], c["tobacco"], c["tier"]) for c in ex.cells} == {("M", True, "graded")}


@pytest.mark.parametrize("make", ["stacked_header_pdf", "stacked_header_xlsx"])
def test_stacked_face_and_sex_headers_with_no_tobacco_split(tmp_path, make):
    """Real carrier charts put face amounts on one header line and MALE / FEMALE
    under each; a guaranteed-issue book has one rate for tobacco and non-tobacco."""
    src = getattr(books, make)(tmp_path / ("b.pdf" if make.endswith("pdf") else "b.xlsx"))
    ex = rb.extract(src, unit="flat_monthly", default_tier="guaranteed", default_tobacco="any")
    assert not ex.errors and len(ex.cells) == 36 * 10 * 2 and ex.faces == sorted(books.STACKED_FACES)
    truth = {(c["age"], c["sex"], c["face"]): monthly(c["age"], c["sex"], False, "level", c["face"]) for c in ex.cells}
    assert all(c["rate"] == truth[(c["age"], c["sex"], c["face"])] for c in ex.cells)
    assert {c["tier"] for c in ex.cells} == {"guaranteed"} and {c["tobacco"] for c in ex.cells} == {False, True}


def test_without_a_tobacco_class_a_grid_is_refused_not_guessed(tmp_path):
    ex = rb.extract(books.stacked_header_pdf(tmp_path / "b.pdf"), unit="flat_monthly")
    assert ex.errors and "cannot tell the class" in ex.errors[0] and not ex.cells


def test_a_scanned_rate_book_is_refused(tmp_path):
    from tests.fixtures.pdfs import write_scanned_pdf

    scan = write_scanned_pdf(books.two_line_header_pdf(tmp_path / "b.pdf"), tmp_path / "scan.pdf", dpi=100)
    with pytest.raises(rb.RateBookError, match="no text layer"):
        rb.extract(scan, unit="per_1000_annual")


def test_the_wrong_unit_is_caught(tmp_path):
    ex = rb.extract(books.grid_csv(tmp_path / "b.csv"), unit="per_1000_annual")
    assert ex.errors and "face amounts" in ex.errors[0] and not ex.cells


def test_suspicious_tables_are_reported(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text("Level Rates\nAge,Male NT,Female NT\n60,20.00,18.00\n61,21.00,19.00\n63,20.50,19.50\n64,N/A,20.00\n"
                 "65,23.00,abc\n\nLevel Rates\nAge,Male NT,Female NT\n60,20.10,18.00\n")
    ex = rb.extract(p, unit="per_1000_annual")
    text = rb.report_text(ex)
    assert "no rate for ages 62" in text                      # a gap
    assert "falls with age at 63" in text                     # 21.00 -> 20.50
    assert "unreadable cell 'abc'" in text
    assert any("read twice: 20.00 and 20.10" in e for e in ex.errors)


def test_meta_template_fills_only_what_the_book_or_unit_defines(tmp_path):
    src = books.grid_pdf(tmp_path / "b.pdf")
    meta = rb.meta_template(rb.extract(src, unit="flat_monthly"), src, "flat_monthly")
    assert meta["modal_factors"] == {"monthly": "1"} and meta["policy_fee_annual"] == 0
    assert (meta["min_face"], meta["max_face"], meta["face_increment"]) == (5000, 25000, 5000)
    assert meta["carrier"] is None and meta["states"] == [] and meta["effective_date"] is None
    assert meta["tier_questions"] == [{"key": "graded_question", "text": None, "yes_tier": "graded"}]
    assert "b.pdf (sha256 " in meta["source_document"]
    src2 = books.per_1000_pdf(tmp_path / "c.pdf")
    meta2 = rb.meta_template(rb.extract(src2, unit="per_1000_annual"), src2, "per_1000_annual")
    assert meta2["policy_fee_annual"] is None and meta2["modal_factors"] == {"monthly": None}   # from the book only


def test_rates_csv_round_trips_into_the_importer(tmp_path):
    src = books.grid_pdf(tmp_path / "b.pdf")
    ex = rb.extract(src, unit="flat_monthly")
    rows = read_rate_rows(rb.write_rates_csv(ex, tmp_path / "rates.csv", "flat_monthly"), "flat_monthly")
    assert len(rows) == len(ex.cells) and all(r["face"] in books.FACES for r in rows)


def test_spot_check_cases_cover_every_rate_class(tmp_path):
    ex = rb.extract(books.per_1000_pdf(tmp_path / "b.pdf"), unit="per_1000_annual")
    cases = rb.spot_check_cases(ex.cells, "per_1000_annual")
    assert {(c["sex"], c["tobacco"], c["tier"]) for c in cases} == \
        {(s, "Y" if t else "N", tier) for s, t in books.CLASSES for tier in ("level", "graded")}
    assert {c["age"] for c in cases} >= {50, 85} and all(c["expected"] == "" for c in cases)
    gx = rb.extract(books.grid_pdf(tmp_path / "g.pdf"), unit="flat_monthly")
    assert all(c["face"] in books.FACES for c in rb.spot_check_cases(gx.cells, "flat_monthly"))


# ── rate units ────────────────────────────────────────────────────────────────

def test_monthly_per_1000_and_annual_grid_premiums():
    # monthly rate 2.11 per $1,000, $3.15 monthly fee, $10,000: 21.10 + 3.15
    assert compute_premium(rate_unit="per_1000_monthly", rate=Decimal("2.11"), face=10000,
                           policy_fee_annual=Decimal(0), policy_fee_monthly=Decimal("3.15"),
                           modal_factor=Decimal(1)) == Decimal("24.25")
    # annual grid premium x monthly factor
    assert compute_premium(rate_unit="flat_annual", rate=Decimal("600.00"), face=10000,
                           policy_fee_annual=Decimal(0), modal_factor=Decimal("0.0875")) == Decimal("52.50")


def _meta(**kw):
    m = {"carrier": "C", "product_line": "final_expense", "plan_name": "P", "states": ["MI"],
         "effective_date": "2026-01-01", "rate_unit": "per_1000_annual", "modal_factors": {"monthly": "0.0875"},
         "min_face": 2000, "max_face": 40000, "source_document": "Carrier rate book form 123"}
    m.update(kw)
    return m


@pytest.mark.parametrize("kw, match", [
    ({"rate_unit": "flat_monthly", "modal_factors": {"monthly": "0.5"}}, "must be 1"),
    ({"rate_unit": "per_1000_monthly", "modal_factors": {"monthly": "0.0875"}}, "must be 1"),
    ({"rate_unit": "flat_monthly", "modal_factors": {"monthly": "1"}, "policy_fee_annual": "36"}, "already includes"),
    ({"policy_fee_monthly": "3"}, "annual policy fee"),
    ({"rate_unit": "per_1000_monthly", "modal_factors": {"monthly": "1"}, "policy_fee_annual": "36"}, "monthly policy fee"),
    ({"rate_unit": "flat_annual", "modal_factors": {"annual": "1"}}, "monthly"),
])
def test_rate_unit_rules(kw, match):
    with pytest.raises(RateSheetError, match=match):
        _validate_meta(_meta(**kw))


def test_per_1000_rows_take_no_face_column(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("age,sex,tobacco,tier,rate,face\n65,F,N,level,24.00,10000\n")
    with pytest.raises(RateSheetError, match="no face column"):
        read_rate_rows(p, "per_1000_annual")
