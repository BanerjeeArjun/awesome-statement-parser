# Contributing

Thanks for helping. The project lives or dies on two things — never being silently wrong, and
never leaking anyone's financial data — so a few rules come first.

## Never commit or attach real statements

Not in issues, pull requests, test fixtures or screenshots — not even "anonymised" ones.
To report a layout that fails, attach the masked report from `asp inspect statement.pdf`
(see [docs/accuracy.md](docs/accuracy.md#reporting-a-statement-that-fails)). Test data is
always synthetic: invented banks, people and numbers.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not live_llm"          # offline suite, a few seconds
pytest -m live_llm                # also runs against a local model server if one is up
```

## Before opening a pull request

```bash
pytest -m "not live_llm"
asp synth /tmp/corpus --per-profile 6 --seed 77
asp eval /tmp/corpus --mode oracle
asp eval /tmp/corpus --mode noisy
```

Both evaluations must report **`silent_errors 0`**. If you have a local model, run
`--mode llm` on a smaller corpus too and include the summary in the PR.

## Fixing a layout that fails

1. Reproduce it with a synthetic profile: add a `Profile` to
   `src/awesome_statement_parser/synth/profiles.py` (and a rendering knob in
   `synth/generator.py` if the trait is new — a header drawn as an image, a narration line
   above the date, …). Confirm it fails.
2. Fix the general mechanism, not the bank. There are no per-bank code paths, and there should
   not be.
3. Add a test in `tests/` and keep every existing profile passing in `oracle` and `noisy` modes.

## Changing the checks

The reconciliation harness in `validate.py` and `semantics.py` is what makes "verified"
mean something. A change that turns a `fail` into a `warn`, or adds a new route to *verified*,
needs a sentence in the PR on why it cannot let a wrong parse through. The `column_semantics`
and `statement_type` checks guard a case pure arithmetic cannot see (see
[docs/how-it-works.md](docs/how-it-works.md#verification)) — please do not remove them.

## Style

Plain Python 3.11+, standard library first, type hints where they help, comments that explain
*why*. Money is always `Decimal` — never a float.
