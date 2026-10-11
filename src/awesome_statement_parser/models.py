"""Core data structures shared by every stage of the pipeline.

Geometry is in PDF points with the origin at the top-left of the page (LiteParse's
convention). Spreadsheet/CSV inputs are mapped onto a synthetic grid so the same
table logic runs on every format.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

StatementType = Literal["bank_account", "credit_card"]
Direction = Literal["debit", "credit"]
CheckStatus = Literal["pass", "fail", "warn", "skip"]
Verdict = Literal["verified", "consistent", "needs_review", "failed"]

COLUMN_ROLES = (
    "date",          # transaction date
    "value_date",    # value/posting date (kept, not used for the txn date)
    "description",   # narration / particulars / transaction details
    "reference",     # cheque / ref number
    "debit",         # withdrawals / debits column
    "credit",        # deposits / credits column
    "amount",        # single amount column (sign/Dr-Cr marker carries direction)
    "dr_cr",         # separate Dr/Cr indicator column
    "balance",       # running balance
    "other",         # anything else (reward points, category, ...)
)

FIELD_KINDS = (
    "opening_balance",
    "closing_balance",
    "total_debits",
    "total_credits",
    "debit_count",
    "credit_count",
    "account_number",
    "period",
    "period_start",
    "period_end",
    "minimum_amount_due",
    "payment_due_date",
)


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    page: int  # 1-based
    index: int  # 0-based within the page
    words: list[Word]
    y0: float
    y1: float

    @property
    def id(self) -> str:
        return f"P{self.page}L{self.index}"

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    def cells(self, gap: float | None = None) -> list[list[Word]]:
        """Group words into visual cells: a new cell starts at a gap wider than ~2 spaces."""
        if not self.words:
            return []
        height = max(self.y1 - self.y0, 1.0)
        threshold = gap if gap is not None else 0.75 * height
        groups: list[list[Word]] = [[self.words[0]]]
        for w in self.words[1:]:
            if w.x0 - groups[-1][-1].x1 > threshold:
                groups.append([w])
            else:
                groups[-1].append(w)
        return groups


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list[Line]


@dataclass
class Document:
    filename: str
    kind: Literal["pdf", "grid"]
    pages: list[Page]

    def all_lines(self) -> list[Line]:
        return [ln for p in self.pages for ln in p.lines]

    def line(self, line_id: str) -> Optional[Line]:
        for ln in self.all_lines():
            if ln.id == line_id:
                return ln
        return None


@dataclass
class ColumnSpec:
    header: str
    role: str


@dataclass
class FieldSpec:
    kind: str
    label: str
    value: Optional[str] = None  # verbatim value as printed (LLM-provided; re-located by label)


@dataclass
class LayoutSpec:
    """What the LLM learns about a layout. Cacheable: contains no per-statement values
    except in FieldSpec.value, which is ignored when the spec is replayed from cache."""

    statement_type: StatementType
    columns: list[ColumnSpec]
    header_line: Optional[str] = None  # line id the LLM picked (first parse only)
    bank_name: Optional[str] = None
    currency: Optional[str] = None
    fields: list[FieldSpec] = field(default_factory=list)
    non_transaction_rows: list[str] = field(default_factory=list)
    date_format: Optional[str] = None  # filled in by the table stage
    # For tables with no readable header: column x-spans derived from the rows themselves
    # (set by the table stage; replayed from cache). When present, no header search is done.
    column_spans: Optional[list] = None

    def roles(self) -> list[str]:
        return [c.role for c in self.columns]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LayoutSpec":
        return LayoutSpec(
            statement_type=d["statement_type"],
            columns=[ColumnSpec(**c) for c in d["columns"]],
            header_line=d.get("header_line"),
            bank_name=d.get("bank_name"),
            currency=d.get("currency"),
            fields=[FieldSpec(**f) for f in d.get("fields", [])],
            non_transaction_rows=list(d.get("non_transaction_rows", [])),
            date_format=d.get("date_format"),
            column_spans=[tuple(x) for x in d["column_spans"]] if d.get("column_spans") else None,
        )


@dataclass
class SourceRef:
    page: int
    line_ids: list[str]
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in page points


@dataclass
class Transaction:
    id: int
    date: Optional[date]
    description: str
    amount: Optional[Decimal]
    direction: Optional[Direction]
    balance: Optional[Decimal] = None
    reference: Optional[str] = None
    value_date: Optional[date] = None
    date_text: str = ""
    amount_text: str = ""
    balance_text: str = ""
    direction_source: str = ""  # column | marker | sign | balance_delta | default
    source: Optional[SourceRef] = None
    edited: bool = False


@dataclass
class Anchor:
    """A balance printed inside the table that is not a transaction (B/F, C/F, opening row)."""

    kind: Literal["opening", "closing", "carry", "total"]
    balance: Optional[Decimal]
    text: str
    line_id: str
    after_txn: Optional[int]  # id of the transaction preceding it, None if before the first
    debit_total: Optional[Decimal] = None  # "Total" rows: the printed column sums
    credit_total: Optional[Decimal] = None


@dataclass
class StatementInfo:
    statement_type: StatementType
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    currency: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    total_debits: Optional[Decimal] = None
    total_credits: Optional[Decimal] = None
    debit_count: Optional[int] = None
    credit_count: Optional[int] = None
    minimum_amount_due: Optional[Decimal] = None
    payment_due_date: Optional[date] = None
    # where each printed value came from: "summary" (label lookup) | "table" (B/F row) | "derived"
    provenance: dict = field(default_factory=dict)


@dataclass
class UnaccountedLine:
    line_id: str
    page: int
    text: str
    reason: str
    bbox: tuple[float, float, float, float]


@dataclass
class Check:
    name: str
    status: CheckStatus
    message: str
    rows: list[int] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class ValidationReport:
    verdict: Verdict
    checks: list[Check]
    summary: str


@dataclass
class ParseResult:
    status: Verdict
    statement: StatementInfo
    transactions: list[Transaction]
    validation: ValidationReport
    spec: Optional[LayoutSpec]
    fingerprint: Optional[str]
    spec_source: str  # cache | llm | llm_repair | none
    anchors: list[Anchor] = field(default_factory=list)
    unaccounted: list[UnaccountedLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    pages: list[dict] = field(default_factory=list)  # [{number, width, height}]
    document_kind: str = "pdf"
