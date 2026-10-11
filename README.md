# Awesome Statement Parser

Parse **any** bank account or credit card statement — PDF (including password-protected),
CSV, XLS, XLSX — into verified transactions, **entirely on your machine**. No per-bank
adapters: a small local model (Gemma 4 via LM Studio) reads each new layout once, and a
strict arithmetic harness proves every row against the numbers the bank printed.

## How it works

```
file ──► LiteParse (words + geometry) ──► layout fingerprint ──► cached layout? ──yes──┐
                                                    │ no                              │
                                                    ▼                                 │
                                   Gemma: "describe this layout" (1 call)             │
                                   column roles · summary labels · statement type     │
                                                    │                                 │
                                                    ▼                                 ▼
                              deterministic table reader (Python, every row) ◄────────┘
                                                    │
                                                    ▼
                         reconciliation harness ──► verified / consistent / needs review
                                                    │
                                         verified ──► layout cached (next time: no LLM)
```

* **The model never transcribes transactions.** It answers one small question per new
  layout: which column is the date / narration / debit / credit / balance, what the
  printed summary labels are ("Opening Balance", "Total Amount Due", …), and whether it is
  a bank or card statement. Python reads every row from the document geometry.
* **Layouts are learned.** The table header is fingerprinted; once a statement verifies,
  its layout is cached (`~/.awesome-statement-parser/layouts.json`) and later statements
  in that layout parse in well under a second without the model.
* **Cheap self-repair.** If the first reading fails the checks, deterministic variants
  (debit/credit swapped, other sign convention) are tried in milliseconds, then one LLM
  repair call that is told exactly which check failed — only if the time budget allows.

### What "verified" means

A statement is **verified** only when the extracted rows are tied, to the paisa, to numbers
the bank printed independently of those rows — and nothing contradicts them:

| Check | Catches |
|---|---|
| Running-balance chain from the printed opening balance through every row (incl. B/F, C/F rows, end-of-day-only balances, newest-first order) | a missing, extra, mis-signed or mis-read row — at the exact row |
| Opening + credits − debits = closing (cards: + debits − credits) | anything that changes the total |
| Σ debits / Σ credits / counts vs. the printed summary | errors on statements without a balance column |
| Coverage: every amount-bearing line in the table is used or explicitly a non-transaction | rows the reader skipped |
| Column semantics: a printed "Withdrawal/Debit/Dr" column is never mapped as credit | a debit/credit swap that cancels out against a wrong statement type — the one mistake arithmetic alone cannot see |
| Statement type vs. the document's wording | bank-vs-card confusion |

Verdicts: **verified** (reconciled), **consistent** (internally coherent, but nothing printed
to anchor it — e.g. a CSV export with no opening balance), **needs review** (a check failed,
or nothing to check against), **failed** (no rows found). Nothing is ever marked verified
on the model's say-so.

## Install

```bash
git clone <this repo> && cd awesome-statement-parser
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run [LM Studio](https://lmstudio.ai), load a Gemma 4 model (`google/gemma-4-e4b` works on a
16 GB Mac) and start the local server (default `http://localhost:1234`).

## Use

```bash
asp serve                         # review UI at http://127.0.0.1:8765
asp parse statement.pdf           # CLI summary (prompts for a password if needed)
asp parse statement.pdf --json    # full result
asp parse statement.pdf --csv out.csv
asp layouts                       # layouts learned so far
```

**Review UI** — upload a statement; the left pane shows the document, the right pane the
parsed statement with the reconciliation verdict and every check. Click a row to highlight
it in the document; edit, insert or delete rows and the checks re-run live; then
**Approve** or **Reject**. Export as CSV or JSON.

**As a library**

```python
from awesome_statement_parser import parse_statement, result_to_dict

result = await parse_statement(file_bytes, "statement.pdf", password=None)
result.status          # "verified" | "consistent" | "needs_review" | "failed"
result.transactions    # date, description, amount (Decimal), direction, balance, source bbox
```

**Inside another FastAPI app**

```python
from awesome_statement_parser.server import router
app.include_router(router, prefix="/statement-parser")
```

HTTP API: `POST /api/parse` (synchronous, multipart `file` + optional `password`),
`POST /api/jobs` + `GET /api/jobs/{id}` (background job for the review UI),
`POST /api/jobs/{id}/validate | approve | reject`, `GET /api/jobs/{id}/export.csv`,
`GET /api/health`.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `ASP_LLM_URL` | `http://localhost:1234/v1` | any OpenAI-compatible server |
| `ASP_LLM_MODEL` | first loaded Gemma model | |
| `ASP_LLM_TIMEOUT` | `45` | seconds per model call |
| `ASP_TIME_BUDGET` | `60` | whole-parse budget; the repair call is skipped beyond it |
| `ASP_DATA_DIR` | `~/.awesome-statement-parser` | learned layouts + review jobs |
| `ASP_USE_CACHE` | `1` | `0` to always ask the model |

## Synthetic data and accuracy

```bash
asp synth corpus/ --per-profile 10          # 10 fictional layout families × 10 statements
asp eval corpus/ --mode oracle              # deterministic layers with a perfect layout answer
asp eval corpus/ --mode noisy               # simulated imperfect model (paraphrased headers,
                                            # dropped fields, swapped columns, wrong type)
asp eval corpus/ --mode llm                 # the real local model, every layout unseen
```

The generator renders PDFs (some password-protected), CSVs and XLSX files with exact ground
truth, covering: separate debit/credit columns, single signed amounts, Dr/Cr suffixes and
Dr/Cr columns, two-line headers, wrapped and middle-aligned narrations, newest-first order,
end-of-day-only balances, B/F and C/F rows, multiple card sections, headers not repeated
on later pages, summary boxes above or below the table. All banks, people and numbers are
fictional.

The headline metric is **silent errors**: statements the harness called *verified* whose
rows are not exactly right. It must be 0.

## Limits

* Scanned/photographed statements: LiteParse's OCR is tried when a PDF has no text layer,
  but this path is not yet evaluated.
* A statement that prints neither balances nor totals can't be verified — it lands in
  *needs review* by design.
* Descriptions are taken as printed; categorisation is out of scope.

## Tests

```bash
pytest -m "not live_llm"     # offline suite (~2 s)
pytest -m live_llm           # also exercises a running LM Studio
```
