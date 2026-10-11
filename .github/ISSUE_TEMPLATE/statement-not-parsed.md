---
name: A statement did not parse correctly
about: Report a layout that fails — without sharing the statement
labels: layout
---

**Please do not attach the statement, screenshots of it, or any of its contents.**

1. Bank / card issuer and statement type (savings, current, credit card):
2. File type (PDF / CSV / XLS / XLSX), number of pages:
3. What happened (verdict shown, which checks failed):
4. Run `asp inspect path/to/statement.pdf` and attach the file it writes
   (`~/.awesome-statement-parser/inspect-<name>.txt`). It masks every name, number and
   narration — check it yourself before attaching.
5. Model used (e.g. `google/gemma-4-e4b`) and `asp --help` version line:
