"""python scripts/debug_noisy.py <corpus> <case_id> — replay a case through the noisy simulated LLM."""
import asyncio, json, sys
from pathlib import Path
from awesome_statement_parser.config import Settings
from awesome_statement_parser.evaluate import NoisyLLM
from awesome_statement_parser.pipeline import parse_statement
from awesome_statement_parser.serialize import result_to_dict

corpus, cid = Path(sys.argv[1]), sys.argv[2]
m = {c["id"]: c for c in json.loads((corpus / "manifest.json").read_text())}[cid]
cl = NoisyLLM(json.loads((corpus / f"{cid}.spec.json").read_text()), cid)
r = asyncio.run(parse_statement((corpus / m["file"]).read_bytes(), m["file"], m.get("password"), client=cl, settings=Settings(), use_cache=False))
print(cid, r.status, r.spec_source, r.timings.get("attempts"))
if r.spec:
    print(" type", r.spec.statement_type, "cols", [(c.header, c.role) for c in r.spec.columns])
    print(" fields", [(f.kind, f.label, f.value) for f in r.spec.fields], "fp", r.fingerprint)
for c in r.validation.checks:
    if c.status != "pass":
        print("  ", c.status, c.name, c.message[:160])
truth = json.loads((corpus / f"{cid}.truth.json").read_text())
d = result_to_dict(r); n = 0
for t, p in zip(truth["transactions"], d["transactions"]):
    if (t["date"], t["amount"], t["direction"]) != (p["date"], p["amount"], p["direction"]) or t["description"].replace(" ", "") != p["description"].replace(" ", ""):
        print("   T", t["date"], t["amount"], t["direction"], t["description"][:40]); print("   G", p["date"], p["amount"], p["direction"], p["description"][:40], p["amount_text"]); n += 1
        if n > 2: break
