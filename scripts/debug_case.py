"""Debug one corpus case with its oracle spec: python scripts/debug_case.py <corpus> <case_id> [n_lines]"""
import json, sys
from pathlib import Path
from awesome_statement_parser.extract import extract
from awesome_statement_parser.models import LayoutSpec
from awesome_statement_parser.pipeline import run_spec
from awesome_statement_parser.spec import render_line

corpus, cid = Path(sys.argv[1]), sys.argv[2]
nl = int(sys.argv[3]) if len(sys.argv) > 3 else 30
man = {c["id"]: c for c in json.loads((corpus / "manifest.json").read_text())}[cid]
doc = extract((corpus / man["file"]).read_bytes(), man["file"], man.get("password"))
spec = LayoutSpec.from_dict(json.loads((corpus / f"{cid}.spec.json").read_text()))
truth = json.loads((corpus / f"{cid}.truth.json").read_text())
for ln in doc.pages[0].lines[:nl]:
    print(render_line(ln))
r = run_spec(doc, spec, "oracle")
print("date_format", r.spec.date_format, "fp", r.fingerprint)
print("statement", {k: v for k, v in r.statement.__dict__.items() if v is not None and k != "provenance"})
for c in r.validation.checks:
    print(" ", c.status, c.name, c.message[:200])
bad = 0
for t, p in zip(truth["transactions"], r.transactions):
    ok = t["date"] == (p.date and p.date.isoformat()) and t["amount"] == (p.amount is not None and f"{p.amount:.2f}") and t["direction"] == p.direction
    if not ok and bad < 6:
        bad += 1
        print("TRUTH", t["date"], t["amount"], t["direction"], t["balance"], t["description"][:40])
        print("  GOT", p.date, p.amount, p.direction, p.balance, repr(p.date_text), repr(p.amount_text), p.direction_source, p.description[:40])
for u in r.unaccounted[:5]:
    print("UNACC", u.line_id, u.text[:100])
