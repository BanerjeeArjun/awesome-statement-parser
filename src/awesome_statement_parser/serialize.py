"""JSON (de)serialisation of results. Money is always a string ("1234.50") — never a float."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from .values import fmt_amount
from .models import Anchor, ParseResult, SourceRef, StatementInfo, Transaction, UnaccountedLine


def to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return fmt_amount(obj)  # 2 places, or more when the bank printed more
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def result_to_dict(r: ParseResult) -> dict:
    d = to_jsonable(r)
    d["parser"] = "awesome-statement-parser"
    return d


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    return Decimal(str(v).replace(",", ""))


def _date(v) -> Optional[date]:
    if not v:
        return None
    return date.fromisoformat(str(v)[:10])


def txn_from_dict(d: dict) -> Transaction:
    src = d.get("source")
    return Transaction(
        id=int(d["id"]),
        date=_date(d.get("date")),
        description=d.get("description") or "",
        amount=_dec(d.get("amount")),
        direction=d.get("direction") or None,
        balance=_dec(d.get("balance")),
        reference=d.get("reference"),
        value_date=_date(d.get("value_date")),
        date_text=d.get("date_text", ""),
        amount_text=d.get("amount_text", ""),
        balance_text=d.get("balance_text", ""),
        direction_source=d.get("direction_source", ""),
        source=SourceRef(src["page"], src["line_ids"], tuple(src["bbox"])) if src else None,
        edited=bool(d.get("edited")),
    )


def info_from_dict(d: dict) -> StatementInfo:
    return StatementInfo(
        statement_type=d.get("statement_type") or "bank_account",
        bank_name=d.get("bank_name"),
        account_number=d.get("account_number"),
        currency=d.get("currency"),
        period_start=_date(d.get("period_start")),
        period_end=_date(d.get("period_end")),
        opening_balance=_dec(d.get("opening_balance")),
        closing_balance=_dec(d.get("closing_balance")),
        total_debits=_dec(d.get("total_debits")),
        total_credits=_dec(d.get("total_credits")),
        debit_count=d.get("debit_count"),
        credit_count=d.get("credit_count"),
        minimum_amount_due=_dec(d.get("minimum_amount_due")),
        payment_due_date=_date(d.get("payment_due_date")),
        provenance=d.get("provenance") or {},
    )


def anchor_from_dict(d: dict) -> Anchor:
    return Anchor(
        kind=d["kind"],
        balance=_dec(d.get("balance")),
        text=d.get("text", ""),
        line_id=d.get("line_id", ""),
        after_txn=d.get("after_txn"),
        debit_total=_dec(d.get("debit_total")),
        credit_total=_dec(d.get("credit_total")),
    )


def unaccounted_from_dict(d: dict) -> UnaccountedLine:
    return UnaccountedLine(d["line_id"], d["page"], d["text"], d["reason"], tuple(d["bbox"]))
