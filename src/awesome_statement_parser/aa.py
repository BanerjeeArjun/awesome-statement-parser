"""Export a parsed statement as a ReBIT Account Aggregator (AA) FI-data document.

The AA ecosystem (ReBIT specifications, maintained with Sahamati) defines the *decrypted* financial
information a FIP hands to an FIU as one XML-schema'd ``Account`` document per account. ReBIT
publishes the schemas as XSD only — there is no official JSON schema — so, like the AA
providers, we map the XSD one-to-one: XML attributes become JSON keys with the same
spelling, elements become nested objects keyed by their lower-camel name
(``Account`` -> ``account``, ``Profile`` -> ``profile``, ...), and every repeating element
(``Holder``, ``Card``, ``Transaction``) is a list.

    bank account  -> FI type ``deposit``      (schemas/deposit/deposit.xsd)
    credit card   -> FI type ``credit_card``  (schemas/credit_card/others_creditcard.xsd)

Statements do not carry most of what the schema marks required (holder DOB, mobile, PAN, branch,
facility, card network, ...). Those keys are always present: filled from an optional *profile* when
the caller knows them, otherwise ``""`` — the placeholder ReBIT's own sample files use. Nothing is
invented. :func:`aa_gaps` lists what is still empty.

Deliberate deviations from the XSD's primitive types, to keep the project's exact-money guarantee:
amounts and balances are decimal *strings* (``"1234.50"``), never floats; and ``"+05:30"`` (IST) is
attached to the two ``xs:dateTime`` attributes (``balanceDateTime``, ``transactionTimestamp``),
because a statement only prints a date and AA is an Indian framework. Every other date is an
``xs:date`` (``YYYY-MM-DD``) in both schemas.

Only the decrypted ``Account`` payload is produced. The FIFetchResponse envelope and the ECDH
encryption belong to the FIP/AA transport, not to a parser.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from .models import ParseResult
from .serialize import info_from_dict, result_to_dict, to_jsonable, txn_from_dict

# Schema versions we target (``Account/@version``, xs:float in 0.0..2.0).
DEPOSIT_VERSION = "2.0"
CREDIT_CARD_VERSION = "1.0"

IST = "+05:30"
_NS = uuid.UUID("6f0f3a52-5d2c-4c58-9a51-0a7a5e0b6c11")  # namespace for deterministic ids

# Attributes whose "" is *not* a gap: optional in every published version of the schema.
_OPTIONAL = {"landline", "address", "currency", "exchgeRate"}


class AAError(ValueError):
    """The AA document cannot be produced from the given input."""


class ProfileError(AAError):
    """The supplied AA profile has a value the ReBIT schema would reject."""


# ---------------------------------------------------------------------------
# Profile: what the statement cannot tell us
# ---------------------------------------------------------------------------
#
# {
#   "account":  {"linkedAccRef": "...", "maskedAccNumber": "..."},
#   "holders":  [{"name": "...", "dob": "1990-01-31", "mobile": "9199...", "nominee": "REGISTERED",
#                 "email": "...", "pan": "ABCDE1234F", "ckycCompliance": true}],
#   "summary":  {"type": "SAVINGS", "status": "ACTIVE", "branch": "...", "ifscCode": "...", ...},
#   "cards":    [{"cardType": "VISA", "primary": "YES", "issuedDate": "2021-06-01"}]   # credit cards only
# }
#
# Keys are the AA attribute names. A value given here wins over what is derived from the statement.

_SECTIONS = {"account", "holders", "summary", "cards"}
_ACCOUNT_KEYS = {"linkedAccRef", "maskedAccNumber"}
_HOLDER_KEYS = {"name", "dob", "mobile", "nominee", "landline", "address", "email", "pan", "ckycCompliance"}
_CARD_KEYS = {"cardType", "primary", "issuedDate", "maskedCardNumber"}
_SUMMARY_KEYS = {
    "deposit": {"currentBalance", "balanceDateTime", "type", "branch", "facility", "ifscCode", "micrCode",
                "openingDate", "currentODLimit", "drawingLimit", "status", "currency", "exchgeRate"},
    "credit_card": {"currentDue", "lastStatementDate", "dueDate", "previousDueAmount", "totalDueAmount",
                    "minDueAmount", "creditLimit", "cashLimit", "availableCredit", "loyaltyPoints", "financeCharges"},
}
_ENUMS = {
    "nominee": {"REGISTERED", "NOT-REGISTERED"},
    "type": {"SAVINGS", "CURRENT"},  # summary.type (deposit)
    "status": {"ACTIVE", "INACTIVE"},
    "facility": {"OD", "CC"},
    "cardType": {"MASTER_CARD", "VISA", "RUPAY", "OTHERS"},
    "primary": {"YES", "NO"},
}
_SUMMARY_DATES = {"openingDate", "lastStatementDate", "dueDate"}
_SUMMARY_DECIMALS = {"currentBalance", "currentDue", "previousDueAmount", "totalDueAmount", "minDueAmount",
                     "creditLimit", "cashLimit", "availableCredit", "financeCharges"}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
_PAN = re.compile(r"[A-Za-z]{5}[0-9]{4}[A-Za-z]")
_EMAIL = re.compile(r"[^@\s]+@[^.\s]+\.\S+")
_MOBILE = re.compile(r"[1-9][0-9]*")  # xs:integer: a leading zero would be dropped silently
_DECIMAL = re.compile(r"-?\d+(\.\d+)?")


def load_profile(path: str | Path) -> dict:
    """Read and validate a profile JSON file (any failure is a :class:`ProfileError`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ProfileError(f"cannot read profile {path}: {e}") from e
    try:
        profile = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path}: not valid JSON ({e})") from e
    return check_profile(profile)


def check_profile(profile: Optional[dict], fi_type: Optional[str] = None) -> dict:
    """Validate a profile and return a clean copy. ``fi_type`` (``deposit`` / ``credit_card``) narrows
    which summary keys and sections are accepted; without it the union is."""
    if profile is None:
        return {}
    if not isinstance(profile, dict):
        raise ProfileError("the profile must be a JSON object with sections account, holders, summary, cards")
    unknown = set(profile) - _SECTIONS
    if unknown:
        raise ProfileError(f"unknown profile section(s): {', '.join(sorted(unknown))} (use account, holders, summary, cards)")
    out: dict = {}

    if "account" in profile:
        out["account"] = _record(profile["account"], _ACCOUNT_KEYS, "account")

    if "holders" in profile:
        out["holders"] = [_holder_record(h, f"holders[{i}]") for i, h in enumerate(_list(profile["holders"], "holders"))]

    if "cards" in profile:
        if fi_type == "deposit":
            raise ProfileError("'cards' only applies to credit card statements, but this is a bank account statement")
        out["cards"] = [_card_record(c, f"cards[{i}]") for i, c in enumerate(_list(profile["cards"], "cards"))]

    if "summary" in profile:
        allowed = _SUMMARY_KEYS[fi_type] if fi_type else _SUMMARY_KEYS["deposit"] | _SUMMARY_KEYS["credit_card"]
        s = _record(profile["summary"], allowed, "summary")
        for k, v in s.items():
            if k in _ENUMS:
                _check_enum(k, v, f"summary.{k}")
            elif k in _SUMMARY_DATES:
                _check_date(v, f"summary.{k}")
            elif k == "balanceDateTime" and not _DATETIME.fullmatch(v):
                raise ProfileError(f"summary.balanceDateTime {v!r} must be an ISO date-time, e.g. 2025-04-30T00:00:00+05:30")
            elif k in _SUMMARY_DECIMALS and not _DECIMAL.fullmatch(v):
                raise ProfileError(f"summary.{k} {v!r} must be a plain decimal number such as 1234.50")
            elif k == "loyaltyPoints" and not re.fullmatch(r"[1-9][0-9]*", v):
                raise ProfileError(f"summary.loyaltyPoints {v!r} must be a positive whole number")
        out["summary"] = s
    return out


def _list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise ProfileError(f"{where} must be a list")
    return value


def _text(value: Any, where: str) -> str:
    """Profile values are text (or a whole number); floats are refused so money is never imprecise."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProfileError(f"{where} must be a string, got {type(value).__name__}")
    return str(value)


def _record(d: Any, allowed: set, where: str) -> dict:
    if not isinstance(d, dict):
        raise ProfileError(f"{where} must be an object")
    bad = set(d) - allowed
    if bad:
        raise ProfileError(f"{where}: unknown field(s) {', '.join(sorted(bad))}; allowed: {', '.join(sorted(allowed))}")
    return {k: _text(v, f"{where}.{k}") for k, v in d.items()}


def _holder_record(h: Any, where: str) -> dict:
    if not isinstance(h, dict):
        raise ProfileError(f"{where} must be an object")
    ckyc = h.get("ckycCompliance")
    if "ckycCompliance" in h and not isinstance(ckyc, bool):
        raise ProfileError(f"{where}.ckycCompliance must be true or false")
    out = _record({k: v for k, v in h.items() if k != "ckycCompliance"}, _HOLDER_KEYS, where)
    if "ckycCompliance" in h:
        out["ckycCompliance"] = ckyc
    if "pan" in out and not _PAN.fullmatch(out["pan"]):
        raise ProfileError(f"{where}.pan {out['pan']!r} does not match the AA pattern (5 letters, 4 digits, 1 letter)")
    if "email" in out and not _EMAIL.fullmatch(out["email"]):
        raise ProfileError(f"{where}.email {out['email']!r} does not match the AA pattern")
    if "mobile" in out and not _MOBILE.fullmatch(out["mobile"]):
        raise ProfileError(f"{where}.mobile {out['mobile']!r} must be digits only, with no leading zero or '+' (use 919999999999)")
    if "dob" in out:
        _check_date(out["dob"], f"{where}.dob")
    if "nominee" in out:
        _check_enum("nominee", out["nominee"], f"{where}.nominee")
    return out


def _card_record(c: Any, where: str) -> dict:
    out = _record(c, _CARD_KEYS, where)
    for k in ("cardType", "primary"):
        if k in out:
            _check_enum(k, out[k], f"{where}.{k}")
    if "issuedDate" in out:
        _check_date(out["issuedDate"], f"{where}.issuedDate")
    return out


def _check_enum(key: str, value: str, where: str) -> None:
    if value not in _ENUMS[key]:
        raise ProfileError(f"{where} {value!r} is not one of {', '.join(sorted(_ENUMS[key]))}")


def _check_date(value: str, where: str) -> None:
    try:
        if not _DATE.fullmatch(value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise ProfileError(f"{where} {value!r} must be a real date written YYYY-MM-DD") from None


# ---------------------------------------------------------------------------
# Field derivations
# ---------------------------------------------------------------------------

def mask_account_number(number: Optional[str]) -> str:
    """AA only ever carries masked numbers: keep the last four characters, mask everything before them.

    Applied to the statement's number *and* to any number supplied in a profile, so a full account or
    card number cannot leave through this export.
    """
    if not number:
        return ""
    s = re.sub(r"[\s-]+", "", str(number))
    if len(s) <= 4:
        return "X" * len(s)
    return "X" * (len(s) - 4) + s[-4:]


# Order matters: the first match wins. UPI/ATM are specific; a transfer rail (NEFT, IMPS, ...) outranks
# the loose card/cash words, so "NEFT ... CREDIT CARD PAYMENT" is a transfer, not a card swipe.
_MODES = (
    ("UPI", re.compile(r"\bUPI\b|\bBHIM\b|\bVPA\b", re.I)),
    ("ATM", re.compile(r"\bATM\b|\bNFS\b|\bAWB\b", re.I)),
    ("FT", re.compile(r"\bNEFT\b|\bRTGS\b|\bIMPS\b|\bACH\b|\bNACH\b|\bECS\b|\bTRF\b|\bTRANSFER\b|\bFT\b|\bINF\b|\bIFT\b", re.I)),
    ("CARD", re.compile(r"\bPOS\b|\bECOM\b|\bDEBIT\s*CARD\b|\bVISA\b|\bMASTERCARD\b|\bRUPAY\b", re.I)),
    ("CASH", re.compile(r"\bCASH\b|\bCSH\b|\bCDM\b", re.I)),
)


def infer_mode(narration: str) -> str:
    """Transaction/@mode from the narration. ``OTHERS`` when nothing recognisable is printed."""
    for mode, rx in _MODES:
        if rx.search(narration or ""):
            return mode
    return "OTHERS"


def _stamp(d: Optional[str]) -> str:
    """xs:dateTime for a printed date (no time of day on statements)."""
    return f"{d}T00:00:00{IST}" if d else ""


def _txn_type(direction: Optional[str]) -> str:
    return {"debit": "DEBIT", "credit": "CREDIT"}.get(direction or "", "")


def _txn_id(link_ref: str, t: dict) -> str:
    key = "|".join(str(x) for x in (link_ref, t.get("id"), t.get("date"), t.get("amount"), t.get("direction"), t.get("description")))
    return str(uuid.uuid5(_NS, key))


def _span(st: dict, txns: list[dict]) -> tuple[str, str]:
    dates = sorted(t["date"] for t in txns if t.get("date"))
    return st.get("period_start") or (dates[0] if dates else ""), st.get("period_end") or (dates[-1] if dates else "")


def _fill(base: dict, override: Optional[dict]) -> dict:
    """``base`` with the profile's values layered over it (profile wins)."""
    return {**base, **(override or {})}


def _holder(profile_holder: Optional[dict]) -> dict:
    h = _fill({"name": "", "dob": "", "mobile": "", "nominee": "", "email": "", "pan": "", "ckycCompliance": ""}, profile_holder)
    if h["mobile"] != "":
        h["mobile"] = int(h["mobile"])
    return h


def _normalise(src: Any) -> tuple[dict, list[dict]]:
    """Statement and rows in canonical form (decimal strings with two places, ISO dates).

    Reviewed rows come from the UI/API as typed (``"1234.5"``, ``" 99"``); they are re-read through the
    same parsers the pipeline uses so the export can't carry the raw text.
    """
    if not isinstance(src, dict) or not isinstance(src.get("statement"), dict):
        raise AAError("nothing to export: this result has no statement (a rejected job that was never reviewed has no rows)")
    try:
        txns = [txn_from_dict(t) for t in src.get("transactions") or []]
        st = info_from_dict(src["statement"])
    except (InvalidOperation, ValueError, KeyError, TypeError) as e:
        raise AAError(f"cannot read the statement rows: {e!r}") from e
    for where, pairs in [(f"row {t.id}", (("amount", t.amount), ("balance", t.balance))) for t in txns] + [
            ("statement", tuple((k, getattr(st, k)) for k in ("opening_balance", "closing_balance", "total_debits",
                                                              "total_credits", "minimum_amount_due")))]:
        for name, v in pairs:
            if v is not None and not v.is_finite():
                raise AAError(f"{where}: {name} {v} is not a finite number")
    return to_jsonable(st), [to_jsonable(t) for t in txns]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _identity(fi_type: str, st: dict, profile: dict) -> tuple[str, str]:
    acct = profile.get("account") or {}
    masked = mask_account_number(acct.get("maskedAccNumber") or st.get("account_number"))
    link = acct.get("linkedAccRef") or str(uuid.uuid5(_NS, f"{fi_type}|{st.get('bank_name') or ''}|{masked}"))
    return masked, link


def _deposit(st: dict, txns: list[dict], profile: dict) -> dict:
    masked, link = _identity("deposit", st, profile)
    known = profile.get("holders") or []
    holders = [_holder(h) for h in known or [None]]
    # Single vs joint is only knowable from the profile; with no holder given it stays unknown.
    holders_type = "" if not known else ("JOINT" if len(known) > 1 else "SINGLE")
    last_balance = next((t["balance"] for t in reversed(txns) if t.get("balance")), None)
    start, end = _span(st, txns)

    summary = {
        "currentBalance": st.get("closing_balance") or last_balance or "",
        "balanceDateTime": _stamp(end),
        "type": "", "branch": "", "facility": "", "ifscCode": "", "micrCode": "", "openingDate": "",
        "currentODLimit": "", "drawingLimit": "", "status": "",
    }
    if st.get("currency"):
        summary["currency"] = st["currency"]

    transactions = [{
        "txnId": _txn_id(link, t),
        "type": _txn_type(t.get("direction")),
        "mode": infer_mode(t.get("description") or ""),
        "amount": t.get("amount") or "",
        "currentBalance": t.get("balance") or "",
        "transactionTimestamp": _stamp(t.get("date")),
        "valueDate": t.get("value_date") or t.get("date") or "",
        "narration": t.get("description") or "",
        "reference": t.get("reference") or "",
    } for t in txns]

    return {"account": {
        "type": "deposit",
        "maskedAccNumber": masked,
        "version": DEPOSIT_VERSION,
        "linkedAccRef": link,
        "profile": {"holders": {"type": holders_type, "holder": holders}},
        "summary": _fill(summary, profile.get("summary")),
        "transactions": {"startDate": start, "endDate": end, "transaction": transactions},
    }}


def _credit_card(st: dict, txns: list[dict], profile: dict) -> dict:
    masked, link = _identity("credit_card", st, profile)
    start, end = _span(st, txns)

    cards = []
    for c in profile.get("cards") or [None]:
        card = _fill({"cardType": "", "primary": "", "issuedDate": "", "maskedCardNumber": masked}, c)
        card["maskedCardNumber"] = mask_account_number(card["maskedCardNumber"])
        cards.append(card)
    holders = []
    for i, ph in enumerate(profile.get("holders") or [None]):
        h = _holder(ph)
        h["cards"] = {"card": cards if i == 0 else []}
        holders.append(h)
    card_no = cards[0]["maskedCardNumber"]

    summary = {
        "currentDue": "",
        "lastStatementDate": end,
        "dueDate": st.get("payment_due_date") or "",
        "previousDueAmount": st.get("opening_balance") or "",
        "totalDueAmount": st.get("closing_balance") or "",
        "minDueAmount": st.get("minimum_amount_due") or "",
        "creditLimit": "", "cashLimit": "", "availableCredit": "", "loyaltyPoints": "", "financeCharges": "",
    }

    transactions = [{
        "txnId": _txn_id(link, t),
        "txnType": _txn_type(t.get("direction")),
        "txnDate": t.get("date") or "",
        "amount": t.get("amount") or "",
        "valueDate": t.get("value_date") or t.get("date") or "",
        "narration": t.get("description") or "",
        "statementDate": end,
        "mcc": "",
        "maskedCardNumber": card_no,
    } for t in txns]

    return {"account": {
        "type": "credit_card",
        "maskedAccNumber": masked,
        "version": CREDIT_CARD_VERSION,
        "linkedAccRef": link,
        "profile": {"holders": {"holder": holders}},
        "summary": _fill(summary, profile.get("summary")),
        "transactions": {"startDate": start, "endDate": end, "transaction": transactions},
    }}


def to_aa(result: ParseResult | dict, profile: Optional[dict] = None) -> dict:
    """The AA ``Account`` document for a parse result.

    ``result`` is a :class:`ParseResult` or its dict form (``result_to_dict`` / a reviewed job, so
    edits made in the review UI are what gets exported). ``profile`` is described above.

    Raises :class:`ProfileError` for an invalid profile and :class:`AAError` when the rows can't be read.
    """
    src = result_to_dict(result) if isinstance(result, ParseResult) else result
    st, txns = _normalise(src)
    fi_type = "credit_card" if st["statement_type"] == "credit_card" else "deposit"
    profile = check_profile(profile, fi_type)
    return (_credit_card if fi_type == "credit_card" else _deposit)(st, txns, profile)


# ---------------------------------------------------------------------------
# What is still missing
# ---------------------------------------------------------------------------

def aa_gaps(doc: dict) -> list[str]:
    """Paths of required ReBIT fields that hold the ``""`` placeholder.

    Per-transaction gaps are summarised (``transactions.transaction[*].currentBalance``) rather
    than listed row by row.
    """
    acct = doc["account"]
    gaps: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif node == "" and path.rsplit(".", 1)[-1].split("[")[0] not in _OPTIONAL:
            gaps.append(path)

    for section in ("profile", "summary"):
        walk(acct.get(section), f"account.{section}")
    if not acct["maskedAccNumber"]:
        gaps.insert(0, "account.maskedAccNumber")
    rows = acct["transactions"]["transaction"]
    for key in sorted({k for r in rows for k, v in r.items() if v == "" and k not in ("reference", "narration")}):
        n = sum(1 for r in rows if r.get(key) == "")
        gaps.append(f"account.transactions.transaction[*].{key} ({n} of {len(rows)} rows)")
    for key in ("startDate", "endDate"):
        if not acct["transactions"][key]:
            gaps.append(f"account.transactions.{key}")
    return gaps
