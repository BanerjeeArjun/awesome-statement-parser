"""The reconciliation harness on hand-built ledgers, including adversarial mistakes."""
import copy
from datetime import date
from decimal import Decimal as D

from awesome_statement_parser.models import Anchor, StatementInfo, Transaction, UnaccountedLine
from awesome_statement_parser.validate import validate


def ledger(newest_first=False):
    rows = [
        (date(2024, 4, 1), "SALARY", "50000.00", "credit"),
        (date(2024, 4, 2), "RENT", "15000.00", "debit"),
        (date(2024, 4, 3), "GROCERIES", "2345.67", "debit"),
        (date(2024, 4, 5), "REFUND", "100.00", "credit"),
    ]
    bal = D("1000.00")
    txns = []
    for i, (d, desc, amt, direction) in enumerate(rows, start=1):
        bal += D(amt) if direction == "credit" else -D(amt)
        txns.append(Transaction(i, d, desc, D(amt), direction, bal))
    info = StatementInfo("bank_account", opening_balance=D("1000.00"), closing_balance=bal,
                         total_debits=D("17345.67"), total_credits=D("50100.00"))
    if newest_first:
        txns = list(reversed(txns))
        for i, t in enumerate(txns, start=1):
            t.id = i
    return info, txns


def check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_clean_ledger_verifies():
    info, txns = ledger()
    r = validate(info, txns)
    assert r.verdict == "verified", r.summary
    assert check(r, "balance_chain").status == "pass"
    assert check(r, "opening_closing").status == "pass"


def test_newest_first_verifies():
    info, txns = ledger(newest_first=True)
    assert validate(info, txns).verdict == "verified"


def test_dropped_row_is_caught():
    info, txns = ledger()
    del txns[2]
    r = validate(info, txns)
    assert r.verdict == "needs_review"
    assert check(r, "balance_chain").status == "fail"
    assert check(r, "opening_closing").status == "fail"


def test_invented_row_is_caught():
    info, txns = ledger()
    txns.insert(2, Transaction(99, date(2024, 4, 2), "PHANTOM", D("10.00"), "debit", None))
    assert validate(info, txns).verdict == "needs_review"


def test_flipped_direction_is_caught_at_that_row():
    info, txns = ledger()
    txns[1].direction = "credit"
    r = validate(info, txns)
    assert r.verdict == "needs_review"
    breaks = check(r, "balance_chain").details["breaks"]
    assert breaks[0]["row"] == 2


def test_misread_amount_is_caught():
    info, txns = ledger()
    txns[2].amount = D("2345.76")  # transposed digits
    assert validate(info, txns).verdict == "needs_review"


def test_printed_total_mismatch_fails_without_balances():
    info, txns = ledger()
    for t in txns:
        t.balance = None
    info.closing_balance = None
    info.total_debits = D("17000.00")
    r = validate(info, txns)
    assert check(r, "total_debits").status == "fail"
    assert r.verdict == "needs_review"


def test_unaccounted_money_line_blocks_verification():
    info, txns = ledger()
    u = UnaccountedLine("P1L9", 1, "1,234.00", "amount on a line with no date", (0, 0, 1, 1))
    r = validate(info, txns, unaccounted=[u])
    assert check(r, "coverage").status == "fail"
    assert r.verdict == "needs_review"


def test_unanchored_chain_is_only_consistent():
    info, txns = ledger()
    info = StatementInfo("bank_account")
    r = validate(info, txns)
    assert r.verdict == "consistent"


def test_end_of_day_balances_reconcile():
    info, txns = ledger()
    txns[0].balance = None  # only some rows print a balance
    txns[2].balance = None
    assert validate(info, txns).verdict == "verified"


def test_bf_row_checkpoint():
    info, txns = ledger()
    wrong = Anchor("carry", D("999.99"), "Balance B/F", "P2L1", after_txn=2)
    r = validate(info, txns, anchors=[wrong])
    assert check(r, "balance_chain").status == "fail"


def test_credit_card_sign_convention():
    txns = [
        Transaction(1, date(2024, 4, 2), "SHOP", D("500.00"), "debit"),
        Transaction(2, date(2024, 4, 3), "PAYMENT RECEIVED", D("1000.00"), "credit"),
        Transaction(3, date(2024, 4, 4), "FUEL", D("250.50"), "debit"),
    ]
    info = StatementInfo("credit_card", opening_balance=D("1000.00"), closing_balance=D("750.50"))
    assert validate(info, txns).verdict == "verified"
    flipped = copy.deepcopy(info)
    flipped.statement_type = "bank_account"
    assert validate(flipped, txns).verdict == "needs_review"


def test_future_dates_fail():
    info, txns = ledger()
    txns[0].date = date(2099, 1, 1)
    assert check(validate(info, txns), "future_dates").status == "fail"


def test_no_transactions_is_failed():
    assert validate(StatementInfo("bank_account"), []).verdict == "failed"


def test_wordless_narrations_block_verification():
    info, txns = ledger()
    for t in txns:
        t.description = "674548"
    r = validate(info, txns)
    assert check(r, "descriptions").status == "fail"
    assert r.verdict == "needs_review"
