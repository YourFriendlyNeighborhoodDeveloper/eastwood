"""Tiny PDF writers for import tests — no PDF library needed.

write_text_pdf   a lead list with a real text layer (column-aligned text, or
                 a ruled table with grid=True), one list of rows per page
write_scanned_pdf  the same pages rendered to images and saved image-only,
                 like a scanned or photographed list (forces the OCR path)
"""
from __future__ import annotations

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_text_pdf(path: Path, pages: list[list[list[str]]], col_x: list[float], *, grid: bool = False,
                   font_size: float = 10, top: float = 560, line_gap: float = 20,
                   size: tuple[int, int] = (792, 612)) -> Path:
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    kids: list[int] = []
    next_id = 4
    for rows in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        kids.append(page_id)
        ops: list[str] = []
        if grid:
            right = size[0] - 20
            bottom_y = top - 6 - line_gap * (len(rows) - 1)
            ops.append("0.5 w")
            for i in range(len(rows) + 1):
                y = top + line_gap - 6 - i * line_gap
                ops.append(f"{col_x[0] - 4} {y} m {right} {y} l S")
            for x in [*(c - 4 for c in col_x), right]:
                ops.append(f"{x} {top + line_gap - 6} m {x} {bottom_y} l S")
        ops += ["BT", f"/F1 {font_size} Tf"]
        y = top
        for row in rows:
            # a row is either cells at the column positions, or explicit (x, text) pairs
            placed = row if row and isinstance(row[0], tuple) else list(zip(col_x, row))
            for x, cell in placed:
                ops.append(f"1 0 0 1 {x} {y} Tm ({_esc(cell)}) Tj")
            y -= line_gap
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
                         f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode()
        objs[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objs[2] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>").encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += b"%d 0 obj\n" % oid + objs[oid] + b"\nendobj\n"
    xref = len(out)
    n = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % n
    for oid in range(1, n):
        out += b"%010d 00000 n \n" % offsets[oid]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref)
    path.write_bytes(bytes(out))
    return path


def write_scanned_pdf(src: Path, dst: Path, *, dpi: int = 300) -> Path:
    """Rasterize every page of `src` and save the images as an image-only PDF."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(src))
    images = [page.render(scale=dpi / 72).to_pil().convert("L") for page in doc]
    doc.close()
    images[0].save(dst, "PDF", resolution=dpi, save_all=True, append_images=images[1:])
    return dst


HEADERS = ["First Name", "Last Name", "Phone", "State", "DOB", "Product", "Gender", "Tobacco"]
COL_X = [36, 120, 210, 320, 370, 450, 560, 630]


def lead_rows(n: int, *, start: int = 1) -> list[list[str]]:
    sexes = ["Female", "Male"]
    return [[f"Pat{i}", "Synthetic", f"(313) 555-{100 + i:04d}", "MI", f"03/{1 + i % 27:02d}/1955",
             "Final Expense", sexes[i % 2], "No" if i % 3 else "Yes"] for i in range(start, start + n)]
