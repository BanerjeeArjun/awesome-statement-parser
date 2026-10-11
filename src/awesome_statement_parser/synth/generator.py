"""Synthetic statement generator with exact ground truth.

For every statement it writes:
  <id>.<pdf|csv|xlsx>   the rendered statement
  <id>.truth.json       ground truth (rows, opening/closing, totals, period)
  <id>.spec.json        the oracle LayoutSpec (what a perfect LLM answer would be)
and a manifest.json for the whole corpus. Requires `reportlab` for PDFs.

Data is fictional: invented banks, people, merchants and account numbers.
"""
from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

from ..models import ColumnSpec, FieldSpec, LayoutSpec
from .profiles import PROFILES, ROLE_OF, Profile

PASSWORD = "test1234"

MERCHANTS = [
    "SWIGGY", "ZOMATO", "AMAZON PAY", "FLIPKART", "BIGBASKET", "DMART", "RELIANCE FRESH", "UBER INDIA", "OLA CABS",
    "IRCTC", "MAKEMYTRIP", "BOOKMYSHOW", "NETFLIX", "SPOTIFY", "AIRTEL PAYMENTS", "JIO PREPAID", "BESCOM",
    "TATA POWER", "INDIAN OIL", "HP PETROL PUMP", "APOLLO PHARMACY", "MEDPLUS", "CROMA", "DECATHLON",
    "STARBUCKS", "CAFE COFFEE DAY", "NYKAA", "MYNTRA", "URBAN COMPANY", "PVR CINEMAS", "BLINKIT", "ZEPTO",
]
CITIES = ["BANGALORE", "MUMBAI", "PUNE", "DELHI", "GURGAON", "CHENNAI", "HYDERABAD", "KOLKATA", "NOIDA", "AHMEDABAD"]
PEOPLE = ["RAHUL SHARMA", "PRIYA NAIR", "ANKIT VERMA", "SNEHA IYER", "VIKRAM SINGH", "MEERA PATEL", "ARJUN RAO", "KAVYA REDDY"]
BANK_CODES = ["SAFR", "PCOK", "LOTS", "BNYN", "MNSN", "KSTR", "TIGR", "CMET", "NMBS", "ORCD"]
COMPANIES = ["ACME TECHNOLOGIES PVT LTD", "INFOSPHERE SOLUTIONS", "BRIGHTLINE CONSULTING", "NORTHWIND TRADERS"]


@dataclass
class SynthTxn:
    date: date
    description: str
    amount: Decimal
    direction: str
    balance: Optional[Decimal] = None  # balance after the transaction (bank only)
    ref: str = ""
    points: str = ""
    section: int = 0


@dataclass
class SynthStatement:
    profile: Profile
    opening: Decimal
    closing: Decimal
    txns: list[SynthTxn]  # chronological
    period_start: date
    period_end: date
    account_number: str
    holder: str
    date_fmt: str
    grouping: str
    min_due: Optional[Decimal] = None
    due_date: Optional[date] = None
    sections: list[str] = field(default_factory=list)


def q(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_num(v: Decimal, grouping: str) -> str:
    s = f"{abs(v):.2f}"
    whole, frac = s.split(".")
    if grouping == "western":
        whole = f"{int(whole):,}"
    elif grouping == "indian" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{frac}"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _bank_desc(rng: random.Random, direction: str, big: bool) -> str:
    ref = "".join(rng.choice("0123456789") for _ in range(12))
    m = rng.choice(MERCHANTS)
    if direction == "credit":
        if big:
            return f"NEFT CR-{rng.choice(BANK_CODES)}0{rng.randint(100000, 999999)}-{rng.choice(COMPANIES)}-SALARY {rng.choice(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])}"
        return rng.choice([
            f"UPI/{ref}/{rng.choice(PEOPLE)}/{rng.choice(PEOPLE).split()[0].lower()}@ok{rng.choice(['axis','sbi','hdfc','icici'])}/Payment",
            f"IMPS/P2A/{ref}/{rng.choice(PEOPLE)}",
            "INT.PD:01-01-2024 TO 31-03-2024" if rng.random() < 0.3 else f"REFUND {m} {ref[:8]}",
            f"CHQ DEP-{rng.randint(100000, 999999)}-CLG",
        ])
    return rng.choice([
        f"UPI/{ref}/{m} {rng.choice(CITIES)}/{m.split()[0].lower()}@ybl/UPI",
        f"POS 4{rng.randint(100, 999)}XXXXXX{rng.randint(1000, 9999)} {m} {rng.choice(CITIES)}",
        f"ATM WDL {rng.choice(CITIES)} {rng.choice(['MG ROAD','STATION RD','SECTOR 18','ANDHERI W'])} ATM ID {rng.randint(10000, 99999)}",
        f"ACH D- {rng.choice(['HDFC MF SIP','BAJAJ FINANCE EMI','LIC PREMIUM','ICICI PRU'])}-{ref}",
        f"NEFT DR-{rng.choice(BANK_CODES)}0{rng.randint(100000, 999999)}-{rng.choice(PEOPLE)}-RENT",
        f"BIL/ONL/{ref[:10]}/{rng.choice(['BESCOM','AIRTEL','JIO','TATA POWER'])}",
    ])


def _cc_desc(rng: random.Random, direction: str) -> str:
    m = rng.choice(MERCHANTS)
    if direction == "credit":
        return rng.choice([f"REFUND {m} {rng.choice(CITIES)}", "FUEL SURCHARGE WAIVER", f"CASHBACK {m}"])
    return rng.choice([
        f"{m} {rng.choice(CITIES)} IN",
        f"{m} PVT LTD {rng.choice(CITIES)}",
        f"POS {m} {rng.choice(CITIES)} IND",
        f"{m}*ORDER {rng.randint(1000000, 9999999)} {rng.choice(CITIES)}",
    ])


def make_statement(profile: Profile, rng: random.Random, n_txns: int) -> SynthStatement:
    start = date(2023, 1, 1) + timedelta(days=rng.randint(0, 3 * 365))
    span = rng.choice([30, 30, 31, 60, 90]) if profile.statement_type == "bank_account" else 30
    end = start + timedelta(days=span - 1)
    days = sorted(start + timedelta(days=rng.randint(0, span - 1)) for _ in range(n_txns))
    cc = profile.statement_type == "credit_card"
    txns: list[SynthTxn] = []
    if cc:
        opening = q(rng.uniform(0, 60000))
        owed = opening
        pay_day = start + timedelta(days=rng.randint(2, 12))
        paid = False
        for d in days:
            if not paid and d >= pay_day and opening > 0:
                amt = opening if rng.random() < 0.7 else q(float(opening) * rng.uniform(0.2, 0.9))
                txns.append(SynthTxn(d, "PAYMENT RECEIVED - THANK YOU" if rng.random() < 0.5 else "NEFT PAYMENT RECEIVED", amt, "credit"))
                owed -= amt
                paid = True
                continue
            if rng.random() < 0.08:
                amt = q(rng.uniform(20, 3000))
                direction = "credit"
                desc = _cc_desc(rng, "credit")
            elif rng.random() < 0.05:
                amt = q(rng.uniform(10, 400))
                direction = "debit"
                desc = rng.choice(["IGST @18%", "LATE PAYMENT FEE", "FINANCE CHARGES", "ANNUAL FEE"])
            else:
                amt = q(min(rng.lognormvariate(6.8, 1.1), 90000))
                direction = "debit"
                desc = _cc_desc(rng, "debit")
            if direction == "credit" and owed - amt < -5000:
                direction, desc = "debit", _cc_desc(rng, "debit")
            owed += amt if direction == "debit" else -amt
            txns.append(SynthTxn(d, desc, amt, direction, points=str(int(amt) // 100) if direction == "debit" else ""))
        closing = owed
        min_due = q(max(float(closing) * 0.05, 0)) if closing > 0 else Decimal("0.00")
        due = end + timedelta(days=18)
    else:
        opening = q(rng.uniform(5000, 300000))
        bal = opening
        salary_day = start + timedelta(days=rng.randint(0, min(5, span - 1)))
        got_salary = False
        for d in days:
            if not got_salary and d >= salary_day:
                amt, direction, big = q(rng.uniform(40000, 180000)), "credit", True
                got_salary = True
            elif rng.random() < 0.22:
                amt, direction, big = q(min(rng.lognormvariate(7.5, 1.2), 200000)), "credit", False
            else:
                amt, direction, big = q(min(rng.lognormvariate(6.5, 1.3), 150000)), "debit", False
                if rng.random() < 0.3:
                    amt = Decimal(rng.choice([100, 200, 250, 500, 1000, 2000, 5000]))
                    amt = amt.quantize(Decimal("0.01"))
            if direction == "debit" and bal - amt < 100:
                direction = "credit"
            bal += amt if direction == "credit" else -amt
            desc = _bank_desc(rng, direction, big)
            if profile.desc_prefix:
                desc = ("DEP TFR " if direction == "credit" else "WDL TFR ") + desc
            txns.append(SynthTxn(d, desc, amt, direction, bal,
                                 ref=rng.choice(["", f"{rng.randint(100000, 999999)}", f"UPI-{rng.randint(10**9, 10**10 - 1)}", f"{rng.randint(1000, 9999)}"])))
        closing = bal
        min_due, due = None, None
    if profile.eod_balance:
        for i, t in enumerate(txns):
            if i + 1 < len(txns) and txns[i + 1].date == t.date:
                t.balance = None
    if profile.card_sections and len(txns) > 4:
        for t in txns:
            if rng.random() < 0.35 and "PAYMENT" not in t.description:
                t.section = 1
    acct = (f"XXXX XXXX XXXX {rng.randint(1000, 9999)}" if cc else f"XXXXXXXX{rng.randint(1000, 9999)}")
    return SynthStatement(
        profile=profile, opening=opening, closing=closing, txns=txns, period_start=start, period_end=end,
        account_number=acct, holder=rng.choice(PEOPLE), date_fmt=rng.choice(profile.date_formats),
        grouping=profile.grouping, min_due=min_due, due_date=due,
        sections=["Primary Card : XXXX XXXX XXXX " + acct[-4:], "Add-on Card : XXXX XXXX XXXX " + str(rng.randint(1000, 9999))]
        if profile.card_sections else [],
    )


# ---------------------------------------------------------------------------
# Cell values
# ---------------------------------------------------------------------------


def _amount_cells(st: SynthStatement, t: SynthTxn) -> dict[str, str]:
    p, g = st.profile, st.grouping
    cc = p.statement_type == "credit_card"
    v = fmt_num(t.amount, g)
    out = {"debit": "", "credit": "", "amount": "", "drcr": ""}
    if p.amount_style == "dc":
        out[t.direction] = v
        if p.dash_empty:
            out["credit" if t.direction == "debit" else "debit"] = "-"
        if p.zero_fill:
            other = "credit" if t.direction == "debit" else "debit"
            out[other] = "0.00"
    elif p.amount_style == "signed":
        neg = (t.direction == "credit") if cc else (t.direction == "debit")
        out["amount"] = ("-" + v) if neg else v
    elif p.amount_style == "suffix":
        out["amount"] = f"{v} {'CR' if t.direction == 'credit' else 'DR'}"
    elif p.amount_style == "cr_only":
        out["amount"] = f"{v} Cr" if t.direction == "credit" else v
    elif p.amount_style == "drcr_col":
        out["amount"] = v
        out["drcr"] = "Cr" if t.direction == "credit" else "Dr"
    return out


def fmt_balance(st: SynthStatement, b: Optional[Decimal]) -> str:
    if b is None:
        return ""
    v = fmt_num(b, st.grouping)
    if st.profile.balance_suffix:
        return f"{v} {'Cr' if b >= 0 else 'Dr'}"
    return ("-" + v) if b < 0 else v


def row_cells(st: SynthStatement, t: SynthTxn, serial: int) -> dict[str, str]:
    cells = {
        "date": t.date.strftime(st.date_fmt),
        "vdate": t.date.strftime(st.date_fmt),
        "post_date": (t.date + timedelta(days=1)).strftime(st.date_fmt),
        "desc": t.description,
        "ref": t.ref or ("-" if st.profile.dash_empty else ""),
        "balance": fmt_balance(st, t.balance),
        "serial": str(serial),
        "points": t.points,
    }
    cells.update(_amount_cells(st, t))
    return cells


def summary_values(st: SynthStatement) -> list[tuple[str, str, str]]:
    """(kind, label, printed value) for the profile's summary."""
    txns = st.txns
    vals = {
        "opening_balance": fmt_num(st.opening, st.grouping) if st.opening >= 0 else fmt_num(st.opening, st.grouping) + " Cr",
        "closing_balance": fmt_num(st.closing, st.grouping) if st.closing >= 0 else fmt_num(st.closing, st.grouping) + " Cr",
        "total_debits": fmt_num(sum((t.amount for t in txns if t.direction == "debit"), Decimal(0)), st.grouping),
        "total_credits": fmt_num(sum((t.amount for t in txns if t.direction == "credit"), Decimal(0)), st.grouping),
        "debit_count": str(sum(1 for t in txns if t.direction == "debit")),
        "credit_count": str(sum(1 for t in txns if t.direction == "credit")),
    }
    if st.profile.statement_type == "bank_account":
        for k in ("opening_balance", "closing_balance"):
            b = st.opening if k == "opening_balance" else st.closing
            vals[k] = fmt_balance(st, b) + st.profile.summary_balance_suffix
    return [(k, label, vals[k]) for k, label in st.profile.labels.items()]


def display_order(st: SynthStatement) -> list[SynthTxn]:
    txns = list(st.txns)
    if st.profile.card_sections:
        txns = [t for t in txns if t.section == 0] + [t for t in txns if t.section == 1]
    if st.profile.newest_first:
        txns.reverse()
    return txns


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def render_pdf(st: SynthStatement, path: Path, rng: random.Random, password: Optional[str] = None) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    p = st.profile
    W, H = A4
    font = rng.choice(["Helvetica", "Helvetica", "Times-Roman"])
    bold = {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold"}[font]
    fs = round(rng.uniform(7.4, 8.8), 1)
    lh = fs * 1.3
    row_pad = lh * (0.55 if p.middle_aligned else rng.choice([0.0, 0.15, 0.3]))
    margin = 34
    rel = [c.width * rng.uniform(0.94, 1.06) for c in p.columns]
    scale = (W - 2 * margin) / sum(rel)
    widths = [r * scale for r in rel]
    xs = [margin + sum(widths[:i]) for i in range(len(widths))]
    num_header_right = rng.random() < 0.5
    # a header wider than its column wraps onto two lines, as banks print it
    headers: list[tuple[str, ...]] = []
    for col, w in zip(p.columns, widths):
        h = col.header
        if len(h) == 1 and stringWidth(h[0], bold, fs) > w - 6 and " " in h[0]:
            words = h[0].split(" ")
            cut = max(1, len(words) // 2)
            h = (" ".join(words[:cut]), " ".join(words[cut:]))
        headers.append(h)
    two_line = any(len(h) > 1 for h in headers)

    c = canvas.Canvas(str(path), pagesize=A4, encrypt=password) if password else canvas.Canvas(str(path), pagesize=A4)
    state = {"y": H - margin, "page": 1}

    def text(x, s, f=font, size=fs, right=False):
        c.setFont(f, size)
        if right:
            c.drawRightString(x, state["y"], s)
        else:
            c.drawString(x, state["y"], s)

    def newline(mult=1.0):
        state["y"] -= lh * mult

    def kv_block(items):
        # one or two "Label : value" pairs per line
        per_line = 2 if (len(items) > 2 and rng.random() < 0.5) else 1
        for i in range(0, len(items), per_line):
            for j, (label, val) in enumerate(items[i : i + per_line]):
                x = margin + j * (W - 2 * margin) / 2
                text(x, f"{label} :")
                text(x + 140, val, right=False)
            newline()

    def grid_block(items):
        n = len(items)
        cw = (W - 2 * margin) / n
        for j, (label, _v) in enumerate(items):
            text(margin + j * cw + cw - 8, label, f=bold, right=True)
        newline()
        for j, (_l, val) in enumerate(items):
            text(margin + j * cw + cw - 8, val, right=True)
        newline(1.6)

    def summary_block():
        items = [(label, val) for _k, label, val in summary_values(st)]
        if not items:
            return
        newline(0.5)
        if p.summary.startswith("kv"):
            kv_block(items)
        else:
            grid_block(items)
        if p.statement_type == "credit_card":
            kv_block([("Minimum Amount Due", fmt_num(st.min_due or Decimal(0), st.grouping)),
                      ("Payment Due Date", st.due_date.strftime(st.date_fmt))])
        newline(0.5)

    def ensure_room(lines: float = 1.0):
        """Plain page break inside the preamble (no table header yet)."""
        if state["y"] - lh * lines < margin + 20:
            footer()
            c.showPage()
            state["page"] += 1
            state["y"] = H - margin
            page_top(False)

    def preamble():
        """~1.5 pages of account information, with traps for a layout reader: a summary
        table with amounts, a standing-instruction table with dated amount rows, a
        'Balance as on <date>' line, and long notices."""
        g = st.grouping
        def kv(label, value):
            ensure_room()
            text(margin, label)
            text(margin + 150, f": {value}")
            newline()
        def heading(t):
            ensure_room(3)
            newline(0.6)
            text(margin, t, f=bold, size=fs + 1)
            newline(1.4)
        heading("Account Information")
        for label, value in [
            ("Account Name", st.holder), ("Account Type", "REGULAR SAVINGS ACCOUNT"),
            ("Branch", f"{rng.choice(CITIES).title()} Main Branch"), ("Branch Code", str(rng.randint(1000, 99999))),
            ("IFSC Code", f"ASHK000{rng.randint(1000, 9999)}"), ("MICR Code", str(rng.randint(10**8, 10**9 - 1))),
            ("CIF Number", f"XXXXXXX{rng.randint(1000, 9999)}"), ("Date of Opening", (st.period_start - timedelta(days=rng.randint(400, 4000))).strftime(st.date_fmt)),
            ("Interest Rate (% p.a.)", "2.70"), ("Drawing Power", "0.00"), ("MOD Balance", "0.00"),
            ("Nominee Registered", "Yes"), ("CKYC Number", f"XXXXXXXXXX{rng.randint(1000, 9999)}"),
            (f"Balance as on {st.period_start.strftime(st.date_fmt)}", fmt_balance(st, st.opening)),
        ]:
            kv(label, value)
        heading("Relationship Summary")
        cols = [margin, margin + 160, margin + 300, W - margin]
        ensure_room(5)
        for x, h in zip(cols[:3], ["Product", "Number of Accounts", "Currency"]):
            text(x, h, f=bold)
        text(cols[3], "Balance (INR)", f=bold, right=True)
        newline()
        for prod, code, bal in [("Transaction Accounts", "SB ( 01 )", st.closing), ("Current Accounts", "CA ( 00 )", Decimal(0)),
                                ("Term Deposits", "TD ( 00 )", Decimal(0)), ("Loans", "LN ( 00 )", Decimal(0))]:
            ensure_room()
            text(cols[0], prod); text(cols[1], code); text(cols[2], "INR")
            text(cols[3], fmt_num(bal, g), right=True)
            newline()
        heading("Standing Instructions")
        ensure_room(4)
        for x, h in zip([margin, margin + 90, margin + 180, margin + 330], ["Start Date", "End Date", "Beneficiary", "Frequency"]):
            text(x, h, f=bold)
        text(W - margin, "Amount", f=bold, right=True)
        newline()
        for _ in range(2):
            ensure_room()
            sd = st.period_start - timedelta(days=rng.randint(30, 300))
            text(margin, sd.strftime(st.date_fmt)); text(margin + 90, (sd + timedelta(days=365)).strftime(st.date_fmt))
            text(margin + 180, rng.choice(["LIC PREMIUM", "PPF ACCOUNT", "RD ACCOUNT"])); text(margin + 330, "Monthly")
            text(W - margin, fmt_num(Decimal(rng.choice([1000, 2500, 5000])), g), right=True)
            newline()
        heading("Important Information")
        notices = [
            "Customers are requested to update their KYC documents periodically to avoid restrictions on the account.",
            "Never share your PIN, OTP, password or CVV with anyone, including bank staff. The bank never asks for them.",
            "Interest on savings accounts is calculated on the daily closing balance and credited quarterly.",
            "Please report any unauthorised transaction immediately to reduce your liability under RBI guidelines.",
            "Nomination facility is available on all deposit accounts. Contact your branch to register or modify a nominee.",
            "Cheques deposited after the cut-off time will be sent for clearing on the next working day.",
            "Charges for SMS alerts, debit card maintenance and cash transactions are as per the schedule of charges.",
            "Positive Pay System is mandatory for cheques of value above the threshold notified by the bank.",
        ]
        for _ in range(rng.randint(6, 9)):
            for line in notices:
                ensure_room()
                text(margin, line)
                newline()
        heading("Statement of Transactions")

    def page_top(first: bool):
        if first:
            text(margin, p.bank_name, f=bold, size=fs + 6)
            newline(2)
            text(margin, f"{st.holder}")
            newline()
            text(margin, f"{rng.randint(1, 999)}, {rng.choice(['MG Road', 'Park Street', 'Linking Road', 'Anna Salai'])}, {rng.choice(CITIES).title()} - {rng.randint(110001, 600099)}")
            newline(1.5)
            text(margin, f"{'Card Number' if p.statement_type == 'credit_card' else 'Account Number'} : {st.account_number}")
            newline()
            if p.period_label:
                text(margin, f"{p.period_label} : {st.period_start.strftime(st.date_fmt)} to {st.period_end.strftime(st.date_fmt)}")
                newline()
            text(margin, f"Currency : {p.currency}")
            newline(1.5)
            for noise in p.extra_noise:
                text(margin, noise)
                newline(1.5)
            if p.preamble:
                preamble()
            if p.summary.endswith("top"):
                summary_block()
        else:
            text(margin, f"{p.bank_name} - Statement of Account", f=font, size=fs)
            text(W - margin, f"Page {state['page']}", right=True)
            newline(2)

    def table_header():
        if p.header_text_only is not None:
            # the header is a coloured banner: only some words exist as text
            c.setFillColorRGB(0.12, 0.29, 0.53)
            c.rect(margin, state["y"] - 3, W - 2 * margin, lh + 2, stroke=0, fill=1)
            c.setFillColorRGB(0, 0, 0)
            for col, x, w in zip(p.columns, xs, widths):
                if col.header[0] in p.header_text_only:
                    text(x + w - 2, col.header[0], f=bold, right=True)
            newline(1.6)
            return
        for line_no in range(2 if two_line else 1):
            for col, hdr, x, w in zip(p.columns, headers, xs, widths):
                if len(hdr) == 1 and line_no == 1:
                    continue
                if len(hdr) == 2 or line_no == 0:
                    label = hdr[line_no] if len(hdr) == 2 else hdr[0]
                    if col.align == "r" and num_header_right:
                        text(x + w - 2, label, f=bold, right=True)
                    else:
                        text(x + 2, label, f=bold)
            newline()
        newline(0.3)

    def draw_row(cells: dict[str, str]):
        desc_col = next(i for i, col in enumerate(p.columns) if col.field == "desc")
        desc_lines = wrap(cells.get("desc", ""), widths[desc_col] - 6)
        if p.desc_prefix and cells.get("desc", "").startswith(("WDL TFR ", "DEP TFR ")):
            # "WDL TFR" sits on its own line above the rest of the narration
            desc_lines = [cells["desc"][:7]] + wrap(cells["desc"][8:], widths[desc_col] - 6)
        n = max(1, len(desc_lines))
        mid = (n - 1) / 2 if p.middle_aligned else 0
        if p.date_offset is not None and n > 1:
            mid = p.date_offset
        top_y = state["y"]
        for k, dl in enumerate(desc_lines):
            state["y"] = top_y - k * lh
            text(xs[desc_col] + 2, dl)
        state["y"] = top_y - mid * lh
        for i, (col, x, w) in enumerate(zip(p.columns, xs, widths)):
            if col.field == "desc":
                continue
            v = cells.get(col.field, "")
            if not v:
                continue
            if col.align == "r":
                text(x + w - 2, v, right=True)
            else:
                text(x + 2, v)
        state["y"] = top_y - n * lh - row_pad

    def wrap(s: str, width: float) -> list[str]:
        words, lines, cur = s.split(" "), [], ""
        for w in words:
            cand = (cur + " " + w).strip()
            if stringWidth(cand, font, fs) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            while stringWidth(w, font, fs) > width:  # hard-break very long tokens
                k = len(w)
                while k > 1 and stringWidth(w[:k], font, fs) > width:
                    k -= 1
                lines.append(w[:k])
                w = w[k:]
            cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def footer():
        y = state["y"]
        state["y"] = margin - 8
        text(margin, "This is a computer generated statement and does not require a signature.", size=fs - 1)
        text(W - margin, f"Page {state['page']}", size=fs - 1, right=True)
        state["y"] = y

    def new_page(carry_balance: Optional[Decimal]):
        if p.page_carry and carry_balance is not None:
            draw_row({"desc": p.page_carry[0], "balance": fmt_balance(st, carry_balance)})
        footer()
        c.showPage()
        state["page"] += 1
        state["y"] = H - margin
        page_top(False)
        if p.repeat_header:
            table_header()
        if p.page_carry and carry_balance is not None:
            draw_row({"desc": p.page_carry[1], "balance": fmt_balance(st, carry_balance)})

    page_top(True)
    rows = display_order(st)
    section = None
    table_header_drawn = False
    last_balance: Optional[Decimal] = st.opening
    if p.opening_row:
        table_header()
        table_header_drawn = True
        draw_row({"date": st.period_start.strftime(st.date_fmt), "desc": p.opening_row, "balance": fmt_balance(st, st.opening)})
    for i, t in enumerate(rows, start=1):
        if p.card_sections and t.section != section:
            section = t.section
            newline(0.6)
            text(margin, st.sections[section], f=bold)
            newline(1.4)
            table_header()
            table_header_drawn = True
        elif not table_header_drawn:
            table_header()
            table_header_drawn = True
        needed = lh * (len(wrap(t.description, 100)) + 2)
        if state["y"] - needed < margin + 20:
            new_page(last_balance)
        draw_row(row_cells(st, t, i))
        if t.balance is not None:
            last_balance = t.balance
    if p.closing_row:
        draw_row({"date": st.period_end.strftime(st.date_fmt), "desc": p.closing_row, "balance": fmt_balance(st, st.closing)})
    if p.total_row:
        sums = {d: fmt_num(sum((t.amount for t in st.txns if t.direction == d), Decimal(0)), st.grouping) for d in ("debit", "credit")}
        draw_row({"desc": p.total_row, "debit": sums["debit"], "credit": sums["credit"]})
    if p.summary.endswith("bottom"):
        if state["y"] < margin + 120:
            new_page(None)
        newline()
        summary_block()
    newline()
    text(margin, "Please examine this statement. If no discrepancy is reported within 30 days it will be deemed correct.", size=fs - 1)
    newline()
    text(margin, f"For queries call 1800 {rng.randint(100, 999)} {rng.randint(1000, 9999)} or write to care@{p.key.split('_')[0]}.example", size=fs - 1)
    footer()
    c.save()


# ---------------------------------------------------------------------------
# CSV / XLSX
# ---------------------------------------------------------------------------


def table_rows(st: SynthStatement, numeric: bool) -> list[list]:
    p = st.profile
    rows: list[list] = [[p.bank_name], [f"Account Number : {st.account_number}"]]
    if p.period_label:
        rows.append([f"{p.period_label} : {st.period_start.strftime(st.date_fmt)} to {st.period_end.strftime(st.date_fmt)}"])
    rows.append([])
    rows.append([" ".join(c.header) for c in p.columns])
    if p.opening_row:
        rows.append([{"date": st.period_start.strftime(st.date_fmt), "desc": p.opening_row, "balance": fmt_balance(st, st.opening)}.get(c.field, "") for c in p.columns])
    for i, t in enumerate(display_order(st), start=1):
        cells = row_cells(st, t, i)
        row = []
        for col in p.columns:
            v = cells.get(col.field, "")
            if numeric and col.field in ("debit", "credit", "balance") and v and p.amount_style == "dc" and not p.balance_suffix:
                v = float(v.replace(",", ""))
            row.append(v)
        rows.append(row)
    if p.closing_row:
        rows.append([{"date": st.period_end.strftime(st.date_fmt), "desc": p.closing_row, "balance": fmt_balance(st, st.closing)}.get(c.field, "") for c in p.columns])
    sv = summary_values(st)
    if sv:
        rows.append([])
        rows.append([label for _k, label, _v in sv])
        rows.append([v for _k, _l, v in sv])
    return rows


def render_csv(st: SynthStatement, path: Path) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in table_rows(st, numeric=False):
        w.writerow(r)
    path.write_text(buf.getvalue())


def render_xlsx(st: SynthStatement, path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Statement"
    for r in table_rows(st, numeric=True):
        ws.append(r)
    wb.save(path)


# ---------------------------------------------------------------------------
# Truth + oracle spec
# ---------------------------------------------------------------------------


def truth(st: SynthStatement) -> dict:
    p = st.profile
    rows = display_order(st)
    return {
        "profile": p.key,
        "statement_type": p.statement_type,
        "bank_name": p.bank_name,
        "account_number": st.account_number,
        "period_start": st.period_start.isoformat(),
        "period_end": st.period_end.isoformat(),
        "opening_balance": f"{st.opening:.2f}",
        "closing_balance": f"{st.closing:.2f}",
        "total_debits": f"{sum((t.amount for t in st.txns if t.direction == 'debit'), Decimal(0)):.2f}",
        "total_credits": f"{sum((t.amount for t in st.txns if t.direction == 'credit'), Decimal(0)):.2f}",
        "transactions": [
            {
                "date": t.date.isoformat(),
                "description": t.description,
                "amount": f"{t.amount:.2f}",
                "direction": t.direction,
                "balance": None if t.balance is None else f"{t.balance:.2f}",
            }
            for t in rows
        ],
    }


def oracle_spec(st: SynthStatement, fmt: str) -> LayoutSpec:
    p = st.profile
    fields = [FieldSpec(kind, label, value) for kind, label, value in summary_values(st)]
    if p.period_label:
        fields.append(FieldSpec("period", p.period_label, f"{st.period_start.strftime(st.date_fmt)} to {st.period_end.strftime(st.date_fmt)}"))
    fields.append(FieldSpec("account_number", "Card Number" if p.statement_type == "credit_card" else "Account Number", st.account_number))
    if p.statement_type == "credit_card" and fmt == "pdf":
        fields.append(FieldSpec("minimum_amount_due", "Minimum Amount Due", fmt_num(st.min_due or Decimal(0), st.grouping)))
    markers = [m for m in (p.opening_row, p.closing_row, p.total_row, *(p.page_carry or ())) if m]
    return LayoutSpec(
        statement_type=p.statement_type,
        columns=[ColumnSpec(" ".join(c.header), c.role or ROLE_OF[c.field]) for c in p.columns],
        bank_name=p.bank_name,
        currency=p.currency,
        fields=fields,
        non_transaction_rows=markers,
    )


def generate_corpus(
    out_dir: Path,
    per_profile: int = 6,
    seed: int = 7,
    profiles: Optional[list[str]] = None,
    min_txns: int = 8,
    max_txns: int = 160,
    password_rate: float = 0.15,
) -> list[dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    manifest = []
    for p in PROFILES:
        if profiles and p.key not in profiles:
            continue
        for k in range(per_profile):
            fmt = p.formats[k % len(p.formats)]
            lo, hi = p.txn_range or (min_txns, max_txns if fmt == "pdf" else max_txns * 2)
            n = rng.randint(lo, hi)
            st = make_statement(p, rng, n)
            cid = f"{p.key}_{k:03d}"
            ext = {"pdf": ".pdf", "csv": ".csv", "xlsx": ".xlsx"}[fmt]
            path = out_dir / (cid + ext)
            password = PASSWORD if (fmt == "pdf" and rng.random() < password_rate) else None
            if fmt == "pdf":
                render_pdf(st, path, rng, password)
            elif fmt == "csv":
                render_csv(st, path)
            else:
                render_xlsx(st, path)
            (out_dir / f"{cid}.truth.json").write_text(json.dumps(truth(st), indent=1))
            (out_dir / f"{cid}.spec.json").write_text(json.dumps(oracle_spec(st, fmt).to_dict(), indent=1))
            manifest.append({"id": cid, "profile": p.key, "format": fmt, "file": path.name, "password": password, "n_txns": len(st.txns)})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest
