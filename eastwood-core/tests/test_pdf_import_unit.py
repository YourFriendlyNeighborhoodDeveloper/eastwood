"""PDF lead lists (text layer, ruled tables, scanned pages via OCR) and the
upload preview. OCR tests need the tesseract binary, which the image installs;
they skip on machines without it."""
from __future__ import annotations

import shutil

import pytest

from domain.imports.pdf_reader import _rows_from_lines, extract_pdf
from domain.imports.preview import preview_file
from domain.imports.readers import OCR_CONFIDENCE_COLUMN, detect_format, open_rows
from tests.fixtures.pdfs import COL_X, HEADERS, lead_rows, write_scanned_pdf, write_text_pdf

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")


def _two_page_list(tmp_path):
    return write_text_pdf(tmp_path / "list.pdf",
                          [[["Lead list - November"], HEADERS, *lead_rows(20)],
                           [HEADERS, *lead_rows(5, start=21), ["Page 2 of 2"]]], COL_X)


def test_text_pdf_reads_every_column_across_pages(tmp_path):
    ex = extract_pdf(_two_page_list(tmp_path))
    assert ex.method == "pdf_text" and ex.pages == 2
    assert ex.headers == HEADERS
    assert ex.rows == lead_rows(25)          # title, repeated header and footer skipped
    assert ex.ocr_confidence == []


def test_ruled_table_pdf_uses_the_table_structure(tmp_path):
    ex = extract_pdf(write_text_pdf(tmp_path / "t.pdf", [[HEADERS, *lead_rows(10)]], COL_X, grid=True))
    assert ex.method == "pdf_table"
    assert ex.headers == HEADERS and ex.rows == lead_rows(10)


def test_tight_columns_never_merge(tmp_path):
    tight = [36, 87, 138, 206, 234, 286, 350, 387]   # ~7 pt (under one character) between columns
    ex = extract_pdf(write_text_pdf(tmp_path / "tight.pdf", [[HEADERS, *lead_rows(8)]], tight, font_size=9))
    assert ex.headers == HEADERS and ex.rows == lead_rows(8)


def test_words_go_to_the_column_they_sit_under():
    def w(text, x0, x1):
        return {"text": text, "x0": x0, "x1": x1}

    header = [w("Name", 0, 30), w("Product", 100, 140), w("Age", 200, 215), w("Phone", 260, 290)]
    rows = [
        # left-aligned value much wider than its header; right-aligned age wider than "Age"
        [w("Pat", 0, 18), w("Final", 100, 125), w("Expense", 128, 170), w("Plus", 173, 192),
         w("102", 194, 215), w("3135550101", 260, 320)],
        # an empty middle column
        [w("Lee", 0, 18), w("67", 205, 215), w("3135550102", 260, 320)],
    ]
    headers, out, confs = _rows_from_lines([header, *rows], gap=6)
    assert headers == ["Name", "Product", "Age", "Phone"]
    assert out == [["Pat", "Final Expense Plus", "102", "3135550101"], ["Lee", "", "67", "3135550102"]]


def test_pdf_rows_are_numbered_like_a_spreadsheet(tmp_path):
    p = _two_page_list(tmp_path)
    assert detect_format(p) == "pdf"
    fmt, headers, rows = open_rows(p)
    first = next(rows)
    assert fmt == "pdf" and headers == HEADERS and first == (2, lead_rows(1)[0])


def test_preview_maps_columns_flags_problems_and_takes_overrides(tmp_path):
    p = tmp_path / "vendor.csv"
    p.write_text(
        "Lead ID,First Name,Last Name,Cell Phone,St,Birthdate,Prodct,Gender,Smoker,Notes\n"
        "A1,Pat,One,(313) 555-0101,Michigan,1955-03-04,Final Expense,F,no,call after 5\n"
        "A2,Lee,Two,12345,MI,1950-01-01,Final Expense,M,yes,\n"
        "A3,Kim,Three,313-555-0101,MI,1951-01-01,Life,F,no,\n"
        "A4,Ray,Four,3135550104,Ontario,1952-01-01,Final Expense,M,no,\n")
    pv = preview_file(p)
    assert pv["format"] == "csv" and pv["rows_scanned"] == 4 and pv["phone_column_found"]
    assert pv["mapping"]["Cell Phone"] == "phone" and pv["mapping"]["St"] == "state"
    assert pv["mapping"]["Gender"] == "sex" and pv["mapping"]["Smoker"] == "tobacco"
    assert pv["mapping"]["Prodct"] is None and "product_line" in pv["suggestions"]["Prodct"]
    assert "Notes" not in pv["suggestions"]
    assert pv["valid_phones"] == 3 and pv["duplicate_phones_in_sample"] == 1
    assert pv["issue_counts"]["invalid_phone"] == 1 and pv["issue_counts"]["unknown_state"] == 1
    assert [r["row"] for r in pv["needs_attention"]] == [3]           # rejected rows only
    assert pv["needs_attention"][0]["raw"]["Cell Phone"] == "12345"
    assert pv["sample"][0]["phone_e164"] == "+13135550101" and pv["sample"][0]["state"] == "MI"
    assert pv["sample"][0]["product_line"] is None

    pv2 = preview_file(p, {"Prodct": "product_line", "Notes": "ignore"})
    assert pv2["mapping"]["Prodct"] == "product_line"
    assert pv2["sample"][0]["product_line"] == "final_expense"


def test_preview_of_a_text_pdf(tmp_path):
    pv = preview_file(_two_page_list(tmp_path))
    assert pv["format"] == "pdf" and not pv["ocr"] and pv["rows_scanned"] == 25
    assert pv["pdf"] == {"method": "pdf_text", "pages": 2, "pages_read": 2}
    assert pv["mapping"]["Phone"] == "phone" and pv["mapping"]["Gender"] == "sex"
    assert pv["valid_phones"] == 25 and pv["needs_attention"] == []
    assert pv["sample"][0]["phone_e164"] == "+13135550101"


@needs_tesseract
def test_scanned_pdf_is_read_by_ocr_with_row_confidence(tmp_path):
    src = _two_page_list(tmp_path)
    scan = write_scanned_pdf(src, tmp_path / "scan.pdf")
    ex = extract_pdf(scan)
    assert ex.method == "ocr" and ex.pages == 2
    assert ex.headers == HEADERS
    assert len(ex.rows) == 25 and len(ex.ocr_confidence) == 25
    expected = lead_rows(25)
    digits = lambda s: "".join(ch for ch in s if ch.isdigit())  # noqa: E731
    assert [digits(r[2]) for r in ex.rows] == [digits(r[2]) for r in expected]
    assert sum(r == e for r, e in zip(ex.rows, expected)) >= 23     # whole rows read exactly
    assert min(ex.ocr_confidence) > 50 and sum(ex.ocr_confidence) / 25 > 85

    fmt, headers, rows = open_rows(scan)
    assert headers[-1] == OCR_CONFIDENCE_COLUMN
    pv = preview_file(scan)
    assert pv["ocr"] and OCR_CONFIDENCE_COLUMN not in pv["headers"] and pv["valid_phones"] == 25
    assert pv["pdf"] == {"method": "ocr", "pages": 2, "pages_read": 2}


@needs_tesseract
def test_preview_ocrs_only_the_first_pages(tmp_path, monkeypatch):
    import domain.imports.preview as preview

    monkeypatch.setattr(preview, "PREVIEW_OCR_PAGES", 1)
    scan = write_scanned_pdf(_two_page_list(tmp_path), tmp_path / "scan.pdf")
    pv = preview.preview_file(scan)
    assert pv["pdf"] == {"method": "ocr", "pages": 2, "pages_read": 1} and pv["rows_scanned"] == 20
