"""End-to-end on the synthetic corpus: oracle spec, simulated noisy LLM, cache, semantics."""
import copy

import pytest

from awesome_statement_parser.errors import LLMUnavailable, PasswordRequired
from awesome_statement_parser.evaluate import NoisyLLM, compare
from awesome_statement_parser.models import LayoutSpec
from awesome_statement_parser.pipeline import parse_statement
from awesome_statement_parser.serialize import result_to_dict

from .conftest import FakeLLM, load_case


def assert_matches_truth(result, truth):
    d = result_to_dict(result)
    assert len(d["transactions"]) == len(truth["transactions"])
    rows_exact, _desc = compare(truth, d["transactions"])
    assert rows_exact == len(truth["transactions"])


async def test_every_layout_verifies_with_oracle_spec(corpus, cases, settings):
    for case in cases:
        data, truth, oracle = load_case(corpus, case)
        r = await parse_statement(data, case["file"], case["password"], settings=settings, spec=LayoutSpec.from_dict(oracle))
        assert r.status == "verified", (case["id"], r.validation.summary)
        assert_matches_truth(r, truth)


async def test_noisy_llm_never_yields_silent_errors(corpus, cases, settings):
    settings.use_cache = False
    for case in cases:
        data, truth, oracle = load_case(corpus, case)
        r = await parse_statement(data, case["file"], case["password"], settings=settings, client=NoisyLLM(oracle, case["id"]))
        rows_exact, _ = compare(truth, result_to_dict(r)["transactions"])
        correct = rows_exact == len(truth["transactions"]) == len(r.transactions)
        assert not (r.status == "verified" and not correct), f"silent error in {case['id']}"


async def test_layout_is_cached_after_verification(corpus, cases, settings):
    case = next(c for c in cases if c["format"] == "pdf" and not c["password"])
    data, truth, oracle = load_case(corpus, case)
    llm = FakeLLM(oracle)
    r1 = await parse_statement(data, case["file"], settings=settings, client=llm)
    assert r1.status == "verified" and r1.spec_source == "llm" and llm.calls == 1
    r2 = await parse_statement(data, case["file"], settings=settings, client=llm)
    assert r2.status == "verified" and r2.spec_source == "cache"
    assert llm.calls == 1  # the second parse never asked the model
    assert_matches_truth(r2, truth)


async def test_cached_layout_works_without_llm(corpus, cases, settings):
    same_profile = [c for c in cases if c["profile"] == "tiger_card"]
    data, _t, oracle = load_case(corpus, same_profile[0])
    await parse_statement(data, same_profile[0]["file"], same_profile[0]["password"], settings=settings, client=FakeLLM(oracle))
    # a different statement of the same layout, with the model unreachable
    data2, truth2, _ = load_case(corpus, same_profile[1])
    r = await parse_statement(data2, same_profile[1]["file"], same_profile[1]["password"], settings=settings)
    assert r.spec_source == "cache" and r.status == "verified"
    assert_matches_truth(r, truth2)


async def test_llm_down_and_no_cache(corpus, cases, settings, tmp_path):
    # a statement whose columns can be read from the rows still parses, and must reconcile
    case = next(c for c in cases if c["profile"] == "kestrel_current")
    data, truth, _o = load_case(corpus, case)
    r = await parse_statement(data, case["file"], case["password"], settings=settings)
    assert r.status in ("verified", "consistent") and any("columns were read from the rows" in w for w in r.warnings)
    if r.status == "verified":
        assert_matches_truth(r, truth)
    # a document with no table at all still reports the model as unavailable
    from reportlab.pdfgen import canvas

    p = tmp_path / "letter.pdf"
    c = canvas.Canvas(str(p))
    for i, line in enumerate(["Dear customer,", "Your new cheque book has been dispatched.", "Regards, the bank"]):
        c.drawString(72, 750 - 14 * i, line)
    c.save()
    with pytest.raises(LLMUnavailable):
        await parse_statement(p.read_bytes(), "letter.pdf", settings=settings)


async def test_double_flip_is_not_verified(corpus, cases, settings):
    """Swapped debit/credit columns + wrong statement type reconcile arithmetically;
    the column-semantics and statement-type checks must still refuse them."""
    settings.use_cache = False
    case = next(c for c in cases if c["profile"] == "nimbus_card")
    data, _t, oracle = load_case(corpus, case)
    bad = copy.deepcopy(oracle)
    for c in bad["columns"]:
        c["role"] = {"debit": "credit", "credit": "debit"}.get(c["role"], c["role"])
    bad["statement_type"] = "bank_account"
    r = await parse_statement(data, case["file"], case["password"], settings=settings, spec=LayoutSpec.from_dict(bad))
    assert r.status != "verified"
    assert {c.name for c in r.validation.checks if c.status == "fail"} & {"column_semantics", "statement_type"}


async def test_password_required(corpus, cases, settings):
    case = next(c for c in cases if c["password"])
    data, _t, oracle = load_case(corpus, case)
    with pytest.raises(PasswordRequired):
        await parse_statement(data, case["file"], None, settings=settings, spec=LayoutSpec.from_dict(oracle))
    with pytest.raises(PasswordRequired):
        await parse_statement(data, case["file"], "wrong", settings=settings, spec=LayoutSpec.from_dict(oracle))


def test_two_amount_columns_are_split_by_header_meaning():
    from awesome_statement_parser.spec import spec_from_json

    spec = spec_from_json({
        "statement_type": "bank_account", "bank_name": None, "currency": None, "header_line": "P1L1",
        "columns": [{"header": "Date", "role": "date"}, {"header": "Withdrawal (Dr)", "role": "amount"},
                    {"header": "Deposit (Cr)", "role": "amount"}, {"header": "Balance", "role": "balance"}],
        "fields": [], "non_transaction_rows": [],
    })
    assert spec.roles() == ["date", "debit", "credit", "balance"]


def test_header_with_cell_separator_is_split():
    from awesome_statement_parser.spec import spec_from_json

    spec = spec_from_json({
        "statement_type": "bank_account", "bank_name": None, "currency": None, "header_line": "P1L1",
        "columns": [{"header": "Txn Date | Value", "role": "date"}, {"header": "Debit", "role": "debit"},
                    {"header": "Credit", "role": "credit"}],
        "fields": [], "non_transaction_rows": [],
    })
    assert [(c.header, c.role) for c in spec.columns][:2] == [("Txn Date", "date"), ("Value", "value_date")]


async def test_table_after_long_preamble(tmp_path, settings):
    """New SBI-style format: ~1.5 pages of account information (with dated amount lines and
    summary tables) before the transaction table, which starts mid page 2."""
    from awesome_statement_parser.extract import extract
    from awesome_statement_parser.spec import render_document, table_hint
    from awesome_statement_parser.synth import generate_corpus
    from awesome_statement_parser.table import find_table_start

    import json

    m = generate_corpus(tmp_path, per_profile=1, seed=5, profiles=["ashoka_savings"], password_rate=0)[0]
    data = (tmp_path / m["file"]).read_bytes()
    doc = extract(data, m["file"])
    start = find_table_start(doc)
    assert start.page == 2  # not the dated "Balance as on" line or standing instructions on page 1
    header = next(l for l in doc.pages[1].lines if l.text.startswith("Txn Date"))
    assert header.id in render_document(doc)  # the model gets to see the real table header
    assert header.id in table_hint(doc)
    oracle = json.loads((tmp_path / f"{m['id']}.spec.json").read_text())
    r = await parse_statement(data, m["file"], settings=settings, spec=LayoutSpec.from_dict(oracle))
    assert r.status == "verified"
    assert len(r.transactions) == m["n_txns"]


def test_header_row_pasted_into_every_column():
    from awesome_statement_parser.spec import spec_from_json

    row = "Txn Date | Value | Description | Ref No./Cheque | Debit"
    roles = ["date", "value_date", "description", "reference", "debit", "credit", "balance"]
    spec = spec_from_json({
        "statement_type": "bank_account", "bank_name": None, "currency": None, "header_line": "P2L45",
        "columns": [{"header": row, "role": r} for r in roles], "fields": [], "non_transaction_rows": [],
    })
    assert spec.roles() == roles
    assert [c.header for c in spec.columns] == ["Txn Date", "Value", "Description", "Ref No./Cheque", "Debit", "credit", "balance"]


async def test_table_without_readable_header(tmp_path, settings):
    """New SBI format: the column header is an image/banner (only 'Balance' is text), empty
    cells are '-', and a narration's first line sits just above the dated line. Columns
    must be read from the rows themselves — even when the model names the wrong table."""
    import json

    from awesome_statement_parser.extract import extract
    from awesome_statement_parser.spec import table_hint
    from awesome_statement_parser.synth import generate_corpus

    m = generate_corpus(tmp_path, per_profile=1, seed=8, profiles=["ashoka_v2"], password_rate=0)[0]
    data = (tmp_path / m["file"]).read_bytes()
    assert "NO readable column-header" in table_hint(extract(data, m["file"]))
    truth = json.loads((tmp_path / f"{m['id']}.truth.json").read_text())
    oracle = json.loads((tmp_path / f"{m['id']}.spec.json").read_text())
    wrong = dict(oracle, header_line="P2L30", columns=[
        {"header": "Transaction Accounts", "role": "date"}, {"header": "Account Summary", "role": "description"},
        {"header": "Balance (Rs.)", "role": "amount"}, {"header": "Balance", "role": "balance"}])
    settings.use_cache = False
    r = await parse_statement(data, m["file"], settings=settings, client=FakeLLM(wrong))
    assert r.status == "verified", r.timings.get("attempts")
    assert_matches_truth(r, truth)


async def test_headerless_table_parses_even_without_the_model(tmp_path, settings):
    import json

    from awesome_statement_parser.synth import generate_corpus

    m = generate_corpus(tmp_path, per_profile=1, seed=9, profiles=["ashoka_v2"], password_rate=0)[0]
    data = (tmp_path / m["file"]).read_bytes()
    truth = json.loads((tmp_path / f"{m['id']}.truth.json").read_text())
    settings.use_cache = False
    r = await parse_statement(data, m["file"], settings=settings)  # settings point at a dead LLM port
    assert r.status == "verified" and r.spec_source.startswith("columns_from_rows")
    assert_matches_truth(r, truth)


async def test_headerless_layout_is_reused_from_cache(tmp_path, settings):
    from awesome_statement_parser.synth import generate_corpus

    ms = generate_corpus(tmp_path, per_profile=2, seed=12, profiles=["ashoka_v2"], password_rate=0)
    first = await parse_statement((tmp_path / ms[0]["file"]).read_bytes(), ms[0]["file"], settings=settings)
    assert first.status == "verified"
    second = await parse_statement((tmp_path / ms[1]["file"]).read_bytes(), ms[1]["file"], settings=settings)
    assert second.status == "verified" and second.spec_source == "cache"
