"""Lead lists from PDF: tables, column-aligned text, or scanned pages via OCR.

Three strategies, tried in order per document:
  1. pdf_table  real PDF table structure (pdfplumber)
  2. pdf_text   column-aligned text (vendor exports, CRM print-outs)
  3. ocr        no text layer (scanned / photographed lists): pages rendered at
                300 DPI (pypdfium2) and read by Tesseract locally — nothing
                leaves the machine. Each row carries its mean OCR confidence in
                a "_ocr_confidence" column so low-confidence rows are flagged
                for review instead of imported blindly.

Text and OCR share one column model: the header line fixes the columns, and
every word below it goes to the column whose region it overlaps most. Words
are placed by position, not by counting spaces, so narrow gaps between
columns never merge two fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADER_HINTS = re.compile(r"phone|mobile|cell|name|first|last|state|dob|birth|email|zip|age|product", re.I)


@dataclass
class PdfExtraction:
    method: str
    headers: list[str]
    rows: list[list[str]]
    pages: int
    ocr_confidence: list[float] = field(default_factory=list)
    pages_read: int | None = None   # < pages when OCR was capped (preview)


def _from_tables(pdf) -> PdfExtraction | None:
    headers: list[str] | None = None
    rows: list[list[str]] = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            cleaned = [[(c or "").strip() for c in r] for r in table if r and any((c or "").strip() for c in r)]
            if not cleaned:
                continue
            if headers is None:
                if len(cleaned[0]) >= 2 and _HEADER_HINTS.search(" ".join(cleaned[0])):
                    headers, cleaned = cleaned[0], cleaned[1:]
                else:
                    continue
            elif cleaned[0] == headers:
                cleaned = cleaned[1:]
            rows.extend(r for r in cleaned if len(r) == len(headers))
    return PdfExtraction("pdf_table", headers, rows, len(pdf.pages)) if headers and rows else None


def _group_words(words: list[dict], gap: float) -> list[dict]:
    """Merge words closer than `gap` into cells (e.g. 'First' 'Name' -> 'First Name')."""
    cells: list[dict] = []
    for w in words:
        if cells and w["x0"] - cells[-1]["x1"] < gap:
            cells[-1]["text"] += " " + w["text"]
            cells[-1]["x1"] = max(cells[-1]["x1"], w["x1"])
        else:
            cells.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
    return cells


def _column_for(x0: float, x1: float, bounds: list[float]) -> int:
    """Column whose region overlaps the word most. Region i runs from just left
    of header i to just left of header i+1, so left-aligned values wider than
    their header stay in their column, and right-aligned numbers that start a
    little left of a short header still land under it."""
    edges = [float("-inf"), *bounds, float("inf")]
    best, best_overlap = 0, float("-inf")
    for i in range(len(edges) - 1):
        overlap = min(x1, edges[i + 1]) - max(x0, edges[i])
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    return best


def _rows_from_lines(lines: list[list[dict]], gap: float):
    """Header = first line that looks like one (>= 3 cells, a known header
    word). Every later line's words are placed under the header column they
    overlap most, word by word, so tight columns never merge. Lines that repeat
    the header (multi-page lists) or fill fewer than 2 columns (titles, page
    footers) are skipped. Returns (headers, rows, per-row mean confidence)."""
    header: list[dict] | None = None
    bounds: list[float] = []
    rows: list[list[str]] = []
    confs: list[float] = []
    for words in lines:
        if not words:
            continue
        if header is None:
            cells = _group_words(words, gap)
            if len(cells) >= 3 and _HEADER_HINTS.search(" ".join(c["text"] for c in cells)):
                header = cells
                bounds = [c["x0"] - gap / 2 for c in cells[1:]]
            continue
        values: list[list[str]] = [[] for _ in header]
        word_confs = []
        for w in words:
            values[_column_for(w["x0"], w["x1"], bounds)].append(w["text"])
            if w.get("conf", -1) >= 0:
                word_confs.append(w["conf"])
        joined = [" ".join(v) for v in values]
        if joined == [h["text"] for h in header] or sum(1 for v in joined if v) < 2:
            continue
        rows.append(joined)
        confs.append(round(sum(word_confs) / len(word_confs), 1) if word_confs else 0.0)
    return ([h["text"] for h in header] if header else None), rows, confs


def _text_lines(page) -> list[list[dict]]:
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= max(2.0, (w["bottom"] - w["top"]) * 0.4):
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"]} for w in line),
                   key=lambda w: w["x0"]) for line in lines]


def _from_text(pdf) -> PdfExtraction | None:
    lines = [line for page in pdf.pages for line in _text_lines(page)]
    sizes = sorted(c["size"] for c in pdf.pages[0].chars) or [10.0]
    gap = 0.6 * sizes[len(sizes) // 2]   # ~ two spaces at the body font size
    headers, rows, _ = _rows_from_lines(lines, gap)
    return PdfExtraction("pdf_text", headers, rows, len(pdf.pages)) if headers and rows else None


def _ocr_page_lines(image) -> list[list[dict]]:
    import pytesseract

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config="--psm 6")
    lines: dict[tuple, list[dict]] = {}
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x0 = float(data["left"][i])
        lines.setdefault(key, []).append({"text": word.strip(), "x0": x0, "x1": x0 + float(data["width"][i]),
                                          "conf": float(data["conf"][i])})
    return [sorted(ws, key=lambda w: w["x0"]) for _, ws in sorted(lines.items())]


def _from_ocr(path: Path, dpi: int = 300, max_pages: int | None = None) -> PdfExtraction:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        n_pages = len(doc)
        read = n_pages if max_pages is None else min(n_pages, max_pages)
        lines = [line for i in range(read) for line in _ocr_page_lines(doc[i].render(scale=dpi / 72).to_pil())]
    finally:
        doc.close()
    headers, rows, confs = _rows_from_lines(lines, gap=dpi * 0.09)  # ~ two spaces at 9-11 pt
    if not headers:
        return PdfExtraction("ocr", [], [], n_pages, pages_read=read)
    return PdfExtraction("ocr", headers, rows, n_pages, confs, pages_read=read)


def extract_pdf(path: Path, *, ocr_max_pages: int | None = None) -> PdfExtraction:
    """`ocr_max_pages` caps OCR for previews (about 2 s a page); imports read every page."""
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        has_text = any((p.extract_text() or "").strip() for p in pdf.pages[: min(n, 3)])
        if has_text:
            found = _from_tables(pdf) or _from_text(pdf)
            if found:
                found.pages_read = n
                return found
    return _from_ocr(path, max_pages=ocr_max_pages)
