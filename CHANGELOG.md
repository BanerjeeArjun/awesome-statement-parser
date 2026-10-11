# Changelog

## 0.1.0 — first public release

- Universal parsing of bank and credit card statements (PDF incl. password-protected, CSV,
  XLS, XLSX) with a local model describing each new layout once
- Learned-layout cache keyed by header text or, for header-less tables, column geometry
- Tables with no readable header (image or banner headers): columns read from the rows
- Long account-information preambles: the transaction table is located by its rows
- Reconciliation harness: running-balance chain, opening/closing equation, printed totals and
  counts, coverage, narrations, column semantics, statement type
- Review UI with document preview and live re-checking of edits; JSON API; mountable router
- `asp` CLI: `parse`, `serve`, `synth`, `eval`, `inspect`, `layouts`
- Synthetic statement generator (12 layout families) and evaluation harness (oracle, noisy,
  live model, cache modes)
