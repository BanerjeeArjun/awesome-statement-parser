"""Stage 4 — the reconciliation harness. Pure functions; never edits data, only judges it.

A parse is VERIFIED only when the extracted rows are tied, with exact paisa arithmetic, to
numbers the bank printed independently of those rows:

  E1  opening + Σcredits − Σdebits == closing   (credit cards: opening + Σdebits − Σcredits)
      using a printed opening AND a printed closing balance;
  E2  an unbroken running-balance chain that starts at a printed opening balance (or B/F
      row) and covers every transaction — any missing, extra, mis-signed or mis-read row
      breaks the chain at that exact row;
  E3  Σdebits and Σcredits both equal the printed totals.

and nothing contradicts it: no balance-chain break, no printed total/count mismatch, no
unparsed row, no money-bearing line inside the table left unaccounted for.

  verified      — at least one of E1/E2/E3 holds and no check failed
  consistent    — no check failed, but nothing printed anchors the rows (e.g. a CSV export
                  with a balance column and no opening balance): internally coherent only
  needs_review  — a check failed, or there is nothing to reconcile against
  failed        — no transactions could be extracted
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .models import Anchor, Check, StatementInfo, Transaction, UnaccountedLine, ValidationReport


def signed(t: Transaction, statement_type: str) -> Optional[Decimal]:
    """Movement of the account balance caused by a transaction."""
    if t.amount is None or t.direction is None:
        return None
    if statement_type == "credit_card":  # balance = amount owed
        return t.amount if t.direction == "debit" else -t.amount
    return t.amount if t.direction == "credit" else -t.amount


def is_newest_first(txns: list[Transaction]) -> bool:
    dated = [t.date for t in txns if t.date]
    if len(dated) < 2:
        return False
    inc = sum(1 for a, b in zip(dated, dated[1:]) if b > a)
    dec = sum(1 for a, b in zip(dated, dated[1:]) if b < a)
    return dec > inc


def _money(v: Optional[Decimal]) -> str:
    return "—" if v is None else f"{v:,.2f}"


def validate(
    info: StatementInfo,
    txns: list[Transaction],
    anchors: list[Anchor] | None = None,
    unaccounted: list[UnaccountedLine] | None = None,
    outside: list[UnaccountedLine] | None = None,
    today: Optional[date] = None,
    extra_checks: list[Check] | None = None,
) -> ValidationReport:
    anchors = anchors or []
    unaccounted = unaccounted or []
    outside = outside or []
    today = today or date.today()
    st = info.statement_type
    checks: list[Check] = []

    if not txns:
        checks.append(Check("transactions_found", "fail", "No transactions were extracted."))
        return ValidationReport("failed", checks, "No transactions found.")
    checks.append(Check("transactions_found", "pass", f"{len(txns)} transactions extracted."))

    # ---- every row fully parsed
    bad = [t.id for t in txns if t.date is None or t.amount is None or t.direction is None]
    zero = [t.id for t in txns if t.amount is not None and t.amount == 0]
    if bad:
        checks.append(Check("rows_parsed", "fail", f"{len(bad)} row(s) have a missing date, amount or debit/credit direction.", bad))
    else:
        checks.append(Check("rows_parsed", "pass", "Every row has a date, an amount and a direction."))
    if zero:
        checks.append(Check("zero_amounts", "warn", f"{len(zero)} row(s) have a zero amount.", zero))

    newest_first = is_newest_first(txns)
    chrono = list(reversed(txns)) if newest_first else list(txns)

    # ---- opening / closing resolution
    opening, opening_src = info.opening_balance, ("summary" if info.opening_balance is not None else None)
    closing, closing_src = info.closing_balance, ("summary" if info.closing_balance is not None else None)
    first_id, last_id = txns[0].id, txns[-1].id
    if opening is None:
        for a in anchors:
            at_start = (a.after_txn is None) if not newest_first else (a.after_txn == last_id)
            if a.kind == "opening" and at_start and a.balance is not None:
                opening, opening_src = a.balance, "table"
                break
    if closing is None:
        for a in reversed(anchors):
            at_end = (a.after_txn == last_id) if not newest_first else (a.after_txn is None)
            if a.kind == "closing" and at_end and a.balance is not None:
                closing, closing_src = a.balance, "table"
                break
    if opening is None:
        first_bal = next((t for t in chrono if t.balance is not None), None)
        if first_bal is chrono[0] and signed(first_bal, st) is not None:
            opening, opening_src = first_bal.balance - signed(first_bal, st), "derived"
    if closing is None and chrono[-1].balance is not None:
        closing, closing_src = chrono[-1].balance, "derived"

    # ---- E2: running balance chain
    with_bal = [t for t in chrono if t.balance is not None]
    chain_ok = False
    chain_full = False
    if len(with_bal) >= 1 and (opening is not None or len(with_bal) >= 2):
        breaks = []
        unknown = []
        running = opening if opening_src in ("summary", "table") else None
        covered_from_start = running is not None
        for t in chrono:
            s = signed(t, st)
            if running is not None:
                if s is None:
                    unknown.append(t.id)
                    running = None
                else:
                    running = running + s
            if t.balance is not None:
                if running is not None and running != t.balance:
                    breaks.append(
                        {"row": t.id, "expected": str(running), "printed": str(t.balance), "difference": str(t.balance - running)}
                    )
                running = t.balance
        # B/F, C/F rows printed inside the table are checkpoints too
        if not newest_first:
            bal_after: dict[int, Decimal] = {}
            run = opening
            for t in txns:
                s = signed(t, st)
                run = t.balance if t.balance is not None else (run + s if run is not None and s is not None else None)
                if run is not None:
                    bal_after[t.id] = run
            for a in anchors:
                if a.balance is None or a.after_txn is None or a.kind == "total":
                    continue
                exp = bal_after.get(a.after_txn)
                if exp is not None and exp != a.balance:
                    breaks.append({"row": a.after_txn, "expected": str(exp), "printed": str(a.balance), "difference": str(a.balance - exp), "anchor": a.text})
        rows = sorted({b["row"] for b in breaks} | set(unknown))
        last_has_balance = chrono[-1].balance is not None
        chain_full = covered_from_start and last_has_balance
        if breaks:
            first = breaks[0]
            checks.append(
                Check(
                    "balance_chain",
                    "fail",
                    f"Running balance breaks at {len(breaks)} point(s); first at row {first['row']} "
                    f"(expected {_money(Decimal(first['expected']))}, statement shows {_money(Decimal(first['printed']))}, "
                    f"gap {_money(Decimal(first['difference']))}).",
                    rows,
                    {"breaks": breaks[:50], "newest_first": newest_first},
                )
            )
        elif unknown:
            checks.append(Check("balance_chain", "fail", "Balance chain interrupted by rows without a direction.", rows))
        else:
            chain_ok = True
            scope = "from the opening balance through every row" if chain_full else (
                "between consecutive printed balances (the first rows are not anchored to an opening balance)"
            )
            checks.append(
                Check("balance_chain", "pass", f"Running balance reconciles exactly {scope}.", [], {"newest_first": newest_first, "anchored": chain_full})
            )
    else:
        checks.append(Check("balance_chain", "skip", "The statement prints no running balance."))

    # ---- totals
    sum_dr = sum((t.amount for t in txns if t.direction == "debit" and t.amount is not None), Decimal(0))
    sum_cr = sum((t.amount for t in txns if t.direction == "credit" and t.amount is not None), Decimal(0))
    net = sum((signed(t, st) or Decimal(0)) for t in txns)

    total_dr, total_cr = info.total_debits, info.total_credits
    totals_rows = [a for a in anchors if a.kind == "total" and (a.debit_total is not None or a.credit_total is not None)]
    if len(totals_rows) == 1:
        total_dr = total_dr if total_dr is not None else totals_rows[0].debit_total
        total_cr = total_cr if total_cr is not None else totals_rows[0].credit_total

    # ---- E1: opening + net == closing
    e1 = False
    anchored_ends = opening_src in ("summary", "table") and closing_src in ("summary", "table")
    if opening is not None and closing is not None:
        expected = opening + net
        sign_note = "+ debits − credits" if st == "credit_card" else "+ credits − debits"
        if expected == closing:
            status = "pass"
            e1 = anchored_ends
            msg = f"Opening {_money(opening)} {sign_note} = {_money(expected)} matches closing {_money(closing)}."
            if not anchored_ends:
                msg += f" (opening is {opening_src}, closing is {closing_src} — not independent of the rows)"
        else:
            status = "fail"
            msg = (
                f"Opening {_money(opening)} {sign_note} = {_money(expected)}, but the closing balance is "
                f"{_money(closing)} (gap {_money(closing - expected)})."
            )
        checks.append(
            Check(
                "opening_closing",
                status,
                msg,
                [],
                {"opening": str(opening), "opening_source": opening_src, "closing": str(closing), "closing_source": closing_src, "computed": str(expected)},
            )
        )
    else:
        checks.append(Check("opening_closing", "skip", "Opening and/or closing balance not available."))

    def total_check(name, label, computed, printed):
        if printed is None:
            return None
        if computed == printed:
            return Check(name, "pass", f"Σ {label} {_money(computed)} matches the printed total.", [], {"computed": str(computed), "printed": str(printed)})
        status = "warn" if e1 else "fail"
        note = " The opening→closing equation holds, so the bank likely totals this differently." if e1 else ""
        return Check(name, status, f"Σ {label} {_money(computed)} ≠ printed total {_money(printed)} (gap {_money(printed - computed)}).{note}", [], {"computed": str(computed), "printed": str(printed)})

    e3_parts = []
    for c in (total_check("total_debits", "debits", sum_dr, total_dr), total_check("total_credits", "credits", sum_cr, total_cr)):
        if c:
            checks.append(c)
            e3_parts.append(c.status == "pass")
    e3 = len(e3_parts) == 2 and all(e3_parts)

    # ---- counts
    n_dr = sum(1 for t in txns if t.direction == "debit")
    n_cr = sum(1 for t in txns if t.direction == "credit")
    for name, label, n, printed in (("debit_count", "debits", n_dr, info.debit_count), ("credit_count", "credits", n_cr, info.credit_count)):
        if printed is not None:
            ok = n == printed
            checks.append(Check(name, "pass" if ok else "fail", f"{n} {label} extracted; statement says {printed}.", [], {"computed": n, "printed": printed}))

    # ---- coverage: every money line inside the table must be accounted for
    if unaccounted:
        checks.append(
            Check(
                "coverage",
                "fail",
                f"{len(unaccounted)} line(s) inside the transaction table carry an amount but were not matched to a transaction.",
                [],
                {"lines": [{"line_id": u.line_id, "page": u.page, "text": u.text, "reason": u.reason} for u in unaccounted[:50]]},
            )
        )
    else:
        checks.append(Check("coverage", "pass", "Every amount-bearing line in the transaction table is accounted for."))
    if outside:
        checks.append(
            Check(
                "outside_table",
                "warn",
                f"{len(outside)} amount-bearing line(s) outside the table were ignored (summaries, ads, footers).",
                [],
                {"lines": [{"line_id": u.line_id, "page": u.page, "text": u.text} for u in outside[:50]]},
            )
        )

    # ---- dates
    future = [t.id for t in txns if t.date and t.date > today + timedelta(days=1)]
    if future:
        checks.append(Check("future_dates", "fail", f"{len(future)} row(s) are dated in the future — likely a date-format misread.", future))
    if info.period_start and info.period_end:
        lo = info.period_start - timedelta(days=45 if st == "credit_card" else 3)
        hi = info.period_end + timedelta(days=3)
        out = [t.id for t in txns if t.date and not (lo <= t.date <= hi)]
        if out:
            checks.append(Check("dates_in_period", "warn", f"{len(out)} row(s) fall outside the statement period {info.period_start} – {info.period_end}.", out))
        else:
            checks.append(Check("dates_in_period", "pass", f"All rows fall inside the statement period {info.period_start} – {info.period_end}."))

    # ---- narrations: arithmetic cannot see them, but downstream categorisation lives on them
    wordless = [t.id for t in txns if not any(ch.isalpha() for ch in (t.description or ""))]
    if len(wordless) >= 0.6 * len(txns):
        checks.append(Check("descriptions", "fail", f"{len(wordless)} of {len(txns)} rows have no words in the narration — the narration column was probably misread.", wordless[:200]))
    elif wordless:
        checks.append(Check("descriptions", "warn", f"{len(wordless)} row(s) have no words in the narration.", wordless[:200]))
    else:
        checks.append(Check("descriptions", "pass", "Every row has a narration."))

    checks.extend(extra_checks or [])

    edited = [t.id for t in txns if t.edited]
    if edited:
        checks.append(Check("user_edits", "warn", f"{len(edited)} row(s) were edited by hand.", edited))

    # ---- verdict
    failed = [c for c in checks if c.status == "fail"]
    e2 = chain_ok and chain_full and (closing_src != "summary" or chrono[-1].balance == closing)
    evidence = [name for name, ok in (("opening→closing equation", e1), ("anchored balance chain", e2), ("printed debit/credit totals", e3)) if ok]
    if failed:
        verdict = "needs_review"
        summary = f"{len(failed)} check(s) failed: " + "; ".join(c.name for c in failed) + "."
    elif evidence:
        verdict = "verified"
        summary = "All rows reconcile against the statement (" + ", ".join(evidence) + ")."
    elif chain_ok:
        verdict = "consistent"
        summary = "Rows are internally consistent (running balance), but no printed opening/closing balance or totals anchor them."
    else:
        verdict = "needs_review"
        summary = "Nothing printed on the statement to reconcile against — please review the rows."
    return ValidationReport(verdict, checks, summary)
