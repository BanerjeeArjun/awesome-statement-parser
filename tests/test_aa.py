"""ReBIT Account Aggregator export. Required keys, enums and patterns below are copied from the
published XSDs (schemas/deposit/deposit.xsd, schemas/credit_card/others_creditcard.xsd)."""
import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from awesome_statement_parser import server, to_aa, aa_gaps, load_profile, ProfileError
from awesome_statement_parser.aa import AAError, infer_mode, mask_account_number
from awesome_statement_parser.cli import main as cli_main
from awesome_statement_parser.pipeline import parse_statement
from awesome_statement_parser.store import JobStore

from .conftest import FakeLLM, load_case

DEPOSIT = {
    "account": {"type", "maskedAccNumber", "version", "linkedAccRef"},
    "holder": {"name", "dob", "mobile", "nominee", "email", "pan", "ckycCompliance"},
    "summary": {"currentBalance", "balanceDateTime", "type", "branch", "facility", "ifscCode", "micrCode",
                "openingDate", "currentODLimit", "drawingLimit", "status"},
    "transactions": {"startDate", "endDate"},
    "transaction": {"txnId", "type", "mode", "amount", "currentBalance", "transactionTimestamp", "valueDate",
                    "narration", "reference"},
}
CREDIT_CARD = {
    "account": {"type", "maskedAccNumber", "version", "linkedAccRef"},
    "holder": {"name", "dob", "mobile", "nominee", "email", "pan", "ckycCompliance"},
    "card": {"cardType", "primary", "issuedDate", "maskedCardNumber"},
    "summary": {"currentDue", "lastStatementDate", "dueDate", "previousDueAmount", "totalDueAmount",
                "minDueAmount", "creditLimit", "cashLimit", "availableCredit", "loyaltyPoints", "financeCharges"},
    "transactions": {"startDate", "endDate"},
    "transaction": {"txnId", "txnType", "txnDate", "amount", "valueDate", "narration", "statementDate", "mcc",
                    "maskedCardNumber"},
}

HOLDER = {"name": "A Person", "dob": "1990-01-31", "mobile": "919999999999", "nominee": "REGISTERED",
          "email": "a@example.com", "pan": "ABCDE1234F", "ckycCompliance": True}


def _bank(**over) -> dict:
    d = {
        "status": "verified",
        "statement": {"statement_type": "bank_account", "bank_name": "Test Bank", "account_number": "50100012345128",
                      "currency": "INR", "period_start": "2025-04-01", "period_end": "2025-04-30",
                      "opening_balance": "12000.00", "closing_balance": "15500.00"},
        "transactions": [
            {"id": 1, "date": "2025-04-02", "description": "UPI/41234/GROCER", "amount": "450.00",
             "direction": "debit", "balance": "11550.00", "reference": "UPI-41234", "value_date": None},
            {"id": 2, "date": "2025-04-05", "description": "NEFT CR-ACME-SALARY", "amount": "3950.00",
             "direction": "credit", "balance": "15500.00", "reference": None, "value_date": "2025-04-06"},
        ],
    }
    d.update(over)
    return d


def _card() -> dict:
    return {
        "status": "verified",
        "statement": {"statement_type": "credit_card", "bank_name": "Test Card", "account_number": "4111 1111 1111 1286",
                      "period_start": "2025-04-01", "period_end": "2025-04-30", "opening_balance": "1000.00",
                      "closing_balance": "1200.00", "minimum_amount_due": "60.00", "payment_due_date": "2025-05-18"},
        "transactions": [
            {"id": 1, "date": "2025-04-03", "description": "BIGBASKET MUMBAI", "amount": "700.00",
             "direction": "debit", "balance": None, "reference": None, "value_date": None},
            {"id": 2, "date": "2025-04-09", "description": "PAYMENT RECEIVED", "amount": "500.00",
             "direction": "credit", "balance": None, "reference": None, "value_date": None},
        ],
    }


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_deposit_document_has_every_required_key():
    a = to_aa(_bank())["account"]
    assert set(a) >= DEPOSIT["account"] | {"profile", "summary", "transactions"}
    assert a["type"] == "deposit" and a["version"] == "2.0"
    holders = a["profile"]["holders"]
    assert "type" in holders and len(holders["holder"]) == 1
    assert set(holders["holder"][0]) >= DEPOSIT["holder"]
    assert set(a["summary"]) >= DEPOSIT["summary"]
    assert set(a["transactions"]) >= DEPOSIT["transactions"] | {"transaction"}
    for t in a["transactions"]["transaction"]:
        assert set(t) == DEPOSIT["transaction"]


def test_deposit_values_from_statement():
    a = to_aa(_bank())["account"]
    assert a["maskedAccNumber"] == "XXXXXXXXXX5128"
    assert "50100012345128" not in json.dumps(a)  # the full number never leaves
    s = a["summary"]
    assert s["currentBalance"] == "15500.00" and s["currency"] == "INR"
    assert s["balanceDateTime"] == "2025-04-30T00:00:00+05:30"
    assert (a["transactions"]["startDate"], a["transactions"]["endDate"]) == ("2025-04-01", "2025-04-30")
    t1, t2 = a["transactions"]["transaction"]
    assert (t1["type"], t1["mode"], t1["amount"], t1["currentBalance"]) == ("DEBIT", "UPI", "450.00", "11550.00")
    assert t1["transactionTimestamp"] == "2025-04-02T00:00:00+05:30"
    assert t1["valueDate"] == "2025-04-02"  # no printed value date -> the transaction date
    assert t1["reference"] == "UPI-41234"
    assert (t2["type"], t2["mode"], t2["valueDate"], t2["reference"]) == ("CREDIT", "FT", "2025-04-06", "")
    assert t1["txnId"] != t2["txnId"]


def test_amounts_are_strings_never_floats():
    a = to_aa(_bank())["account"]
    assert isinstance(a["summary"]["currentBalance"], str)
    assert all(isinstance(t["amount"], str) for t in a["transactions"]["transaction"])


def test_output_is_deterministic():
    assert to_aa(_bank()) == to_aa(_bank())


def test_unknown_values_are_empty_placeholders_not_invented():
    a = to_aa(_bank())["account"]
    h = a["profile"]["holders"]["holder"][0]
    assert all(h[k] == "" for k in DEPOSIT["holder"])
    assert a["profile"]["holders"]["type"] == ""
    assert all(a["summary"][k] == "" for k in ("type", "branch", "facility", "ifscCode", "micrCode", "status"))


def test_period_falls_back_to_transaction_dates():
    d = _bank()
    d["statement"].update(period_start=None, period_end=None, closing_balance=None)
    a = to_aa(d)["account"]
    assert (a["transactions"]["startDate"], a["transactions"]["endDate"]) == ("2025-04-02", "2025-04-05")
    assert a["summary"]["currentBalance"] == "15500.00"  # last printed running balance
    assert a["summary"]["balanceDateTime"] == "2025-04-05T00:00:00+05:30"


def test_credit_card_document():
    a = to_aa(_card())["account"]
    assert a["type"] == "credit_card" and a["version"] == "1.0"
    assert set(a["summary"]) >= CREDIT_CARD["summary"]
    holder = a["profile"]["holders"]["holder"][0]
    assert set(holder) >= CREDIT_CARD["holder"] | {"cards"}
    card = holder["cards"]["card"][0]
    assert set(card) == CREDIT_CARD["card"] and card["maskedCardNumber"] == "XXXXXXXXXXXX1286"
    s = a["summary"]
    assert (s["previousDueAmount"], s["totalDueAmount"], s["minDueAmount"]) == ("1000.00", "1200.00", "60.00")
    assert (s["lastStatementDate"], s["dueDate"]) == ("2025-04-30", "2025-05-18")
    t1, t2 = a["transactions"]["transaction"]
    assert set(t1) == CREDIT_CARD["transaction"]
    assert (t1["txnType"], t1["txnDate"], t1["amount"], t1["statementDate"]) == ("DEBIT", "2025-04-03", "700.00", "2025-04-30")
    assert t2["txnType"] == "CREDIT"
    assert "balance" not in t1 and "currentBalance" not in t1  # the card schema has no running balance


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def test_profile_fills_what_the_statement_lacks():
    profile = {"holders": [HOLDER],
               "summary": {"type": "SAVINGS", "status": "ACTIVE", "branch": "MG Road", "ifscCode": "HDFC0001234"},
               "account": {"linkedAccRef": "LINK-1"}}
    doc = to_aa(_bank(), profile)
    a = doc["account"]
    assert a["linkedAccRef"] == "LINK-1"
    assert a["profile"]["holders"]["type"] == "SINGLE"
    h = a["profile"]["holders"]["holder"][0]
    assert h["pan"] == "ABCDE1234F" and h["mobile"] == 919999999999 and h["ckycCompliance"] is True
    assert a["summary"]["type"] == "SAVINGS" and a["summary"]["ifscCode"] == "HDFC0001234"
    assert a["summary"]["currentBalance"] == "15500.00"  # still from the statement
    gaps = aa_gaps(doc)
    assert not any("holder[0]" in g for g in gaps)
    assert "account.summary.micrCode" in gaps


def test_two_holders_make_a_joint_account():
    a = to_aa(_bank(), {"holders": [HOLDER, dict(HOLDER, name="B Person")]})["account"]
    assert a["profile"]["holders"]["type"] == "JOINT" and len(a["profile"]["holders"]["holder"]) == 2


def test_credit_card_profile_supplies_card_details():
    a = to_aa(_card(), {"holders": [HOLDER], "cards": [{"cardType": "VISA", "primary": "YES", "issuedDate": "2021-06-01"}]})["account"]
    card = a["profile"]["holders"]["holder"][0]["cards"]["card"][0]
    assert card == {"cardType": "VISA", "primary": "YES", "issuedDate": "2021-06-01", "maskedCardNumber": "XXXXXXXXXXXX1286"}


@pytest.mark.parametrize("profile, message", [
    ({"holders": [dict(HOLDER, pan="12345")]}, "pan"),
    ({"holders": [dict(HOLDER, email="nope")]}, "email"),
    ({"holders": [dict(HOLDER, mobile="+91 99")]}, "mobile"),
    ({"holders": [dict(HOLDER, dob="31/01/1990")]}, "dob"),
    ({"holders": [dict(HOLDER, nominee="YES")]}, "nominee"),
    ({"holders": [dict(HOLDER, ckycCompliance="true")]}, "ckycCompliance"),
    ({"holders": [{"fullname": "x"}]}, "fullname"),
    ({"summary": {"type": "SALARY"}}, "summary.type"),
    ({"holder": [HOLDER]}, "holder"),
    # finding 4: wrong JSON types are ProfileErrors, not TypeErrors
    ([1], "JSON object"),
    ({"holders": ["x"]}, "holders\\[0\\] must be an object"),
    ({"holders": {"name": "x"}}, "holders must be a list"),
    ({"holders": [dict(HOLDER, name=1.5)]}, "name must be a string"),
    ({"holders": [dict(HOLDER, nominee={"a": 1})]}, "nominee"),
    ({"summary": "x"}, "summary must be an object"),
    ({"summary": {"branhc": "x"}}, "branhc"),  # typos are caught
    ({"summary": {"openingDate": "2015-13-45"}}, "openingDate"),
    ({"summary": {"currentBalance": "1,000"}}, "currentBalance"),
    ({"summary": {"drawingLimit": 1.5}}, "must be a string"),
    ({"account": []}, "account must be an object"),
    ({"account": {"ifscCode": "X"}}, "ifscCode"),
    ({"cards": [{"cardType": "VISA"}]}, "only applies to credit card"),
    # finding 5/6: strict formats
    ({"holders": [dict(HOLDER, dob="20250102")]}, "dob"),
    ({"holders": [dict(HOLDER, dob="2025-W01-1")]}, "dob"),
    ({"holders": [dict(HOLDER, dob="2025-02-30")]}, "dob"),
    ({"holders": [dict(HOLDER, pan="ABCDE1234F\n")]}, "pan"),
    ({"holders": [dict(HOLDER, email="a\n@b.co")]}, "email"),
    ({"holders": [dict(HOLDER, mobile="0919999999999")]}, "mobile"),
])
def test_invalid_profile_is_rejected_with_a_clear_message(profile, message):
    with pytest.raises(ProfileError, match=message):
        to_aa(_bank(), profile)


@pytest.mark.parametrize("profile, message", [
    ({"cards": [{"cardType": "AMEX"}]}, "cardType"),
    ({"cards": [{"issuedDate": "20210601"}]}, "issuedDate"),
    ({"cards": [1]}, "cards\\[0\\] must be an object"),
    ({"summary": {"type": "SAVINGS"}}, "type"),  # a deposit-only attribute on a card statement
    ({"summary": {"creditLimit": "lots"}}, "creditLimit"),
    ({"summary": {"loyaltyPoints": "0"}}, "loyaltyPoints"),
])
def test_invalid_credit_card_profile(profile, message):
    with pytest.raises(ProfileError, match=message):
        to_aa(_card(), profile)


def test_profile_numbers_are_masked_too():
    """A full account/card number in the profile must not leave through the export."""
    doc = to_aa(_bank(), {"account": {"maskedAccNumber": "50100099998888"}})
    assert doc["account"]["maskedAccNumber"] == "XXXXXXXXXX8888" and "5010009999" not in json.dumps(doc)
    doc = to_aa(_card(), {"cards": [{"maskedCardNumber": "4111 1111 1111 9999"}]})
    out = json.dumps(doc)
    assert "4111" not in out and "XXXXXXXXXXXX9999" in out
    t = doc["account"]["transactions"]["transaction"][0]
    assert t["maskedCardNumber"] == "XXXXXXXXXXXX9999"


def test_profile_mobile_is_a_number_and_dates_are_kept_as_written():
    a = to_aa(_bank(), {"holders": [HOLDER]})["account"]
    assert a["profile"]["holders"]["holder"][0]["mobile"] == 919999999999
    assert a["profile"]["holders"]["holder"][0]["dob"] == "1990-01-31"


def test_load_profile_failures_are_profile_errors(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "missing.json")
    p = tmp_path / "bad.json"
    p.write_bytes(b"\xff\xfe\x00not utf8")
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(p)
    p.write_text("[1]")
    with pytest.raises(ProfileError, match="JSON object"):
        load_profile(p)


# ---------------------------------------------------------------------------
# Reviewed rows are normalised (finding 1) and unusable input is an AAError (finding 2)
# ---------------------------------------------------------------------------

def test_edited_rows_are_normalised_not_copied_raw():
    d = _bank()
    t = d["transactions"][0]
    t.update(amount="1,234.5", balance=" 99", date="20250402", value_date="2025-04-03T10:00:00")
    d["statement"]["closing_balance"] = "15500.5"
    a = to_aa(d)["account"]
    row = a["transactions"]["transaction"][0]
    assert (row["amount"], row["currentBalance"]) == ("1234.50", "99.00")
    assert row["transactionTimestamp"] == "2025-04-02T00:00:00+05:30" and row["valueDate"] == "2025-04-03"
    assert a["summary"]["currentBalance"] == "15500.50"


@pytest.mark.parametrize("mutate", [
    lambda d: d["transactions"][0].update(amount="12abc"),
    lambda d: d["transactions"][0].update(amount="NaN"),
    lambda d: d["transactions"][0].update(date="not a date"),
    lambda d: d["transactions"][0].pop("id"),
    lambda d: d["statement"].update(closing_balance="Infinity"),
])
def test_unreadable_rows_raise_aa_error(mutate):
    d = _bank()
    mutate(d)
    with pytest.raises(AAError):
        to_aa(d)


def test_result_without_a_statement_raises_aa_error():
    with pytest.raises(AAError, match="no statement"):
        to_aa({"transactions": []})
    with pytest.raises(AAError, match="no statement"):
        to_aa({"rejected_reason": "bad", "rejected_at": "now"})


def test_load_profile(tmp_path):
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"holders": [HOLDER]}))
    assert load_profile(p)["holders"][0]["pan"] == "ABCDE1234F"
    p.write_text("{not json")
    with pytest.raises(ProfileError, match="not valid JSON"):
        load_profile(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, masked", [
    ("50100012345128", "XXXXXXXXXX5128"),
    ("4111 1111 1111 1286", "XXXXXXXXXXXX1286"),
    ("XXXXXXXX5128", "XXXXXXXX5128"),
    ("4111 XXXX XXXX 1286", "XXXXXXXXXXXX1286"),  # a bank's own masking may still show the first digits
    ("123", "XXX"),
    (None, ""),
])
def test_mask_account_number(raw, masked):
    assert mask_account_number(raw) == masked


@pytest.mark.parametrize("narration, mode", [
    ("UPI/41234/GROCER/grocer@ybl", "UPI"),
    ("ATM CASH WDL MG ROAD", "ATM"),
    ("NFS/CASH WDL/123", "ATM"),
    ("POS 4775XXXXXX8240 MAKEMYTRIP", "CARD"),
    ("ECOM PUR AMAZON", "CARD"),
    ("CASH DEPOSIT BRANCH", "CASH"),
    ("BY CASH-CDM", "CASH"),
    ("NEFT DR-BNYN0562990-RENT", "FT"),
    ("IMPS/P2A/095597297926/SNEHA", "FT"),
    ("RTGS CR-ACME", "FT"),
    ("NEFT-HDFC0000001-ICICI CREDIT CARD PAYMENT", "FT"),  # a transfer rail outranks the word CARD
    ("IMPS/P2A/123/HDFC CARD BILL", "FT"),
    ("BILLDESK HDFC CREDIT CARD", "OTHERS"),
    ("INT.PD:01-01-2024 TO 31-03-2024", "OTHERS"),
    ("UPIGOLD", "OTHERS"),  # word boundaries: not a UPI transaction
    ("", "OTHERS"),
])
def test_infer_mode(narration, mode):
    assert infer_mode(narration) == mode


def test_modes_are_always_in_the_schema_enum():
    for t in to_aa(_bank())["account"]["transactions"]["transaction"]:
        assert t["mode"] in {"CASH", "ATM", "CARD", "UPI", "FT", "OTHERS"}


def test_gaps_list_what_is_left_to_fill():
    gaps = aa_gaps(to_aa(_bank()))
    assert "account.profile.holders.holder[0].pan" in gaps
    assert "account.summary.branch" in gaps
    assert "account.summary.currentBalance" not in gaps  # derived from the statement
    # optional attributes are not gaps
    assert not any(g.endswith((".landline", ".address", ".exchgeRate")) for g in gaps)
    # transactions with no running balance are summarised, not listed row by row
    d = _bank()
    d["transactions"][0]["balance"] = None
    assert any("transaction[*].currentBalance (1 of 2 rows)" in g for g in aa_gaps(to_aa(d)))


def test_credit_card_gaps_include_row_level_mcc():
    gaps = aa_gaps(to_aa(_card()))
    assert any(g.startswith("account.transactions.transaction[*].mcc") for g in gaps)
    assert "account.summary.creditLimit" in gaps


def test_document_matches_xsd_patterns_when_profile_complete():
    doc = to_aa(_bank(), {"holders": [HOLDER]})
    h = doc["account"]["profile"]["holders"]["holder"][0]
    assert re.fullmatch(r"[a-zA-Z]{5}[0-9]{4}[a-zA-Z]", h["pan"])
    assert re.fullmatch(r"[^@]+@[^.]+\..+", h["email"])


# ---------------------------------------------------------------------------
# End to end: parse -> AA through the server and the CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "store", JobStore(settings.jobs_dir))
    return TestClient(server.create_app())


async def _learn(corpus, case, settings):
    data, _t, oracle = load_case(corpus, case)
    r = await parse_statement(data, case["file"], case["password"], settings=settings, client=FakeLLM(oracle))
    assert r.status == "verified"


async def test_export_aa_from_the_server_and_native_json_is_unchanged(client, corpus, cases, settings):
    case = next(c for c in cases if c["format"] == "csv")
    await _learn(corpus, case, settings)
    data, truth, _ = load_case(corpus, case)
    job_id = client.post("/api/jobs", files={"file": (case["file"], data)}).json()["id"]
    txns = client.get(f"/api/jobs/{job_id}").json()["review"]["transactions"]
    client.post(f"/api/jobs/{job_id}/approve", json={"transactions": txns})

    aa = client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"})
    assert aa.status_code == 200 and aa.headers["content-disposition"].endswith('.aa.json"')
    rows = aa.json()["account"]["transactions"]["transaction"]
    assert len(rows) == len(truth["transactions"])

    native = client.get(f"/api/jobs/{job_id}/export.json").json()
    assert set(native) == {"statement", "transactions", "validation", "status"}
    assert client.get(f"/api/jobs/{job_id}/export.json", params={"format": "xml"}).status_code == 400


async def test_export_aa_uses_the_configured_profile(client, corpus, cases, settings, tmp_path):
    case = next(c for c in cases if c["format"] == "csv")
    await _learn(corpus, case, settings)
    data, _t, _o = load_case(corpus, case)
    job_id = client.post("/api/jobs", files={"file": (case["file"], data)}).json()["id"]
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"holders": [HOLDER]}))
    settings.aa_profile = str(profile)
    doc = client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"}).json()
    assert doc["account"]["profile"]["holders"]["holder"][0]["pan"] == "ABCDE1234F"
    settings.aa_profile = str(tmp_path / "missing.json")
    assert client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"}).status_code == 500


def test_cli_prints_aa_and_reports_gaps(corpus, cases, settings, tmp_path, monkeypatch, capsys):
    case = next(c for c in cases if c["format"] == "csv" and not c["password"])
    asyncio.run(_learn(corpus, case, settings))
    monkeypatch.setenv("ASP_DATA_DIR", str(settings.data_dir))
    path = corpus / case["file"]
    code = cli_main(["parse", str(path), "--aa"])
    out = capsys.readouterr()
    doc = json.loads(out.out)
    assert code == 0 and doc["account"]["type"] in ("deposit", "credit_card")
    assert "required ReBIT field(s)" in out.err and "--profile" in out.err

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"holders": [HOLDER]}))
    cli_main(["parse", str(path), "--aa", "--profile", str(profile)])
    assert json.loads(capsys.readouterr().out)["account"]["profile"]["holders"]["holder"][0]["name"] == "A Person"

    profile.write_text(json.dumps({"holders": [dict(HOLDER, pan="bad")]}))
    assert cli_main(["parse", str(path), "--aa", "--profile", str(profile)]) == 2


async def test_server_normalises_edited_rows_and_validates_format(client, corpus, cases, settings):
    case = next(c for c in cases if c["format"] == "csv")
    await _learn(corpus, case, settings)
    data, _t, _o = load_case(corpus, case)
    job_id = client.post("/api/jobs", files={"file": (case["file"], data)}).json()["id"]
    txns = client.get(f"/api/jobs/{job_id}").json()["review"]["transactions"]
    txns[0]["amount"] = "1,234.5"  # as an API client might send it
    client.put(f"/api/jobs/{job_id}/review", json={"transactions": txns})
    row = client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"}).json()["account"]["transactions"]["transaction"][0]
    assert row["amount"] == "1234.50"

    # finding 11: the selector is validated for every extension
    assert client.get(f"/api/jobs/{job_id}/export.csv", params={"format": "aa"}).status_code == 400
    assert client.get(f"/api/jobs/{job_id}/export.csv", params={"format": "xml"}).status_code == 400
    assert client.get(f"/api/jobs/{job_id}/export.csv", params={"format": "native"}).status_code == 200

    # a row the AA mapping cannot read is a 422 with the reason, not a 500
    job = server.store.get(job_id)
    job["review"]["transactions"][0]["amount"] = "12abc"  # seeded directly: PUT /review itself rejects it
    server.store.save(job)
    r = client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"})
    assert r.status_code == 422 and "cannot read" in r.json()["detail"]


async def test_rejected_job_without_a_saved_review_still_exports(client, corpus, cases, settings):
    """finding 2: reject() leaves a review holding only the reason; export must fall back to the parse result."""
    case = next(c for c in cases if c["format"] == "csv")
    await _learn(corpus, case, settings)
    data, truth, _ = load_case(corpus, case)
    job_id = client.post("/api/jobs", files={"file": (case["file"], data)}).json()["id"]
    job = server.store.get(job_id)
    job.pop("review", None)
    server.store.save(job)
    assert client.post(f"/api/jobs/{job_id}/reject", json={"reason": "wrong bank"}).status_code == 200
    assert "statement" not in server.store.get(job_id)["review"]
    for params in ({"format": "aa"}, {}):
        r = client.get(f"/api/jobs/{job_id}/export.json", params=params)
        assert r.status_code == 200, r.text
    aa = client.get(f"/api/jobs/{job_id}/export.json", params={"format": "aa"}).json()
    assert len(aa["account"]["transactions"]["transaction"]) == len(truth["transactions"])
    assert client.get(f"/api/jobs/{job_id}/export.csv").status_code == 200


def test_cli_flags_are_not_silently_ignored(corpus, cases, settings, tmp_path, monkeypatch, capsys):
    case = next(c for c in cases if c["format"] == "csv" and not c["password"])
    asyncio.run(_learn(corpus, case, settings))
    monkeypatch.setenv("ASP_DATA_DIR", str(settings.data_dir))
    path = str(corpus / case["file"])
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"holders": [HOLDER]}))

    for argv in (["parse", path, "--aa", "--json"],           # two output formats
                 ["parse", path, "--profile", str(profile)]):  # a profile that would be ignored
        with pytest.raises(SystemExit) as e:
            cli_main(argv)
        assert e.value.code == 2
    capsys.readouterr()

    csv_path = tmp_path / "out.csv"
    assert cli_main(["parse", path, "--aa", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["account"]  # stdout is still pure JSON
    assert csv_path.read_text().splitlines()[0] == "date,description,debit,credit,balance"


def test_cli_rejects_a_bad_profile_before_parsing(tmp_path, monkeypatch, capsys):
    """finding 3: no parse (and so no model call or password prompt) for a profile that can't be used."""
    import awesome_statement_parser.pipeline as pipeline

    async def boom(*a, **k):
        raise AssertionError("the statement was parsed before the profile was checked")

    monkeypatch.setattr(pipeline, "parse_statement", boom)
    statement = tmp_path / "s.csv"
    statement.write_text("x")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"holders": [dict(HOLDER, pan="nope")]}))
    assert cli_main(["parse", str(statement), "--aa", "--profile", str(bad)]) == 2
    assert "pan" in capsys.readouterr().err
    assert cli_main(["parse", str(statement), "--aa", "--profile", str(tmp_path / "missing.json")]) == 2
    bad.write_bytes(b"\xff\xfe")
    assert cli_main(["parse", str(statement), "--aa", "--profile", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err
