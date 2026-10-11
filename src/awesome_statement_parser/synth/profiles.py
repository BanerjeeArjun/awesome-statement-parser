"""Layout families for synthetic statements. All banks are fictional.

Each profile reproduces a structural trait seen in real Indian statements (column sets,
two-line headers, Dr/Cr conventions, newest-first ordering, end-of-day balances, B/F and
C/F rows, multi-card sections, summary boxes...). Per-statement randomisation (fonts,
widths, row counts, wrapping, number style) happens in the generator.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Col:
    header: tuple[str, ...]  # header lines (1 or 2)
    field: str  # date|vdate|post_date|desc|ref|debit|credit|amount|drcr|balance|serial|points
    width: float  # relative width
    align: str = "l"  # l | r
    role: str = ""  # LayoutSpec role (filled from field when empty)


ROLE_OF = {
    "date": "date", "vdate": "value_date", "post_date": "value_date", "desc": "description", "ref": "reference",
    "debit": "debit", "credit": "credit", "amount": "amount", "drcr": "dr_cr", "balance": "balance",
    "serial": "other", "points": "other",
}


@dataclass
class Profile:
    key: str
    bank_name: str
    statement_type: str  # bank_account | credit_card
    columns: list[Col]
    date_formats: list[str]
    summary: str  # kv_top | kv_bottom | grid_top | grid_bottom | none
    labels: dict[str, str]  # field kind -> printed label
    amount_style: str = "dc"  # dc | signed | suffix | cr_only | drcr_col
    balance_suffix: bool = False  # "12,345.00 Cr"
    grouping: str = "indian"  # indian | western | none
    newest_first: bool = False
    opening_row: str | None = None  # in-table opening row narration
    closing_row: str | None = None
    page_carry: tuple[str, str] | None = None  # (C/F text at page end, B/F text at page start)
    repeat_header: bool = True
    middle_aligned: bool = False
    eod_balance: bool = False  # balance printed only on the last txn of each day
    zero_fill: bool = False  # print 0.00 in the empty debit/credit column
    card_sections: bool = False
    period_label: str | None = "Statement Period"
    formats: tuple[str, ...] = ("pdf",)
    total_row: str | None = None  # "Total" row at the end of the table with column sums
    currency: str = "INR"
    extra_noise: list[str] = field(default_factory=list)
    # ~1.5 pages of account information before the transaction table (new SBI-style format),
    # including distractor tables with amounts and dated rows
    preamble: bool = False
    txn_range: tuple[int, int] | None = None  # overrides the corpus-wide row count range
    # header printed as an image/banner: only these header words are real text ("none" = no text)
    header_text_only: tuple[str, ...] | None = None
    dash_empty: bool = False  # empty ref/debit/credit cells printed as "-"
    date_offset: float | None = None  # date line sits this many lines below the narration's first line
    summary_balance_suffix: str = ""  # e.g. "CR" appended to balances in the summary block
    desc_prefix: bool = False  # narrations start with a "WDL TFR"/"DEP TFR" line


PROFILES: list[Profile] = [
    Profile(
        key="saffron_savings",
        bank_name="Saffron Bank",
        statement_type="bank_account",
        columns=[
            Col(("Date",), "date", 1.0),
            Col(("Narration",), "desc", 3.3),
            Col(("Chq./Ref.No.",), "ref", 1.5),
            Col(("Value Dt",), "vdate", 1.0),
            Col(("Withdrawal Amt.",), "debit", 1.35, "r"),
            Col(("Deposit Amt.",), "credit", 1.35, "r"),
            Col(("Closing Balance",), "balance", 1.45, "r"),
        ],
        date_formats=["%d/%m/%y"],
        summary="grid_bottom",
        labels={"opening_balance": "Opening Balance", "debit_count": "Dr Count", "credit_count": "Cr Count",
                "total_debits": "Debits", "total_credits": "Credits", "closing_balance": "Closing Bal"},
        formats=("pdf", "csv"),
        extra_noise=["Earn 5% cashback up to 1,500.00 with Saffron Millennia Card!"],
    ),
    Profile(
        key="peacock_savings",
        bank_name="Peacock Bank of India",
        statement_type="bank_account",
        columns=[
            Col(("Txn Date",), "date", 1.15),
            Col(("Value", "Date"), "vdate", 1.15),
            Col(("Description",), "desc", 3.0),
            Col(("Ref No./Cheque", "No."), "ref", 1.6),
            Col(("Debit",), "debit", 1.2, "r"),
            Col(("Credit",), "credit", 1.2, "r"),
            Col(("Balance",), "balance", 1.4, "r"),
        ],
        date_formats=["%d %b %Y"],
        summary="kv_top",
        labels={"opening_balance": "Opening Balance", "closing_balance": "Closing Balance"},
        formats=("pdf", "xlsx"),
    ),
    Profile(
        key="lotus_current",
        bank_name="Lotus Co-operative Bank",
        statement_type="bank_account",
        columns=[
            Col(("Date",), "date", 1.1),
            Col(("Particulars",), "desc", 3.6),
            Col(("Amount",), "amount", 1.4, "r"),
            Col(("Dr/Cr",), "drcr", 0.6),
            Col(("Balance",), "balance", 1.6, "r"),
        ],
        date_formats=["%d-%m-%Y"],
        summary="none",
        labels={},
        amount_style="drcr_col",
        balance_suffix=True,
        opening_row="Opening Balance",
        closing_row="Closing Balance",
        page_carry=("Balance C/F", "Balance B/F"),
        period_label="Period",
        formats=("pdf", "csv"),
    ),
    Profile(
        key="banyan_savings",
        bank_name="Banyan Bank",
        statement_type="bank_account",
        columns=[
            Col(("Date",), "date", 1.2),
            Col(("Transaction Details",), "desc", 3.8),
            Col(("Amount (INR)",), "amount", 1.5, "r"),
            Col(("Balance (INR)",), "balance", 1.6, "r"),
        ],
        date_formats=["%d-%b-%Y"],
        summary="kv_top",
        labels={"opening_balance": "Opening Balance", "closing_balance": "Closing Balance",
                "total_debits": "Total Debits", "total_credits": "Total Credits"},
        amount_style="signed",
        newest_first=True,
        grouping="western",
    ),
    Profile(
        key="monsoon_savings",
        bank_name="Monsoon Bank",
        statement_type="bank_account",
        columns=[
            Col(("#",), "serial", 0.45),
            Col(("Date",), "date", 1.25),
            Col(("Description",), "desc", 3.1),
            Col(("Chq/Ref No",), "ref", 1.4),
            Col(("Withdrawal", "(Dr)"), "debit", 1.25, "r"),
            Col(("Deposit", "(Cr)"), "credit", 1.25, "r"),
            Col(("Balance",), "balance", 1.4, "r"),
        ],
        date_formats=["%d %b, %Y"],
        summary="grid_top",
        labels={"opening_balance": "Opening Balance", "total_debits": "Total Withdrawals",
                "total_credits": "Total Deposits", "closing_balance": "Closing Balance"},
        middle_aligned=True,
        repeat_header=False,
    ),
    Profile(
        key="kestrel_current",
        bank_name="Kestrel Bank",
        statement_type="bank_account",
        columns=[
            Col(("Date",), "date", 0.9),
            Col(("Description",), "desc", 3.8),
            Col(("Debit",), "debit", 1.3, "r"),
            Col(("Credit",), "credit", 1.3, "r"),
            Col(("Balance",), "balance", 1.5, "r"),
        ],
        date_formats=["%d%m%y"],
        summary="kv_bottom",
        labels={"opening_balance": "Opening balance", "closing_balance": "Closing balance"},
        grouping="western",
        eod_balance=True,
        zero_fill=False,
        total_row="Total",
    ),
    Profile(
        key="tiger_card",
        bank_name="Tiger Bank Credit Cards",
        statement_type="credit_card",
        columns=[
            Col(("Date",), "date", 1.2),
            Col(("Transaction Details",), "desc", 4.2),
            Col(("Reward Points",), "points", 1.0, "r"),
            Col(("Amount (in Rs.)",), "amount", 1.6, "r"),
        ],
        date_formats=["%d/%m/%Y"],
        summary="grid_top",
        labels={"opening_balance": "Previous Balance", "total_debits": "Purchases/Debits",
                "total_credits": "Payments/Credits", "closing_balance": "Total Amount Due"},
        amount_style="cr_only",
    ),
    Profile(
        key="comet_card",
        bank_name="Comet Card Services",
        statement_type="credit_card",
        columns=[
            Col(("Transaction Date",), "date", 1.4),
            Col(("Description",), "desc", 4.4),
            Col(("Amount",), "amount", 1.6, "r"),
        ],
        date_formats=["%d %b %y"],
        summary="kv_top",
        labels={"opening_balance": "Opening Balance", "total_debits": "Total Debits",
                "total_credits": "Total Credits", "closing_balance": "Closing Balance"},
        amount_style="suffix",
    ),
    Profile(
        key="nimbus_card",
        bank_name="Nimbus Bank",
        statement_type="credit_card",
        columns=[
            Col(("Date",), "date", 1.1),
            Col(("Posting Date",), "post_date", 1.1),
            Col(("Description",), "desc", 3.6),
            Col(("Debit",), "debit", 1.3, "r"),
            Col(("Credit",), "credit", 1.3, "r"),
        ],
        date_formats=["%Y-%m-%d"],
        summary="kv_bottom",
        labels={"opening_balance": "Previous Statement Balance", "total_debits": "Total Purchases",
                "total_credits": "Total Payments", "closing_balance": "New Balance"},
        card_sections=True,
        grouping="western",
    ),
    Profile(
        key="orchid_card",
        bank_name="Orchid Bank",
        statement_type="credit_card",
        columns=[
            Col(("DATE",), "date", 1.2),
            Col(("TRANSACTION DESCRIPTION",), "desc", 4.4),
            Col(("AMOUNT (Rs)",), "amount", 1.6, "r"),
        ],
        date_formats=["%d-%m-%Y"],
        summary="grid_bottom",
        labels={"opening_balance": "Opening Balance", "total_debits": "Purchases & Charges",
                "total_credits": "Payments & Credits", "closing_balance": "Total Dues"},
        amount_style="signed",
        middle_aligned=True,
    ),
    Profile(
        key="ashoka_savings",
        bank_name="Ashoka Bank",
        statement_type="bank_account",
        columns=[
            Col(("Txn Date",), "date", 1.15),
            Col(("Value", "Date"), "vdate", 1.15),
            Col(("Description",), "desc", 3.0),
            Col(("Ref No./Cheque", "No."), "ref", 1.6),
            Col(("Debit",), "debit", 1.2, "r"),
            Col(("Credit",), "credit", 1.2, "r"),
            Col(("Balance",), "balance", 1.4, "r"),
        ],
        date_formats=["%d %b %Y", "%d/%m/%Y"],
        summary="kv_bottom",
        labels={"opening_balance": "Opening Balance", "total_debits": "Total Debits",
                "total_credits": "Total Credits", "closing_balance": "Closing Balance"},
        preamble=True,
        txn_range=(150, 700),
    ),
    Profile(
        key="ashoka_v2",
        bank_name="Ashoka Bank",
        statement_type="bank_account",
        columns=[
            Col(("Txn Date",), "date", 1.0),
            Col(("Value Date",), "vdate", 1.0),
            Col(("Description",), "desc", 3.0),
            Col(("Ref No./Cheque No.",), "ref", 1.3),
            Col(("Debit",), "debit", 1.3, "r"),
            Col(("Credit",), "credit", 1.3, "r"),
            Col(("Balance",), "balance", 1.5, "r"),
        ],
        date_formats=["%d/%m/%Y"],
        summary="grid_bottom",
        labels={"opening_balance": "Brought Forward", "debit_count": "Dr Count", "credit_count": "Cr Count",
                "total_debits": "Total Debits", "total_credits": "Total Credits", "closing_balance": "Closing Balance"},
        preamble=True,
        txn_range=(150, 700),
        header_text_only=("Balance",),
        dash_empty=True,
        date_offset=0.8,
        summary_balance_suffix="CR",
        desc_prefix=True,
        period_label="Statement From",
    ),
]

BY_KEY = {p.key: p for p in PROFILES}
