"""Minimal reproduction: LiteParse silently drops text items.

Three strings, drawn in this order, on slightly offset baselines (~5 pt apart):

    "90000"         at (121.33, 392.47)
    "13"            at ( 36.00, 397.48)
    "18 Jul, 2025"  at ( 58.68, 397.48)     all Times-Roman 7.7 pt

LiteParse returns only ["18 Jul, 2025"]: "90000" and "13" are gone. The loss depends on
drawing order (drawing the date first loses the DATE instead), font and size (Helvetica
or 10 pt does not reproduce), and the 5 pt baseline offset (same baseline, or 15 pt
apart, does not reproduce). Output is deterministic for a given file.

Found while parsing bank statements whose narrations wrap onto a second line that is
only a number, with a serial number on the row's date line.

Run:  python scripts/liteparse_text_drop_repro.py     (needs reportlab + liteparse)
"""
import tempfile
from pathlib import Path

import liteparse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def render(items, path, font="Times-Roman", size=7.7):
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    for x, y, text in items:
        c.setFont(font, size)
        c.drawString(x, y, text)
    c.save()


def extracted(path):
    result = liteparse.LiteParse(quiet=True, ocr_enabled=False).parse(Path(path).read_bytes())
    return [item.text for item in result.pages[0].text_items]


def main():
    items = [(121.33, 392.47, "90000"), (36.0, 397.48, "13"), (58.68, 397.48, "18 Jul, 2025")]
    cases = {
        "as above": (items, {}),
        "date drawn first": ([items[2], items[0], items[1]], {}),
        "Helvetica": (items, {"font": "Helvetica"}),
        "10 pt": (items, {"size": 10}),
        "same baseline": ([(121.33, 397.48, "90000")] + items[1:], {}),
    }
    print(f"liteparse {getattr(liteparse, '__version__', '')}".strip())
    with tempfile.TemporaryDirectory() as tmp:
        for name, (its, kw) in cases.items():
            path = Path(tmp) / "repro.pdf"
            render(its, path, **kw)
            got = extracted(path)
            lost = [t for *_, t in its if not any(t in g for g in got)]
            print(f"{name:18} drawn={[t for *_, t in its]} -> extracted={got}  LOST={lost}")


if __name__ == "__main__":
    main()
