"""Command line: asp parse | serve | synth | eval | layouts"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path


def _parse(args) -> int:
    from .errors import ParserError, PasswordRequired
    from .pipeline import parse_statement
    from .serialize import result_to_dict

    data = Path(args.file).read_bytes()
    password = args.password
    for attempt in range(2):
        try:
            r = asyncio.run(parse_statement(data, Path(args.file).name, password, use_cache=not args.no_cache))
            break
        except PasswordRequired as e:
            if attempt or args.password or not sys.stdin.isatty():
                print(f"error: {e}", file=sys.stderr)
                return 2
            password = getpass.getpass("Statement password: ")
        except ParserError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    d = result_to_dict(r)
    if args.json:
        print(json.dumps(d, indent=1))
        return 0 if r.status in ("verified", "consistent") else 1
    st = d["statement"]
    print(f"{Path(args.file).name}: {r.status.upper()}  ({len(r.transactions)} rows, {d['timings'].get('total')}s, layout: {r.spec_source})")
    print(f"  {st.get('bank_name') or '?'} · {st['statement_type']} · {st.get('period_start')} → {st.get('period_end')}")
    print(f"  opening {st.get('opening_balance')} · closing {st.get('closing_balance')}")
    for c in r.validation.checks:
        if c.status != "skip":
            print(f"  [{c.status:4}] {c.name}: {c.message}")
    if args.csv:
        import csv

        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "description", "debit", "credit", "balance"])
            for t in d["transactions"]:
                w.writerow([t["date"], t["description"], t["amount"] if t["direction"] == "debit" else "",
                            t["amount"] if t["direction"] == "credit" else "", t.get("balance") or ""])
        print(f"  wrote {args.csv}")
    return 0 if r.status in ("verified", "consistent") else 1


def _serve(args) -> int:
    import uvicorn

    print(f"Awesome Statement Parser → http://{args.host}:{args.port}")
    uvicorn.run("awesome_statement_parser.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


def _synth(args) -> int:
    from .synth import generate_corpus

    m = generate_corpus(Path(args.out), per_profile=args.per_profile, seed=args.seed, profiles=args.profiles or None)
    print(f"wrote {len(m)} statements to {args.out}")
    return 0


def _eval(args) -> int:
    from .evaluate import main

    return main(args.corpus, args.mode, args.only, args.out)


def _inspect(args) -> int:
    from .errors import PasswordRequired
    from .inspect import inspect_file

    password = args.password
    for attempt in range(2):
        try:
            out = asyncio.run(inspect_file(Path(args.file), password))
            break
        except PasswordRequired as e:
            if attempt or not sys.stdin.isatty():
                print(f"error: {e}", file=sys.stderr)
                return 2
            password = getpass.getpass("Statement password (not stored): ")
    print(f"wrote a masked structure report (no names, numbers or narrations) to {out}")
    return 0


def _layouts(args) -> int:
    from .config import get_settings
    from .fingerprint import LayoutCache

    data = LayoutCache(get_settings().layouts_path).all()
    for key, entries in data.items():
        for e in entries:
            s = e["spec"]
            print(f"{key}  {s.get('bank_name') or '?':30} {s['statement_type']:12} uses={e.get('uses', 0)}  "
                  + " | ".join(f"{c['header']}={c['role']}" for c in s["columns"]))
    if not data:
        print("no learned layouts yet")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="asp", description="Awesome Statement Parser — local bank & credit card statement parsing")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("parse", help="parse one statement file")
    a.add_argument("file")
    a.add_argument("-p", "--password")
    a.add_argument("--json", action="store_true", help="print the full JSON result")
    a.add_argument("--csv", help="also write transactions to this CSV file")
    a.add_argument("--no-cache", action="store_true", help="ignore learned layouts (always ask the model)")
    a.set_defaults(fn=_parse)

    a = sub.add_parser("serve", help="run the review UI + API")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8765)
    a.set_defaults(fn=_serve)

    a = sub.add_parser("synth", help="generate a synthetic statement corpus with ground truth")
    a.add_argument("out")
    a.add_argument("--per-profile", type=int, default=10)
    a.add_argument("--seed", type=int, default=7)
    a.add_argument("--profiles", nargs="*")
    a.set_defaults(fn=_synth)

    a = sub.add_parser("eval", help="measure accuracy on a corpus")
    a.add_argument("corpus")
    a.add_argument("--mode", choices=["oracle", "noisy", "llm", "cache"], default="oracle")
    a.add_argument("--only", help="substring filter on case ids")
    a.add_argument("--out", help="write the JSON report here")
    a.set_defaults(fn=_eval)

    a = sub.add_parser("inspect", help="write a privacy-safe structure report for a statement that fails")
    a.add_argument("file")
    a.add_argument("-p", "--password")
    a.set_defaults(fn=_inspect)

    a = sub.add_parser("layouts", help="list learned layouts")
    a.set_defaults(fn=_layouts)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
