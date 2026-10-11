# API

## Python

```python
from awesome_statement_parser import parse_statement, parse_statement_sync, result_to_dict
from awesome_statement_parser import PasswordRequired, UnsupportedFormat, ScannedDocument, LLMUnavailable

result = await parse_statement(data: bytes, filename: str, password: str | None = None)
result = parse_statement_sync(data, filename, password)          # same, blocking
```

Optional keyword arguments: `settings` (a `Settings` instance, otherwise read from the
environment), `use_cache` (bool), `spec` (a `LayoutSpec` to apply instead of asking the model).

Raises `PasswordRequired` (missing or wrong password), `UnsupportedFormat`, `ScannedDocument`,
and `LLMUnavailable` — the last only when the model server cannot be reached *and* the
statement could not be read without it (no learned layout, and columns could not be read
from the rows).

`ParseResult` fields: `status`, `statement` (`StatementInfo`), `transactions`, `validation`
(`ValidationReport` with `verdict`, `summary`, `checks`), `spec`, `fingerprint`, `spec_source`,
`anchors`, `unaccounted`, `warnings`, `timings`, `pages`, `document_kind`.

## Result JSON

`result_to_dict(result)` — the same shape the HTTP API returns. Money is always a string
(`"1234.50"`), never a float; dates are ISO `YYYY-MM-DD`.

```json
{
  "status": "verified",
  "statement": {
    "statement_type": "bank_account",
    "bank_name": "…", "account_number": "XXXXXXXX1234", "currency": "INR",
    "period_start": "2025-04-01", "period_end": "2025-04-30",
    "opening_balance": "12000.00", "closing_balance": "15500.00",
    "total_debits": "4500.00", "total_credits": "8000.00",
    "debit_count": 12, "credit_count": 3,
    "provenance": {"opening_balance": {"label": "Opening Balance", "text": "12,000.00", "line_id": "P1L6"}}
  },
  "transactions": [
    {
      "id": 1, "date": "2025-04-02", "description": "UPI/…/GROCER",
      "amount": "450.00", "direction": "debit", "balance": "11550.00",
      "reference": "UPI-41234", "value_date": "2025-04-02",
      "date_text": "02/04/25", "amount_text": "450.00", "balance_text": "11,550.00",
      "direction_source": "column",
      "source": {"page": 1, "line_ids": ["P1L9", "P1L10"], "bbox": [36.0, 210.4, 559.1, 229.8]},
      "edited": false
    }
  ],
  "validation": {
    "verdict": "verified",
    "summary": "All rows reconcile against the statement (…).",
    "checks": [{"name": "balance_chain", "status": "pass", "message": "…", "rows": [], "details": {}}]
  },
  "spec_source": "llm",
  "timings": {"extract": 0.2, "llm": 14.1, "apply": 0.03, "total": 14.5}
}
```

`direction` is from the account holder's side: `debit` = money out (or a card charge),
`credit` = money in (or a card payment/refund). `source.bbox` is in PDF points from the
top-left of the page — the review UI uses it to highlight rows.

## HTTP

Start with `asp serve`, or mount the router in your own FastAPI app:

```python
from awesome_statement_parser.server import router
app.include_router(router, prefix="/statements")
```

| Method | Path | |
|---|---|---|
| `POST` | `/api/parse` | synchronous parse; multipart `file`, optional `password`. 401 = password required/incorrect, 422 = unsupported or scanned, 503 = model unavailable |
| `POST` | `/api/jobs` | start a background parse for the review UI; returns the job summary |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | list jobs / full job with result and review state |
| `POST` | `/api/jobs/{id}/password` | supply a password (`{"password": "…"}`) for a job waiting on one |
| `GET` | `/api/jobs/{id}/pages/{n}.png` | page image (rendered at parse time) |
| `POST` | `/api/jobs/{id}/validate` | re-run the checks on edited rows without saving (`{"transactions": […], "statement": {…}, "resolved_lines": […]}`) |
| `PUT` | `/api/jobs/{id}/review` | save edits |
| `POST` | `/api/jobs/{id}/approve`, `/reject` | finish the review (`reject` takes `{"reason": "…"}`) |
| `GET` | `/api/jobs/{id}/export.csv`, `/export.json` | the reviewed (or original) result |
| `GET` | `/api/layouts` | learned layouts |
| `GET` | `/api/health` | version and model availability |
| `DELETE` | `/api/jobs/{id}` | delete a job and its files |

The UI pages are `/` and `/review/{id}`; all their requests use relative paths, so they work
under any mount prefix.
