# Accuracy, synthetic data and evaluation

## The metric that matters

**Silent errors**: statements the checks called *verified* whose rows are not exactly right
(date, amount, direction and balance of every row). A parser that is sometimes unsure is
useful; one that is sometimes confidently wrong is not. Every change is held to zero silent
errors. The evaluation also reports `verified_bad_descriptions` — verified statements whose
narrations are mostly wrong — because arithmetic cannot see narrations.

## Synthetic statements

`asp synth OUT_DIR` renders statements for fictional banks with exact ground truth:

```bash
asp synth corpus/ --per-profile 10 --seed 7
```

For each statement it writes the file (`.pdf`, `.csv` or `.xlsx`; about 15% of PDFs are
password-protected with `test1234`), `<id>.truth.json` (every transaction, balances, totals)
and `<id>.spec.json` (the perfect layout answer), plus a `manifest.json`.

The layout families (`src/awesome_statement_parser/synth/profiles.py`) each reproduce a trait
seen in real statements:

| Profile | Traits |
|---|---|
| `saffron_savings` | separate withdrawal/deposit columns, wrapped narrations, summary grid at the bottom, CSV export |
| `peacock_savings` | two-line headers, `1 Apr 2024` dates, summary at the top, XLSX export |
| `lotus_current` | single amount + Dr/Cr column, `Cr` balances, opening/closing rows, B/F and C/F rows between pages |
| `banyan_savings` | newest first, signed amounts, western digit grouping |
| `monsoon_savings` | serial-number column, middle-aligned narrations, header not repeated on later pages |
| `kestrel_current` | `DDMMYY` dates, balance only at the end of each day, a "Total" row |
| `tiger_card` | card statement, `Cr` suffix for credits, reward points column, summary box |
| `comet_card` | `DR`/`CR` suffixes on every amount |
| `nimbus_card` | separate debit/credit columns without a balance, primary and add-on card sections |
| `orchid_card` | signed amounts, middle-aligned narrations |
| `ashoka_savings` | 1.5 pages of account information before the table, distractor tables with dates and amounts, long statements |
| `ashoka_v2` | as above, plus the header is a coloured banner (only "Balance" is text), "-" in empty cells, a narration line printed above the date line |

All banks, people, account numbers and merchants are invented.

## Evaluation modes

```bash
asp eval corpus/ --mode oracle   # perfect layout answer: tests extraction, table reading and checks
asp eval corpus/ --mode noisy    # a simulated imperfect model (below)
asp eval corpus/ --mode llm      # the real local model, cache off: every layout is unseen
asp eval corpus/ --mode cache    # with the layout cache: second-statement behaviour
```

The **noisy** mode stands in for a sloppy small model: it paraphrases, abbreviates or
lower-cases headers, sometimes replaces them with synonyms entirely, drops summary fields and
values, swaps the debit and credit roles, and flips the statement type. It runs through the full
pipeline — variants, fallbacks and repair — and is the fastest way to test robustness without
a GPU.

## Results

`google/gemma-4-e4b` (4-bit) in LM Studio, Apple M4, 16 GB.

| Set | Mode | Statements | Correct | Verified | Silent errors |
|---|---|---|---|---|---|
| 12 families × 6 | oracle | 72 | 72 | 72 | 0 |
| 12 families × 6 | noisy | 72 | 72 | 72 | 0 |
| 10 families × 2 | llm | 20 | 20 | 20 | 0 |
| `ashoka_savings` × 4 (long preamble) | llm | 4 | 4 | 4 | 0 |
| `ashoka_v2` × 4 (image header) | llm | 4 | 4 | 4 | 0 |

One of the 72 has a narration missing its last word in a few rows (`verified_bad_descriptions 1`):
the [LiteParse text-drop issue](../scripts/liteparse_text_drop_repro.py) in the middle-aligned
`monsoon_savings` layout. Amounts and dates are unaffected.

Live-model time per statement: median ~16 s, maximum ~35 s (statements of up to ~700 rows and
20 pages).

Real statements (not included in the repository): savings PDF, CSV and XLSX exports from two
Indian banks matched independently written per-bank parsers row for row (424, 424 and 924
rows); a 45-row PDF those parsers cannot read verified; and a 49-page statement whose table
header is an image verified with 619 rows, agreeing exactly with the same statement's XLSX export.

## Reporting a statement that fails

Do not share the statement. Run:

```bash
asp inspect statement.pdf
```

It writes `~/.awesome-statement-parser/inspect-<name>.txt`: where the table was found, the
prompt the model saw, its answer, the header search and every check — with every letter
replaced by `A` and every digit by `9`, except column-header and label vocabulary. Attach
that report to an issue. The fix usually starts by adding a synthetic profile that reproduces
the layout, so it stays covered by the evaluation from then on.
