from datetime import date
from decimal import Decimal

import pytest

from awesome_statement_parser.values import infer_date_format, looks_like_money, parse_amount, parse_date, signed_balance


@pytest.mark.parametrize(
    "text, value, marker, negative",
    [
        ("1,23,456.78", "123456.78", None, False),
        ("123,456.78", "123456.78", None, False),
        ("450", "450", None, False),
        ("1,234.56 Cr", "1234.56", "cr", False),
        ("1,234.56Dr", "1234.56", "dr", False),
        ("1,234.56 CR", "1234.56", "cr", False),
        ("-450.00", "450.00", None, True),
        ("450.00-", "450.00", None, True),
        ("(450.00)", "450.00", None, True),
        ("₹ 2,500.00", "2500.00", None, False),
        ("Rs. 99.50", "99.50", None, False),
        ("INR -10.00", "10.00", None, True),
    ],
)
def test_parse_amount(text, value, marker, negative):
    p = parse_amount(text)
    assert p is not None
    assert p.value == Decimal(value)
    assert p.marker == marker
    assert p.negative == negative


@pytest.mark.parametrize("text", ["", "abc", "12/04/2024", "UPI/1234", "1.2.3"])
def test_parse_amount_rejects(text):
    assert parse_amount(text) is None


def test_signed_balance_overdrawn():
    assert signed_balance(parse_amount("1,000.00 Dr")) == Decimal("-1000.00")
    assert signed_balance(parse_amount("1,000.00 Cr")) == Decimal("1000.00")


def test_money_detection_needs_decimals():
    assert looks_like_money("paid 1,234.50 today")
    assert not looks_like_money("ref 123456")
    assert not looks_like_money("UPI/4123456789/x")


@pytest.mark.parametrize(
    "samples, fmt",
    [
        (["01/04/24", "15/04/24"], "%d/%m/%y"),
        (["1 Apr 2024", "30 Apr 2024"], "%d %b %Y"),
        (["01-Apr-2024"], "%d-%b-%Y"),
        (["2024-04-01", "2024-04-30"], "%Y-%m-%d"),
        (["040724", "150724"], "%d%m%y"),
        (["03/04/2024", "04/05/2024"], "%d/%m/%Y"),  # ambiguous -> day-first (Indian)
    ],
)
def test_infer_date_format(samples, fmt):
    assert infer_date_format(samples) == fmt


def test_parse_date_year_bounds():
    assert parse_date("01/04/2024", "%d/%m/%Y") == date(2024, 4, 1)
    assert parse_date("01/04/1890", "%d/%m/%Y") is None


def test_three_decimal_amounts():
    assert parse_amount("12345.678").value == Decimal("12345.678")
    assert looks_like_money("1234567.890 Cr")
