# How it works

The pipeline has one rule: **the language model describes the layout; it never produces data.**
Every date, amount and balance is read by deterministic code from positions in the document,
and every result is judged by arithmetic against numbers the bank printed.

## Stages

| Stage | Module | What it does |
|---|---|---|
| Extract | `extract.py` | PDF → LiteParse word boxes (exact positions); CSV/XLS/XLSX → a synthetic grid so all formats share the same code path. Password-protected PDFs and Office files are decrypted in memory. |
| Locate the table | `table.find_table_start` | The longest run of lines that start with a date and carry an amount. A preamble may hold a dated amount or two ("Balance as on …"); the transaction table is always the longest run. |
| Fingerprint | `fingerprint.py` | A hash of the table's header text — or, for header-less tables, of the column geometry. A layout that verified before is replayed from `layouts.json` with no model call. |
| Describe the layout | `spec.py`, `llm.py` | One structured-output call to the local model with a compact view: the preamble (trimmed to lines with labels or amounts), the header and first rows, and the end of the statement. Repetitive rows are elided. It returns a `LayoutSpec`: statement type, column roles in order, summary labels with their printed values, and in-table marker rows ("Balance B/F"). |
| Read the table | `table.apply_spec` | Finds the header on every page, anchors columns on it, places boundaries in the whitespace gutters, assigns each word to a column, and groups lines into rows. |
| Read the summary | `summary.py` | Finds each printed value by its label — on the same line, inside the same cell, or in the cell directly below a row of labels. The model's value is only used to choose between candidates. |
| Check | `validate.py`, `semantics.py` | The reconciliation harness (below). |
| Orchestrate | `pipeline.py` | Cache → model → cheap variants → columns read from the rows → one repair question, keeping the best-scoring attempt. |

## Reading the table

- **Column boundaries.** A header always sits inside its own column, so the boundary between
  two columns lies in the whitespace between their headers. When there are several gutters,
  alignment decides: amounts are right-aligned, so after an amount column the boundary is the
  right-most gutter; after a text column it is the left-most one. In the narrow strip between a
  text column and an amount column, words are placed by content (money goes right, words left),
  so a long narration and a long amount can both overhang without being misread.
- **Two- and three-line headers** are merged; headers the model paraphrases or merges are
  matched to the printed cells by an order-preserving alignment that scores both text and the
  usual vocabulary of each role (debit ↔ withdrawal/dr/paid out, description ↔ narration/particulars).
  Printed columns the model forgot become anonymous columns, so their contents cannot leak
  into a neighbour.
- **Rows.** A line whose date column starts with a date starts a row (a serial number or a
  date split over two lines is tolerated). Other lines attach to the nearest row: below it for
  top-aligned tables, above it when narrations are vertically centred. Lines with amounts that
  cannot be attached are recorded as *unaccounted* — and block verification.
- **Header-less tables.** When the header is an image or a coloured banner, columns are derived
  from the rows: gutters give the columns, contents give the roles (dates; amounts or "-";
  words), the rightmost always-filled amount column is the balance, and two complementary amount
  columns are debit and credit. The balance chain confirms their order.
- **Direction.** Separate debit/credit columns; a single signed amount; `Dr`/`Cr` suffixes or
  a Dr/Cr column; and, for unsigned single amounts, the movement of the running balance.

## Verification

All arithmetic uses `Decimal` and exact equality.

**Completeness evidence** — at least one must hold for *verified*:

1. **Opening + net = closing**, with both balances printed (bank: + credits − debits;
   card: + debits − credits).
2. **An anchored running-balance chain**: starting from a printed opening balance (or a B/F
   row), every row's movement must land exactly on the printed balance, through to the last
   row. Balances printed only once per day, newest-first ordering and B/F/C/F rows between
   pages are all handled. Any missing, extra, mis-signed or mis-read row breaks the chain at that row.
3. **Printed totals**: Σ debits and Σ credits both equal the printed totals.

**Contradiction checks** — any failure means *needs review*:

- every row has a date, an amount and a direction; no dates in the future;
- printed totals and debit/credit counts agree (a totals mismatch is only a warning when the
  opening → closing equation already holds, since banks sometimes total fees separately);
- **coverage**: no amount inside the table was left unaccounted for;
- **narrations**: most rows have words in their narration;
- **column semantics**: a column whose *printed* header says Withdrawal/Debit/Dr is never mapped
  as credit, and vice versa;
- **statement type**: bank vs. card agrees with the document's vocabulary.

The last two exist because of a real blind spot. Swapping the debit and credit columns *and*
treating a card statement as a bank statement (or the reverse) flips every sign twice: every
balance, total and closing equation still reconciles while every row's direction is wrong.
Arithmetic alone cannot see this, so the layout must also agree with the printed words.

**Verdicts**

| Verdict | Meaning |
|---|---|
| `verified` | completeness evidence holds and no check failed |
| `consistent` | no check failed, but nothing printed anchors the rows (e.g. a running balance with no opening balance) |
| `needs_review` | a check failed, or there was nothing to check against |
| `failed` | no transactions were found |

## Speed

A new layout costs one model call (a few thousand prompt tokens, a few hundred output tokens):
roughly 10–30 s with a 4B model on a laptop. Everything else takes milliseconds, so a learned
layout parses in well under a second. The repair question is only asked when the time budget
(`ASP_TIME_BUDGET`, 60 s) allows.
