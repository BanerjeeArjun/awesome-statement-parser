"""Stage 2 — ask the local LLM to describe the layout (never to transcribe rows).

The model sees a compact rendering of the first page and the end of the last page:
one line per visual row, prefixed with a line id, cells separated by " | ". It returns
a LayoutSpec: statement type, column roles in header order, the labels of the printed
summary values, and rows inside the table that are not transactions. Everything else is
deterministic Python. One small call (~2–4k tokens in, a few hundred out) per new layout.
"""
from __future__ import annotations

from typing import Optional

from .errors import LayoutNotUnderstood
from .semantics import header_meaning
from .table import find_date, find_table_start
from .llm import LLMClient
from .models import COLUMN_ROLES, FIELD_KINDS, ColumnSpec, Document, FieldSpec, LayoutSpec
from .values import looks_like_money

MAX_CELL = 48
FIRST_PAGE_LINES = 70
LAST_PAGE_LINES = 30
PREAMBLE_HEAD = 30   # first lines of a long preamble (bank, account, period)
PREAMBLE_EXTRA = 25  # later preamble lines with amounts or "Label :" pairs

SYSTEM_PROMPT = """You read the layout of a bank account statement or credit card statement.
You are given its text lines. Each line starts with an id like P1L12 (page 1, line 12); visually separate cells are joined with " | ".
Do NOT list transactions. Describe the layout so a program can read every row itself.

Return JSON:
- statement_type: "bank_account" (savings/current account) or "credit_card".
- bank_name: the issuing bank as printed, or null.
- currency: ISO code such as "INR", or null.
- header_line: id of the transaction table's column-header line (for a two-line header, the first line).
- columns: EVERY column of the transaction table from left to right. "header" is the header text exactly as printed (join both lines of a two-line header, e.g. "Withdrawal Amount (INR)"). "role" is one of:
    date = transaction date; value_date = value/posting date (when a separate transaction date exists);
    description = narration/particulars/details; reference = cheque or reference number;
    debit = withdrawals/debits column; credit = deposits/credits column;
    amount = a single amount column whose sign or Dr/Cr marker gives the direction;
    dr_cr = a separate column holding only Dr/Cr; balance = running balance; other = anything else.
- fields: statement-level values printed OUTSIDE the transaction rows — look for the summary box at the top of the first page AND the end of the last page; always include opening_balance and closing_balance when printed. For each: kind, label exactly as printed, value exactly as printed.
    kinds: opening_balance (also "previous balance", "balance b/f" in a summary), closing_balance (for cards: "total amount due"/"closing balance"),
    total_debits (all debits/purchases/withdrawals), total_credits (all credits/payments/deposits), debit_count, credit_count,
    account_number, period (a "from ... to ..." statement period), period_start, period_end, minimum_amount_due, payment_due_date.
    Only include values that are actually printed. Copy labels and values character for character.
- non_transaction_rows: short narration text of rows INSIDE the table that are not transactions (e.g. "Opening Balance", "Balance B/F", "Total"). Text, never line ids. Usually empty.
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "statement_type": {"type": "string", "enum": ["bank_account", "credit_card"]},
        "bank_name": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "header_line": {"type": "string"},
        "columns": {
            "type": "array",
            "maxItems": 14,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"header": {"type": "string", "maxLength": 60}, "role": {"type": "string", "enum": list(COLUMN_ROLES)}},
                "required": ["header", "role"],
            },
        },
        "fields": {
            "type": "array",
            "maxItems": 14,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": list(FIELD_KINDS)},
                    "label": {"type": "string"},
                    "value": {"type": ["string", "null"]},
                },
                "required": ["kind", "label", "value"],
            },
        },
        "non_transaction_rows": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 40}},
    },
    "required": ["statement_type", "bank_name", "currency", "header_line", "columns", "fields", "non_transaction_rows"],
}


def render_line(ln) -> str:
    # split cells at ~2 spaces: tight column headers ("Withdrawal Amt.  Deposit Amt.") stay apart
    cells = []
    for cell in ln.cells(gap=0.42 * max(ln.y1 - ln.y0, 1.0)):
        t = " ".join(w.text for w in cell)
        cells.append(t if len(t) <= MAX_CELL else t[: MAX_CELL - 1] + "…")
    return f"{ln.id}: " + " | ".join(cells)


def _compact(lines, keep_rows: int) -> list[str]:
    """Keep every non-transaction line (headers, summaries, labels) but only the first
    `keep_rows` dated rows — they all look alike and cost the most tokens."""
    out, rows, skipped = [], 0, 0
    for ln in lines:
        if find_date(ln.text) and looks_like_money(ln.text):
            rows += 1
            if rows > keep_rows:
                skipped += 1
                continue
        elif rows > keep_rows and not looks_like_money(ln.text) and len(ln.words) <= 6 and skipped:
            continue  # wrapped narration of an omitted row
        if skipped:
            out.append(f"... ({skipped} similar transaction lines omitted)")
            skipped = 0
        out.append(render_line(ln))
    if skipped:
        out.append(f"... ({skipped} similar transaction lines omitted)")
    return out


def render_document(doc: Document) -> str:
    """Compact view of what the model needs, wherever it sits in the document:

      * the preamble before the table (account details, summary boxes) — its first lines
        plus every later line that carries an amount or a "Label :" pair;
      * the table header and the first rows — located deterministically, so a table that
        starts mid-page-2 after a long account-information preamble is still shown;
      * the end of the statement (closing summaries live there).

    Repetitive transaction rows are elided."""
    lines = doc.all_lines()
    pos = {ln.id: i for i, ln in enumerate(lines)}
    start = find_table_start(doc)
    t0 = pos[start.id] if start else min(FIRST_PAGE_LINES, len(lines))
    win_lo = max(0, t0 - 10)

    keep: list[int] = []
    pre = list(range(0, win_lo))
    head = pre[:PREAMBLE_HEAD]
    keep.extend(head)
    labelled = [i for i in pre[PREAMBLE_HEAD:] if looks_like_money(lines[i].text) or " : " in f" {lines[i].text} " or ":" in lines[i].text]
    keep.extend(labelled[:PREAMBLE_EXTRA])
    keep.extend(range(win_lo, min(len(lines), t0 + 24)))  # header + first rows (compacted below)
    keep.extend(range(max(0, len(lines) - LAST_PAGE_LINES), len(lines)))
    keep = sorted(set(keep))

    out: list[str] = []
    block: list = []
    prev = None
    def flush():
        if block:
            out.extend(_compact(block, keep_rows=6 if block[0] is lines[win_lo] else 2))
    for i in keep:
        if prev is not None and i != prev + 1:
            flush()
            block = []
            out.append("...")
        block.append(lines[i])
        prev = i
    flush()
    return "\n".join(out)


def spec_from_json(d: dict) -> LayoutSpec:
    cols: list[ColumnSpec] = []
    raw = [c for c in d.get("columns", []) if (c.get("header") or "").strip()]
    if sum("|" in c["header"] for c in raw) >= 2:
        # the model pasted the whole header row into every column: column k is the k-th cell
        # (or, if the paste was cut short, just its role — header alignment matches by role words)
        for k, c in enumerate(raw):
            parts = [p.strip() for p in c["header"].split("|") if p.strip()]
            header = parts[k] if k < len(parts) else c["role"].replace("_", " ")
            cols.append(ColumnSpec(header=header, role=c["role"]))
        raw = []
    for c in raw:
        # the model sometimes copies two rendered cells as one header: "Txn Date | Value"
        parts = [p.strip() for p in (c.get("header") or "").split("|") if p.strip()]
        for k, part in enumerate(parts):
            role = c["role"]
            if k:
                low = part.lower()
                role = "value_date" if role == "date" or "value" in low or "posting" in low else "other"
            cols.append(ColumnSpec(header=part, role=role))
    if sum(c.role == "amount" for c in cols) > 1:
        # two "amount" columns can't both be right: "Withdrawal"/"Deposit" say which is which
        for c in cols:
            if c.role == "amount":
                c.role = header_meaning(c.header) or c.role
    roles = [c.role for c in cols]
    if "date" not in roles:
        raise LayoutNotUnderstood("The model did not identify a transaction date column.")
    if not ({"debit", "credit", "amount"} & set(roles)):
        raise LayoutNotUnderstood("The model did not identify an amount column.")
    fields = [
        FieldSpec(kind=f["kind"], label=f["label"].strip(), value=(f.get("value") or None))
        for f in d.get("fields", [])
        if f.get("label", "").strip()
    ]
    return LayoutSpec(
        statement_type=d.get("statement_type") or "bank_account",
        columns=cols,
        header_line=d.get("header_line"),
        bank_name=d.get("bank_name"),
        currency=d.get("currency"),
        fields=fields,
        non_transaction_rows=[s for s in d.get("non_transaction_rows", []) if s.strip()],
    )


def table_hint(doc: Document) -> Optional[str]:
    """Where the transaction rows start, found deterministically, so a small model does not
    mistake a summary box or an account-information table for the transaction table."""
    start = find_table_start(doc)
    if start is None:
        return None
    page = doc.pages[start.page - 1].lines

    def cells(ln) -> int:
        return len(ln.cells(gap=0.42 * max(ln.y1 - ln.y0, 1.0)))

    def plain(ln) -> bool:
        return not looks_like_money(ln.text) and not find_date(ln.text)

    # the header is the nearest MULTI-column line above the first row (a wrapped narration
    # of a middle-aligned row can sit in between)
    above = next((page[j] for j in range(start.index - 1, max(-1, start.index - 7), -1)
                  if plain(page[j]) and cells(page[j]) >= 3), None)
    from .table import tokens, _ROLE_WORDS

    vocab = set().union(*_ROLE_WORDS.values())
    if above is not None and len(tokens(above.text) & vocab) < 2:
        above = None  # nearest multi-column line is not a header (e.g. account details)
    if above is None:
        return (f"Hint: the transaction rows begin at {start.id}. This table has NO readable column-header line "
                "(it may be an image). List its columns left to right from what the rows contain, e.g. "
                "date | value date | description | reference | debit | credit | balance, and use the first row's "
                "line id as header_line. Tables above it are not the transaction table.")
    if above is not None:
        j = above.index
        if j > 0:  # a two-line header whose second line sits between
            up = page[j - 1]
            if cells(up) >= 3 and plain(up) and above.y0 - up.y1 < 0.8 * max(up.y1 - up.y0, 1.0):
                above = up
        return (f"Hint: the transaction rows begin at {start.id}; the column header line is probably {above.id}. "
                "Tables above that (account details, summaries, standing instructions) are not the transaction table.")
    return f"Hint: the transaction rows begin at {start.id}."


async def infer_spec(doc: Document, client: LLMClient, feedback: Optional[str] = None) -> LayoutSpec:
    user = "Statement lines:\n" + render_document(doc)
    hint = table_hint(doc)
    if hint:
        user += "\n\n" + hint
    if feedback:
        user += (
            "\n\nA previous reading of this layout failed the arithmetic checks:\n"
            + feedback
            + "\nRe-check the column roles (which column is debit vs credit, whether a single amount column is used), "
            "the header line, and that every label/value is copied exactly."
        )
    data = await client.complete_json(SYSTEM_PROMPT, user, SCHEMA, name="statement_layout", max_tokens=1200)
    return spec_from_json(data)
