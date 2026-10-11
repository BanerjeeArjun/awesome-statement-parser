"""Stage 1 — document -> words with geometry -> lines.

PDF goes through LiteParse. CSV/XLS/XLSX are already tabular, so they are read with
the stdlib/openpyxl/xlrd and laid out on a synthetic grid (one cell = one word,
column i at x = i*GRID_W) — every later stage then treats all formats identically.

LiteParse returns phrase-level text items and merges neighbouring text when spacing is
tight ("741.81 Cr" spans an Amount and a Dr/Cr column). Column assignment needs real
word positions, so LiteParse is asked for word boxes (emit_word_boxes); items without
them are split with x-extents estimated from relative glyph widths.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from statistics import median
from typing import Optional

from .errors import PasswordRequired, ScannedDocument, UnsupportedFormat
from .models import Document, Line, Page, Word

GRID_W = 100.0
GRID_H = 12.0

# Rough Helvetica-like relative glyph widths (em fraction). Only ratios matter.
_NARROW = set("il.,:;'|!`ijlI1()[]{}/ ")
_WIDE = set("mwMW@%")


def _glyph_w(ch: str) -> float:
    if ch == " ":
        return 0.28
    if ch in _NARROW:
        return 0.3
    if ch in _WIDE:
        return 0.85
    if ch.isdigit():
        return 0.556
    if ch.isupper():
        return 0.68
    return 0.52


def split_item(text: str, x: float, y: float, width: float, height: float) -> list[Word]:
    """Split a LiteParse text item into words, distributing its width by glyph weight."""
    if not text or not text.strip():
        return []
    weights = [_glyph_w(c) for c in text]
    total = sum(weights) or 1.0
    scale = width / total if width > 0 else height * 0.5
    words: list[Word] = []
    pos = x
    cur, start = "", None
    for ch, w in zip(text, weights):
        if ch.isspace():
            if cur:
                words.append(Word(cur, start, pos, y, y + height))
                cur, start = "", None
        else:
            if not cur:
                start = pos
            cur += ch
        pos += w * scale
    if cur:
        words.append(Word(cur, start, pos, y, y + height))
    return words


def group_lines(page_no: int, words: list[Word]) -> list[Line]:
    """Cluster words into lines by vertical centre; sort each line left->right."""
    if not words:
        return []
    words = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    tol = 0.5 * (median(heights) if heights else 8.0)
    groups: list[list[Word]] = []
    centres: list[float] = []
    for w in words:
        yc = (w.y0 + w.y1) / 2
        if groups and abs(yc - centres[-1]) <= tol:
            groups[-1].append(w)
            n = len(groups[-1])
            centres[-1] = centres[-1] + (yc - centres[-1]) / n
        else:
            groups.append([w])
            centres.append(yc)
    lines = []
    for i, g in enumerate(groups):
        g.sort(key=lambda w: w.x0)
        lines.append(Line(page_no, i, g, min(w.y0 for w in g), max(w.y1 for w in g)))
    return lines


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_MIN_TEXT_CHARS = 40


def _liteparse(data: bytes, password: Optional[str], ocr: bool):
    import liteparse

    kwargs = dict(quiet=True, ocr_enabled=ocr, emit_word_boxes=True)
    if password:
        kwargs["password"] = password
    try:
        return liteparse.LiteParse(**kwargs).parse(data)
    except liteparse.ParseError as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise PasswordRequired(
                "This PDF is password-protected." if not password else "The password is incorrect."
            ) from e
        raise UnsupportedFormat(f"Could not read PDF: {e}") from e


def extract_pdf(data: bytes, filename: str, password: Optional[str] = None) -> Document:
    result = _liteparse(data, password, ocr=False)
    chars = sum(len(it.text.strip()) for p in result.pages for it in p.text_items)
    if chars < _MIN_TEXT_CHARS:
        # Image-only / scanned PDF: try LiteParse's built-in OCR (slower).
        try:
            result = _liteparse(data, password, ocr=True)
        except UnsupportedFormat:
            pass
        chars = sum(len(it.text.strip()) for p in result.pages for it in p.text_items)
        if chars < _MIN_TEXT_CHARS:
            raise ScannedDocument("No readable text found (scanned image and OCR produced nothing).")
    pages = []
    for idx, p in enumerate(result.pages, start=1):
        words: list[Word] = []
        for it in p.text_items:
            boxes = getattr(it, "words", None)
            if boxes:
                # exact word geometry from LiteParse
                words.extend(Word(b.text, b.x, b.x + b.width, b.y, b.y + b.height) for b in boxes if b.text.strip())
            else:
                words.extend(split_item(it.text, it.x, it.y, it.width, it.height))
        pages.append(Page(idx, p.width, p.height, group_lines(idx, words)))
    return Document(filename=filename, kind="pdf", pages=pages)


# ---------------------------------------------------------------------------
# Spreadsheets / CSV
# ---------------------------------------------------------------------------


def _grid_document(filename: str, rows: list[list[str]]) -> Document:
    lines: list[Line] = []
    ncols = 0
    for r, row in enumerate(rows):
        words = []
        for c, v in enumerate(row):
            v = "" if v is None else str(v).strip()
            if not v:
                continue
            x0 = c * GRID_W + 4
            y0 = r * GRID_H
            # cell height well under the row pitch, so an empty row reads as a vertical gap
            words.append(Word(v.replace("\n", " "), x0, x0 + GRID_W - 8, y0, y0 + GRID_H / 2))
            ncols = max(ncols, c + 1)
        if words:
            lines.append(Line(1, len(lines), words, words[0].y0, words[0].y1))
    page = Page(1, max(ncols, 1) * GRID_W, max(len(rows), 1) * GRID_H, lines)
    return Document(filename=filename, kind="grid", pages=[page])


def _cell_text(v) -> str:
    from datetime import date, datetime

    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, float):
        # Long integral floats are account/reference numbers, not money.
        if v.is_integer() and abs(v) >= 1e9:
            return str(int(v))
        return f"{v:.2f}"
    return str(v)


def extract_csv(data: bytes, filename: str) -> Document:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return _grid_document(filename, rows)


def _decrypt_office(data: bytes, password: Optional[str]) -> bytes:
    import msoffcrypto

    f = msoffcrypto.OfficeFile(io.BytesIO(data))
    if not f.is_encrypted():
        return data
    if not password:
        raise PasswordRequired("This spreadsheet is password-protected.")
    out = io.BytesIO()
    try:
        f.load_key(password=password)
        f.decrypt(out)
    except Exception as e:  # msoffcrypto raises several exception types for a bad key
        raise PasswordRequired("The password is incorrect.") from e
    return out.getvalue()


def extract_xlsx(data: bytes, filename: str, password: Optional[str] = None) -> Document:
    import openpyxl

    data = _decrypt_office(data, password)
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    rows: list[list[str]] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            rows.append([_cell_text(v) for v in row])
        rows.append([])
    return _grid_document(filename, rows)


def extract_xls(data: bytes, filename: str, password: Optional[str] = None) -> Document:
    import xlrd

    data = _decrypt_office(data, password)
    book = xlrd.open_workbook(file_contents=data)
    rows: list[list[str]] = []
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            out = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    from datetime import datetime

                    out.append(_cell_text(datetime(*xlrd.xldate_as_tuple(cell.value, book.datemode))))
                else:
                    out.append(_cell_text(cell.value))
            rows.append(out)
        rows.append([])
    return _grid_document(filename, rows)


def extract(data: bytes, filename: str, password: Optional[str] = None) -> Document:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return extract_pdf(data, filename, password)
    if ext == ".csv" or ext == ".txt":
        return extract_csv(data, filename)
    if ext == ".xlsx" or ext == ".xlsm":
        return extract_xlsx(data, filename, password)
    if ext == ".xls":
        # Some banks ship HTML or XLSX bytes with an .xls extension.
        if data[:2] == b"PK":
            return extract_xlsx(data, filename, password)
        return extract_xls(data, filename, password)
    raise UnsupportedFormat(f"Unsupported file type: {ext or 'unknown'} (PDF, CSV, XLS, XLSX supported)")
