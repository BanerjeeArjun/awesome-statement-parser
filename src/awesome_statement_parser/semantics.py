"""Non-arithmetic cross-checks that break the sign symmetry the arithmetic cannot see.

Swapping the debit and credit columns AND flipping bank<->card (whose balances move in
opposite directions) cancel out: every row's direction is wrong yet every balance, total
and closing equation still reconciles. So the layout itself must also agree with the
printed words:

  column_semantics — a column whose PRINTED header says Withdrawal/Debit/Dr/Paid out is
                     never mapped as credit, and vice versa.
  statement_type   — the document's own vocabulary (card number, minimum amount due,
                     IFSC, savings account ...) must not strongly contradict the type.
"""
from __future__ import annotations

import re

from .models import Check, Document, LayoutSpec

_DEBIT_WORDS = {"debit", "debits", "withdrawal", "withdrawals", "dr", "paid out", "money out", "payments out", "outflow", "purchases", "charges", "spent"}
_CREDIT_WORDS = {"credit", "credits", "deposit", "deposits", "cr", "paid in", "money in", "receipts", "inflow", "lodgements"}

_CARD_PHRASES = [
    "credit card", "card number", "card no", "minimum amount due", "min amount due", "minimum due", "total amount due",
    "payment due date", "credit limit", "available credit", "cash limit", "reward points", "statement balance",
]
_BANK_PHRASES = [
    "account number", "account no", "a c no", "ifsc", "micr", "savings account", "current account", "branch",
    "nominee", "cheque", "chq", "customer id", "cif", "account type", "joint holder",
]


def _norm(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


def header_meaning(header: str) -> str | None:
    """'debit' / 'credit' when a printed header says so unambiguously."""
    h = _norm(header)
    deb = any(f" {w} " in h for w in _DEBIT_WORDS)
    cre = any(f" {w} " in h for w in _CREDIT_WORDS)
    if deb and not cre:
        return "debit"
    if cre and not deb:
        return "credit"
    return None


def column_semantics(spec: LayoutSpec, printed_headers: list[str | None]) -> Check:
    bad = []
    for col, printed in zip(spec.columns, printed_headers):
        if col.role not in ("debit", "credit"):
            continue
        meaning = header_meaning(printed or col.header)
        if meaning and meaning != col.role:
            bad.append(f"'{printed or col.header}' is mapped as {col.role}")
    if bad:
        return Check("column_semantics", "fail", "Column roles contradict the printed headers: " + "; ".join(bad) + ".")
    return Check("column_semantics", "pass", "Debit/credit columns agree with their printed headers.")


def classify_statement_type(doc: Document) -> tuple[str | None, int, int]:
    text = _norm(" ".join(ln.text for ln in doc.all_lines()[:400]))
    card = sum(1 for p in _CARD_PHRASES if f" {p} " in text)
    bank = sum(1 for p in _BANK_PHRASES if f" {p} " in text)
    if card >= bank + 2:
        return "credit_card", card, bank
    if bank >= card + 2:
        return "bank_account", card, bank
    return None, card, bank


def statement_type_check(doc: Document, spec: LayoutSpec) -> Check:
    guess, card, bank = classify_statement_type(doc)
    label = {"credit_card": "a credit card statement", "bank_account": "a bank account statement"}
    if guess and guess != spec.statement_type:
        return Check(
            "statement_type",
            "fail",
            f"Read as {label[spec.statement_type]}, but the document's wording says {label[guess]} "
            f"({card} card terms vs {bank} bank terms).",
        )
    return Check("statement_type", "pass", f"Statement type ({spec.statement_type.replace('_', ' ')}) agrees with the document's wording.")
