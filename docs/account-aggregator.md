# Account Aggregator (ReBIT) export

A parsed statement can be exported as the **FI-data document** defined by ReBIT's Account
Aggregator specifications, shaped so that tooling written for AA data can read it. The native JSON
(`asp parse --json`, `result_to_dict`) is unchanged; this is an additional format.

> **Status.** This is an independent implementation of the published schemas. It is not
> affiliated with or certified by ReBIT or Sahamati, and it is not an FIP, an FIU or an AA — it
> does not speak the consent, fetch or encryption APIs. It turns a statement *you already hold*
> into a document with the same shape an FIP would have produced. Check it against your
> consumer's validator before relying on it.

| Statement | AA FI type | Schema |
|---|---|---|
| bank account | `deposit` | [deposit.xsd](https://github.com/Sahamati/account-aggregator-standards/blob/main/schemas/deposit/deposit.xsd) · [docs](https://specifications.rebit.org.in/api_schema/account_aggregator/documentation/deposit.html) |
| credit card | `credit_card` | [others_creditcard.xsd](https://github.com/Sahamati/account-aggregator-standards/blob/main/schemas/credit_card/others_creditcard.xsd) |

Only the decrypted `Account` payload is produced — one document per statement. The
`FIFetchResponse` envelope and the ECDH (Curve25519) encryption are transport concerns of the
FIP and the AA, not of a parser.

## Use it

```bash
asp parse statement.pdf --aa > account.json                       # prints the document; notes on stderr
asp parse statement.pdf --aa --profile aa-profile.json > account.json
```

`--aa` and `--json` are mutually exclusive, and `--profile` requires `--aa` (both are usage errors,
not silently ignored). The profile is validated *before* the statement is parsed, so a typo costs
nothing. `--csv` still works alongside `--aa`; stdout stays pure JSON.

```python
from awesome_statement_parser import parse_statement_sync, to_aa, aa_gaps, load_profile

result = parse_statement_sync(data, "statement.pdf")
doc = to_aa(result, load_profile("aa-profile.json"))   # profile is optional
aa_gaps(doc)                                           # required fields still empty
```

HTTP: `GET /api/jobs/{id}/export.json?format=aa` (the review UI has an **AA JSON** button). It
exports the *reviewed* rows, so edits made in the UI are what you get; a job with no saved review
(e.g. rejected straight away) exports the original parse. Set `ASP_AA_PROFILE` to a profile file to
have the server apply it. `format` is `native` (default) or `aa` for any extension; `aa` is only
available as `export.json` (400 otherwise). Rows that can't be read as amounts/dates give 422 with
the reason; an unusable profile file gives 500 naming the file.

`to_aa` also accepts the dict form (`result_to_dict(result)`).

**The AA document carries no verdict.** The schema has nowhere to say that a parse is *verified*
or *needs review*, so a `needs_review` statement exports as readily as a verified one. Check
`result.status` first (the CLI exit code is `0` for verified/consistent, `1` otherwise, and the
document is still printed), or export from approved review jobs only.

## JSON mapping

ReBIT publishes XSD only — there is no official JSON schema. The mapping follows the schema
one-to-one, as AA providers do: **XML attributes become keys with the same spelling, elements
become nested objects named in lower camel case** (`Account` → `account`, `Profile` → `profile`),
and **repeating elements are lists** (`holder`, `card`, `transaction`).

```json
{ "account": {
    "type": "deposit", "maskedAccNumber": "XXXXXXXX5128", "version": "2.0", "linkedAccRef": "3347a7c7-…",
    "profile": { "holders": { "type": "SINGLE", "holder": [ { "name": "…", "dob": "1990-01-31", "mobile": 919999999999,
                  "nominee": "REGISTERED", "email": "…", "pan": "ABCDE1234F", "ckycCompliance": true } ] } },
    "summary": { "currentBalance": "93124.96", "balanceDateTime": "2025-10-09T00:00:00+05:30", "type": "SAVINGS",
                 "branch": "…", "facility": "", "ifscCode": "…", "micrCode": "", "openingDate": "", "currentODLimit": "",
                 "drawingLimit": "", "status": "ACTIVE", "currency": "INR" },
    "transactions": { "startDate": "2025-08-11", "endDate": "2025-10-09", "transaction": [
      { "txnId": "dadf1f28-…", "type": "DEBIT", "mode": "UPI", "amount": "5000.00", "currentBalance": "24545.03",
        "transactionTimestamp": "2025-08-11T00:00:00+05:30", "valueDate": "2025-08-11",
        "narration": "UPI/161461003307/STARBUCKS HYDERABAD/starbucks@ybl/UPI", "reference": "1981" } ] } } }
```

Deviations from the XSD's primitive types, on purpose:

- **Amounts and balances are decimal strings** (`"5000.00"`), not `xs:float`, so the paisa-exact
  guarantee survives the export. Parse them as decimals.
- **The two `xs:dateTime` attributes carry `+05:30`** (`balanceDateTime`, `transactionTimestamp`).
  Statements print a date, not a time; AA is an Indian framework. Every other date is a plain
  `xs:date` in both schemas.
- Amounts, balances and dates are **re-read through the pipeline's own parsers** before export, so
  rows edited as `1,234.5` come out as `"1234.50"`.

## What comes from where

| AA field | Source |
|---|---|
| `maskedAccNumber`, card `maskedCardNumber` | the statement's number — or the profile's — masked to its last four characters. Everything before them becomes `X`, even if the bank's own masking showed the first digits, so a full number cannot leave through this export |
| `linkedAccRef` | the profile, else a deterministic UUID derived from bank name + masked number (the real value is assigned by a FIP) |
| deposit `summary.currentBalance` | printed closing balance, else the last running balance |
| `summary.balanceDateTime`, `transactions.startDate/endDate` | statement period, else the first/last transaction date |
| `transaction.type` / `txnType` | the parsed debit/credit direction (a card *debit* is a charge, a *credit* a payment or refund) |
| `transaction.mode` | **inferred from the narration**, first match wins: `UPI`, `ATM`, `FT` (NEFT/RTGS/IMPS/ACH/…), `CARD` (POS/ECOM/debit card/network names), `CASH`, else `OTHERS`. A transfer rail outranks the word *card*, so a credit-card bill paid by NEFT is `FT` |
| `transaction.valueDate` | the printed value date, else the transaction date |
| `transaction.txnId` | deterministic UUID of the account and the row (stable across exports) |
| card `summary` | `previousDueAmount` ← opening balance, `totalDueAmount` ← closing balance, `minDueAmount`, `dueDate`, `lastStatementDate` ← period end |
| credit card rows have no running balance | the card schema has none, so it is not exported |

Transactions keep the statement's order (not re-sorted).

## Fields a statement cannot supply

The ReBIT schema requires many attributes a statement never prints — holder DOB, mobile, PAN,
email, nominee, `ckycCompliance`, `branch`, `ifscCode`, `micrCode`, `facility`, OD limits, card
network, credit limit, `mcc`, … These keys are **always present**: filled from the *profile*
when you provide it, otherwise `""` — the placeholder ReBIT's own sample files use. Nothing is
guessed. `""` is a valid `xs:string` but not a valid date, enum, PAN, integer or boolean, so a
strict validator will reject the placeholders until you supply them.

`aa_gaps(doc)` lists what is still empty, and `asp parse --aa` prints that list to stderr.

### Profile

A JSON file; keys are the AA attribute names. A profile value wins over a derived one. Copy
[examples/aa-profile.example.json](examples/aa-profile.example.json) (all values are fictional).

```json
{
  "account": { "linkedAccRef": "your-reference" },
  "holders": [
    { "name": "A Person", "dob": "1990-01-31", "mobile": "919999999999", "nominee": "REGISTERED",
      "email": "a@example.com", "pan": "ABCDE1234F", "ckycCompliance": true }
  ],
  "summary": { "type": "SAVINGS", "status": "ACTIVE", "branch": "MG Road", "ifscCode": "HDFC0001234",
               "micrCode": "560240001", "openingDate": "2015-04-01", "facility": "OD",
               "currentODLimit": "0", "drawingLimit": "0" },
  "cards": [ { "cardType": "VISA", "primary": "YES", "issuedDate": "2021-06-01" } ]
}
```

- `holders`: one entry → `SINGLE`, more → `JOINT`. `cards` (credit cards only; an error on a bank
  statement) attach to the first holder.
- `summary` takes the attributes of that FI type (deposit: `type`, `status`, `branch`, `ifscCode`, …;
  card: `creditLimit`, `cashLimit`, `currentDue`, …). Unknown keys, typos and keys of the other FI
  type are errors.
- Values are strings (whole numbers are accepted and converted; floats and other types are refused).
- Everything the schema constrains is checked up front: PAN, email, mobile (digits, no leading zero
  or `+`), real `YYYY-MM-DD` dates (not `20250102` or week dates), decimal amounts, and the enums
  (`nominee`, `type`, `status`, `facility`, `cardType`, `primary`). Any problem — including a wrong JSON
  type, a missing or non-UTF-8 file — is a `ProfileError` (CLI exit code 2), never a traceback or
  a document ReBIT would reject.
- A profile describes one account. It holds personal data (PAN, date of birth, mobile), so keep it
  out of version control: `.gitignore` already excludes `aa-profile*.json` (but not
  `*.example.json`), which is why the examples use that name.

## Known limits

- **Two versions of the deposit schema are published.** The Sahamati GitHub copy marks `facility`,
  `currentODLimit`, `drawingLimit` and the holder's `email`/`pan`/`ckycCompliance` optional;
  [specifications.rebit.org.in](https://specifications.rebit.org.in/api_schema/account_aggregator/documentation/deposit.html)
  marks them required. The export always emits the stricter set, which also satisfies the other.
- **The credit card XSD allows exactly one `Transaction`** per `Transactions` (no `maxOccurs`) — an
  upstream omission. The export emits every transaction as a list; a literal XSD validator will
  flag statements with more than one.
- `mcc` (merchant category code) is not on statements and stays `""`; so does the card `currentDue`,
  whose meaning ("current month outstanding") is ambiguous against the printed totals — set it in the profile.
- Only the `deposit` and `credit_card` FI types are covered (not recurring/term deposits).

## How it was checked

The JSON is mapped back to XML (keys → attributes, objects → elements, lists → repeated elements) and
validated with an XSD 1.1 validator against the two published schemas. With a complete profile, 24
documents generated from the synthetic test corpus (16 bank accounts, 8 credit cards) validate
with no errors except the credit card single-`Transaction` limit above. Without a profile, the only
errors are the empty placeholders. The offline test suite (`tests/test_aa.py`) asserts the required
keys, enums and patterns copied from the XSDs, but does not itself run an XSD validator, to keep
the dev dependencies unchanged.
