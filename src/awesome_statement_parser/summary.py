"""Locate printed statement-level values (opening/closing balance, totals, period...) by label.

The LLM tells us the *label* ("Opening Balance", "Total Amount Due", ...) and the value it
read. We never trust the value blindly: the label is found in the document and the value
is read from the geometry next to it — same line to the right, or the cell directly below
(summary boxes are often a row of labels above a row of values). The LLM's value is used
only to choose between candidates, and is discarded if it does not appear in the document.
On a cached layout there is no LLM value at all; the label is enough.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from .models import Document, FieldSpec, Line, StatementInfo
from .values import date_prefix, find_dates, infer_date_format, parse_amount, parse_date, parse_date_any, signed_balance

_NUMERIC = {"opening_balance", "closing_balance", "total_debits", "total_credits", "minimum_amount_due"}
_COUNTS = {"debit_count", "credit_count"}
_DATES = {"period", "period_start", "period_end", "payment_due_date"}

_MONEYISH = re.compile(r"[-+(]?(?:INR|Rs\.?|₹)?\s?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?\)?(?:\s?(?:CR|DR|Cr|Dr|cr|dr)\b)?")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find_label(line: Line, label: str) -> Optional[tuple[int, int]]:
    """Return (first_word_index, last_word_index) of the label inside the line, or None."""
    want = _norm(label).split()
    if not want:
        return None
    toks = [_norm(w.text) for w in line.words]
    flat: list[tuple[str, int]] = []
    for i, t in enumerate(toks):
        for part in t.split():
            flat.append((part, i))
    for s in range(len(flat) - len(want) + 1):
        if [p for p, _ in flat[s : s + len(want)]] == want:
            return flat[s][1], flat[s + len(want) - 1][1]
    return None


def _label_rest(line: Line, label: str) -> Optional[str]:
    """Text after the label on the same line — including the remainder of the label's own
    cell ("Account Number : XXXX1234" is often ONE spreadsheet cell)."""
    toks = _norm(label).split()
    if not toks:
        return None
    m = re.search(r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, toks)) + r"(?![a-z0-9])", line.text, re.I)
    return line.text[m.end():] if m else None


def _values_right(line: Line, last: int, kind: str, rest: Optional[str] = None) -> list[str]:
    if rest is None:
        rest = " ".join(w.text for w in line.words[last + 1 :])
    rest = rest.strip(" :-")
    if kind in _NUMERIC:
        out = []
        for m in _MONEYISH.finditer(rest):
            t = m.group(0).strip()
            p = parse_amount(t)
            if p and ("." in t or "," in t):
                out.append(t)
        return out
    if kind in _COUNTS:
        m = re.match(r"^\D{0,3}(\d{1,5})\b", rest)
        return [m.group(1)] if m else []
    if kind in _DATES:
        return find_dates(rest) or ([date_prefix(rest)] if date_prefix(rest) else [])
    if kind == "account_number":
        m = re.search(r"[0-9Xx*•]{4,}[0-9Xx*• -]*\d", rest)
        return [m.group(0).strip()] if m else []
    return [rest] if rest else []


def _values_below(doc: Document, line: Line, first: int, last: int, kind: str) -> list[str]:
    page = doc.pages[line.page - 1]
    x0, x1 = line.words[first].x0, line.words[last].x1
    h = max(line.y1 - line.y0, 1.0)
    lx = (x0 + x1) / 2
    for nxt in page.lines[line.index + 1 : line.index + 4]:
        if nxt.y0 - line.y1 > 4 * h:
            break
        scored = []
        for cell in nxt.cells():
            cx0, cx1 = cell[0].x0, cell[-1].x1
            if cx1 < x0 - 2 * h or cx0 > x1 + 2 * h:
                continue
            overlap = max(0.0, min(cx1, x1) - max(cx0, x0))
            fake = Line(nxt.page, nxt.index, [type(cell[0])("", cx0, cx0, nxt.y0, nxt.y1)] + cell, nxt.y0, nxt.y1)
            for v in _values_right(fake, 0, kind):
                # the value directly under the label wins: most overlap, then nearest centre
                scored.append((-overlap, abs((cx0 + cx1) / 2 - lx), v))
        if scored:
            return [v for *_k, v in sorted(scored)]
    return []


# Common labels tried when the layout spec does not name a field (never trusted blindly —
# the pipeline keeps them only if the reconciliation verdict improves).
DEFAULT_LABELS = {
    "opening_balance": ["Opening Balance", "Previous Balance", "Previous Statement Balance", "Balance B/F",
                        "Balance Brought Forward", "Brought Forward", "Opening Bal", "Last Statement Balance", "Balance as on"],
    "closing_balance": ["Closing Balance", "Closing Bal", "Total Amount Due", "Total Dues", "New Balance",
                        "Balance C/F", "Total Outstanding", "Current Balance"],
    "total_debits": ["Total Debits", "Total Withdrawals", "Debits", "Purchases & Other Charges", "Total Purchases"],
    "total_credits": ["Total Credits", "Total Deposits", "Credits", "Payments & Credits", "Total Payments"],
    "debit_count": ["Dr Count", "Debit Count", "No. of Debits"],
    "credit_count": ["Cr Count", "Credit Count", "No. of Credits"],
}


def locate_field(doc: Document, f: FieldSpec, exclude: frozenset = frozenset()) -> Optional[tuple[str, str]]:
    """Return (value_text, line_id) for a field, reading the document next to its label."""
    candidates: list[tuple[str, str]] = []
    for ln in doc.all_lines():
        if ln.id in exclude:
            continue
        hit = _find_label(ln, f.label)
        if not hit:
            continue
        first, last = hit
        vals = _values_right(ln, last, f.kind, _label_rest(ln, f.label))
        if not vals:
            vals = _values_below(doc, ln, first, last, f.kind)
        candidates.extend((v, ln.id) for v in vals)
    if not candidates:
        return None
    if f.value:
        want = re.sub(r"\s+", "", f.value).lower()
        for v, lid in candidates:
            if re.sub(r"\s+", "", v).lower() == want:
                return v, lid
        p = parse_amount(f.value)
        if p:
            for v, lid in candidates:
                q = parse_amount(v)
                if q and q.value == p.value:
                    return v, lid
    return candidates[0]


def _amount(text: str) -> Optional[Decimal]:
    p = parse_amount(text)
    if not p:
        return None
    return signed_balance(p)


def build_statement_info(
    doc: Document, spec, txn_dates: list, exclude: frozenset = frozenset(), defaults: bool = False
) -> tuple[StatementInfo, list[str]]:
    """Read printed statement-level values. `exclude`: table line ids (a "Closing Balance"
    column header must never be read as the statement's closing balance)."""
    info = StatementInfo(statement_type=spec.statement_type, bank_name=spec.bank_name, currency=spec.currency)
    warnings: list[str] = []
    date_fmt = spec.date_format
    seen: set[str] = set()
    fields = list(spec.fields)
    if defaults:
        named = {f.kind for f in spec.fields}
        for kind, labels in DEFAULT_LABELS.items():
            if kind not in named:
                fields.extend(FieldSpec(kind, label, None) for label in labels)
    for f in fields:
        if f.kind in seen:
            continue
        found = locate_field(doc, f, exclude)
        if not found:
            if f.value:
                warnings.append(f"'{f.label}' ({f.kind}) could not be located in the document; ignored.")
            continue
        text, line_id = found
        seen.add(f.kind)
        info.provenance[f.kind] = {"label": f.label, "text": text, "line_id": line_id}
        if f not in spec.fields:
            info.provenance[f.kind]["default_label"] = True
        if f.kind in _NUMERIC:
            v = _amount(text)
            if v is None:
                continue
            if f.kind in ("total_debits", "total_credits", "minimum_amount_due"):
                v = abs(v)
            if spec.statement_type == "credit_card" and f.kind in ("opening_balance", "closing_balance"):
                # card balances: an amount owed is positive; a "Cr" balance means the bank owes you
                p = parse_amount(text)
                v = -p.value if (p.marker == "cr" or p.negative) else p.value
            setattr(info, f.kind, v)
        elif f.kind in _COUNTS:
            try:
                setattr(info, f.kind, int(text))
            except ValueError:
                pass
        elif f.kind == "account_number":
            info.account_number = text
        elif f.kind in _DATES:
            dates = [text] + [d for d in find_dates(text) if d != text]
            fmt = infer_date_format(dates) or date_fmt
            parsed = [parse_date(d, fmt) if fmt else parse_date_any(d) for d in dates]
            parsed = [p for p in parsed if p]
            if not parsed:
                continue
            if f.kind == "period":
                # "01/04/2024 to 30/04/2024": the label line holds both dates
                ln = doc.line(line_id)
                all_dates = [parse_date_any(d, fmt) for d in find_dates(ln.text if ln else text)]
                all_dates = sorted({d for d in all_dates if d})
                if len(all_dates) >= 2:
                    info.period_start, info.period_end = all_dates[0], all_dates[-1]
                else:
                    info.period_end = parsed[0]
            elif f.kind == "period_start":
                info.period_start = parsed[0]
            elif f.kind == "period_end":
                info.period_end = parsed[0]
            elif f.kind == "payment_due_date":
                info.payment_due_date = parsed[0]
    dates = [d for d in txn_dates if d]
    if dates and not info.period_start:
        info.provenance["period_start"] = {"derived": True}
    return info, warnings
