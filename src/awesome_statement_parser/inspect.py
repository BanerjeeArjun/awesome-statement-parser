"""`asp inspect` — a privacy-safe structural report for debugging a statement that fails.

Everything personal is masked: letters become "A", digits "9". Only column-header and
summary-label vocabulary (Date, Narration, Debit, Opening Balance, ...) stays readable,
so the report can be shared to diagnose a layout without revealing names, account
numbers, narrations or amounts.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

VOCAB = {
    "date", "txn", "tran", "transaction", "transactions", "value", "post", "posting", "description", "narration",
    "particulars", "details", "detail", "remarks", "ref", "reference", "cheque", "chq", "no", "number", "instrument",
    "debit", "debits", "credit", "credits", "withdrawal", "withdrawals", "deposit", "deposits", "dr", "cr", "balance",
    "amount", "amt", "rs", "inr", "opening", "closing", "total", "totals", "brought", "forward", "carried", "b", "f",
    "c", "statement", "period", "from", "to", "account", "summary", "page", "of", "branch", "code", "type", "count",
    "previous", "due", "minimum", "payment", "purchases", "payments", "available", "limit", "as", "on", "id", "sl",
    "s", "name", "currency", "ifsc", "micr", "nominee", "interest", "rate", "cleared", "uncleared", "mod", "drawing",
    "power", "sb", "ca", "td", "ln", "product", "accounts", "time", "upi", "neft", "imps", "rtgs", "atm", "pos",
}


def mask(text: str) -> str:
    def word(m: re.Match) -> str:
        w = m.group(0)
        return w if w.lower() in VOCAB else "A" * min(len(w), 6)
    return re.sub(r"[A-Za-z]+", word, re.sub(r"\d", "9", text or ""))


async def inspect_file(path: Path, password: Optional[str], settings=None, out: Optional[Path] = None) -> Path:
    from .config import get_settings
    from .extract import extract
    from .llm import LLMClient
    from .pipeline import run_spec
    from .spec import infer_spec, render_document, table_hint
    from .table import find_headers, find_table_start

    settings = settings or get_settings()
    data = path.read_bytes()
    doc = extract(data, path.name, password)
    rep: list[str] = [f"# asp inspect — {mask(path.name)}", f"kind={doc.kind} pages={len(doc.pages)} lines/page={[len(p.lines) for p in doc.pages[:6]]}…"]
    start = find_table_start(doc)
    rep.append(f"table start: {start.id if start else None}")
    hint = table_hint(doc)
    rep.append(f"hint: {hint}")

    rep.append("\n## lines around the table start (masked)")
    if start:
        page = doc.pages[start.page - 1]
        for ln in page.lines[max(0, start.index - 12): start.index + 10]:
            cells = ln.cells(gap=0.42 * max(ln.y1 - ln.y0, 1.0))
            rep.append(f"{ln.id:8} y={ln.y0:6.1f}-{ln.y1:6.1f}  " + " | ".join(f"{c[0].x0:.0f}-{c[-1].x1:.0f}:{mask(' '.join(w.text for w in c))}" for c in cells))

    rep.append("\n## prompt sent to the model (masked)")
    rep.extend(mask(l) for l in render_document(doc).splitlines())

    rep.append("\n## model answer")
    t = time.monotonic()
    try:
        spec = await infer_spec(doc, LLMClient(settings))
    except Exception as e:  # report whatever happened
        rep.append(f"model error: {type(e).__name__}: {e}")
        spec = None
    rep.append(f"seconds={time.monotonic() - t:.1f}")
    if spec:
        rep.append(f"type={spec.statement_type} header_line={spec.header_line}")
        rep.append("columns: " + " | ".join(f"{c.header!r}={c.role}" for c in spec.columns))
        rep.append("fields: " + "; ".join(f"{f.kind}: label={f.label!r} value={mask(f.value or '')!r}" for f in spec.fields))
        rep.append(f"non_transaction_rows: {spec.non_transaction_rows}")
        hits = find_headers(doc, spec)
        rep.append(f"header occurrences found: {len(hits)}; first: {[(h.page, h.line_ids, len(h.spans)) for h in hits[:3]]}")
        r = run_spec(doc, spec, "inspect")
        rep.append(f"\n## result: {r.status} rows={len(r.transactions)} date_format={r.spec.date_format}")
        for c in r.validation.checks:
            rep.append(f"  [{c.status}] {c.name}: {mask(c.message)}")
        rep.append(f"warnings: {[mask(w) for w in r.warnings]}")
        rep.append(f"unaccounted lines: {[(u.line_id, mask(u.text)[:60]) for u in r.unaccounted[:10]]}")
    out = out or settings.data_dir / f"inspect-{path.stem}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(rep))
    return out
