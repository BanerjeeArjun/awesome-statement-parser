"""Deterministic parsing of amounts and dates. The LLM never parses numbers — Python does.

Handles Indian digit grouping (1,23,456.78), western grouping, Dr/Cr markers as
prefix/suffix, leading/trailing minus, parentheses, and currency prefixes.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

_CURRENCY = r"(?:INR|Rs\.?|₹|USD|\$|EUR|€|GBP|£)"
_NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,3})?"  # some banks print 3 decimals
_MARK = r"(?:CR|DR|C|D)\.?"

_AMOUNT_RE = re.compile(
    rf"""^\s*
    (?P<open>\()?\s*
    (?P<premark>{_MARK}\s+)?
    (?P<lead>[-+])?\s*
    (?:{_CURRENCY}\s*)?
    (?P<lead2>[-+])?\s*
    (?P<num>{_NUM})
    \s*(?P<trail>-)?
    \s*(?P<close>\))?
    \s*(?P<suffix>{_MARK})?
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# An amount-looking token *with* decimals (used to spot stray money values on a line).
MONEY_TOKEN_RE = re.compile(
    rf"(?<![\w/.-])[-+(]?{_CURRENCY}?\s?(?:\d{{1,3}}(?:,\d{{2,3}})+|\d+)\.\d{{2,3}}\)?(?:\s?(?:CR|DR|Cr|Dr|cr|dr))?(?![\w/])"
)


@dataclass(frozen=True)
class ParsedAmount:
    value: Decimal  # absolute value
    marker: Optional[str]  # "cr" | "dr" | None
    negative: bool  # printed with a minus sign or in parentheses
    text: str


def parse_amount(text: str) -> Optional[ParsedAmount]:
    """Parse a printed amount. Returns None if the text is not a single amount."""
    if text is None:
        return None
    t = text.strip().replace("−", "-").replace("\xa0", " ")
    if not t:
        return None
    m = _AMOUNT_RE.match(t)
    if not m:
        return None
    try:
        value = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    mark = (m.group("suffix") or m.group("premark") or "").strip().rstrip(".").lower()
    marker = {"cr": "cr", "c": "cr", "dr": "dr", "d": "dr"}.get(mark)
    negative = bool(
        (m.group("open") and m.group("close"))
        or m.group("lead") == "-"
        or m.group("lead2") == "-"
        or m.group("trail")
    )
    return ParsedAmount(value=value, marker=marker, negative=negative, text=text.strip())


def looks_like_money(text: str) -> bool:
    return bool(MONEY_TOKEN_RE.search(text or ""))


def money_tokens(text: str) -> list[str]:
    return [m.group(0).strip() for m in MONEY_TOKEN_RE.finditer(text or "")]


def signed_balance(p: ParsedAmount) -> Decimal:
    """A printed balance: 'Dr' or minus means overdrawn (negative)."""
    return -p.value if (p.negative or p.marker == "dr") else p.value


def fmt_amount(v: Optional[Decimal]) -> Optional[str]:
    if v is None:
        return None
    return f"{v:.2f}" if v == v.quantize(Decimal("0.01")) else format(v.normalize(), "f")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

DATE_FORMATS = [
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    "%d %b %Y", "%d %b %y", "%d-%b-%Y", "%d-%b-%y", "%d/%b/%Y", "%d/%b/%y",
    "%d %B %Y", "%d-%B-%Y", "%d %b, %Y", "%d%b%Y", "%d%b%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
    "%d%m%Y", "%d%m%y",
    "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
]

# Shapes a leading date token can take; used to cut the date off the front of a cell.
_DATE_PREFIX_RE = re.compile(
    r"""^\s*(
        \d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}
      | \d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/,]{1,2}\d{2,4}
      | \d{1,2}[A-Za-z]{3}\d{2,4}
      | \d{4}[-/]\d{1,2}[-/]\d{1,2}
      | [A-Za-z]{3,9}\s\d{1,2},?\s\d{4}
      | \d{8}(?!\d)
      | \d{6}(?!\d)
    )""",
    re.VERBOSE,
)

_ANY_DATE_RE = re.compile(
    r"""(?<![\w])(
        \d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}
      | \d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/,]{1,2}\d{2,4}
      | \d{4}-\d{1,2}-\d{1,2}
      | [A-Za-z]{3,9}\s\d{1,2},?\s\d{4}
    )(?![\w])""",
    re.VERBOSE,
)


def date_prefix(text: str) -> Optional[str]:
    m = _DATE_PREFIX_RE.match(text or "")
    return m.group(1).strip() if m else None


def find_dates(text: str) -> list[str]:
    return [m.group(1) for m in _ANY_DATE_RE.finditer(text or "")]


def parse_date(text: str, fmt: str) -> Optional[date]:
    t = re.sub(r"\s+", " ", (text or "").strip().rstrip(","))
    try:
        d = datetime.strptime(t, fmt).date()
    except ValueError:
        return None
    if not (1990 <= d.year <= 2100):
        return None
    return d


def parse_date_any(text: str, preferred: Optional[str] = None) -> Optional[date]:
    fmts = ([preferred] if preferred else []) + DATE_FORMATS
    for f in fmts:
        d = parse_date(text, f)
        if d:
            return d
    return None


def infer_date_format(samples: Iterable[str]) -> Optional[str]:
    """Pick the format that parses the most samples; ties go to the earlier (day-first) format."""
    samples = [s for s in samples if s]
    if not samples:
        return None
    best, best_n = None, 0
    for f in DATE_FORMATS:
        n = sum(1 for s in samples if parse_date(s, f))
        if n > best_n:
            best, best_n = f, n
    return best


def most_common(xs: Iterable) -> Optional[object]:
    c = Counter(x for x in xs if x is not None)
    return c.most_common(1)[0][0] if c else None
