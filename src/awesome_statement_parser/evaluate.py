"""Accuracy evaluation against a synthetic (or hand-labelled) corpus.

Modes:
  oracle — use each case's <id>.spec.json (perfect layout answer): measures the
           deterministic layers (extraction, table, summary, validation) in isolation.
  noisy  — full pipeline with a simulated, imperfect LLM: the oracle spec is degraded the
           way a small model degrades it (paraphrased/abbreviated headers, dropped fields
           and values, swapped debit/credit, wrong statement type). Measures robustness of
           everything downstream of the model, including variants and the repair round.
  llm    — full pipeline with the local LLM, cache disabled (every case is "unseen").
  cache  — full pipeline with the layout cache on (second-run behaviour).

Per case: rows exactly right (date, amount, direction, balance), description match,
the validator's verdict, and wall time. The headline safety metric is SILENT ERRORS:
cases the validator called `verified` whose rows are not exactly right. It must be 0.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings, get_settings
from .errors import ParserError
from .models import LayoutSpec
from .pipeline import parse_statement


def _norm_desc(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


@dataclass
class CaseResult:
    id: str
    profile: str
    format: str
    n_truth: int
    n_parsed: int = 0
    rows_exact: int = 0
    desc_exact: int = 0
    verdict: str = "error"
    correct: bool = False
    silent_error: bool = False
    seconds: float = 0.0
    spec_source: str = ""
    error: Optional[str] = None
    failed_checks: list[str] = field(default_factory=list)


def compare(truth: dict, parsed: list[dict]) -> tuple[int, int]:
    rows_exact = desc_exact = 0
    for t, p in zip(truth["transactions"], parsed):
        same = (
            t["date"] == p.get("date")
            and t["amount"] == p.get("amount")
            and t["direction"] == p.get("direction")
            and (t["balance"] is None or t["balance"] == p.get("balance"))
        )
        rows_exact += same
        desc_exact += _norm_desc(t["description"]) == _norm_desc(p.get("description", ""))
    return rows_exact, desc_exact


class NoisyLLM:
    """Stands in for the LLM: answers with a perturbed oracle spec (deterministic per case)."""

    SYNONYMS = {"date": "Transaction Date", "description": "Details", "debit": "Withdrawals", "credit": "Deposits",
                "balance": "Running Balance", "amount": "Amount", "reference": "Reference", "value_date": "Value Date"}

    def __init__(self, oracle: dict, seed: str):
        import random

        self.oracle = oracle
        self.rng = random.Random(seed)
        self.calls = 0

    def _header_line_id(self, prompt: str) -> str:
        from .table import tokens

        want = set().union(*(tokens(c["header"]) for c in self.oracle["columns"]))
        best, best_n = "P1L0", -1
        for line in prompt.splitlines():
            if ":" not in line or not line.startswith("P"):
                continue
            lid, text = line.split(":", 1)
            n = len(tokens(text) & want)
            if n > best_n:
                best, best_n = lid.strip(), n
        return best

    async def complete_json(self, system, user, schema, name, max_tokens=1500) -> dict:
        import copy

        self.calls += 1
        rng = self.rng
        d = copy.deepcopy(self.oracle)
        d["header_line"] = self._header_line_id(user)
        paraphrase = rng.random() < 0.15
        for c in d["columns"]:
            h = c["header"]
            if paraphrase:
                h = self.SYNONYMS.get(c["role"], h)
            else:
                r = rng.random()
                if r < 0.2:
                    h = h.lower()
                elif r < 0.35:
                    h = re.sub(r"[^\w\s]", "", h)
                elif r < 0.45 and len(h.split()) > 1:
                    h = " ".join(h.split()[:-1])
                elif r < 0.55:
                    h = h.split()[0] + "s" + (" " + " ".join(h.split()[1:]) if len(h.split()) > 1 else "")
            c["header"] = h
        roles = [c["role"] for c in d["columns"]]
        if self.calls == 1 and "debit" in roles and "credit" in roles and rng.random() < 0.15:
            for c in d["columns"]:
                c["role"] = {"debit": "credit", "credit": "debit"}.get(c["role"], c["role"])
        if self.calls == 1 and rng.random() < 0.1:
            d["statement_type"] = "credit_card" if d["statement_type"] == "bank_account" else "bank_account"
        fields = []
        for f in d["fields"]:
            if rng.random() < 0.15:
                continue  # model missed it
            if rng.random() < 0.3:
                f["value"] = None
            if rng.random() < 0.3:
                f["label"] = f["label"].lower().rstrip(" :")
            fields.append(f)
        d["fields"] = fields
        if rng.random() < 0.3:
            d["non_transaction_rows"] = []
        d.pop("date_format", None)
        return d


async def run_case(corpus: Path, case: dict, mode: str, settings: Settings) -> CaseResult:
    from .serialize import result_to_dict

    truth = json.loads((corpus / f"{case['id']}.truth.json").read_text())
    cr = CaseResult(case["id"], case["profile"], case["format"], len(truth["transactions"]))
    data = (corpus / case["file"]).read_bytes()
    spec = None
    if mode == "oracle":
        spec = LayoutSpec.from_dict(json.loads((corpus / f"{case['id']}.spec.json").read_text()))
    client = None
    if mode == "noisy":
        client = NoisyLLM(json.loads((corpus / f"{case['id']}.spec.json").read_text()), case["id"])
    t0 = time.monotonic()
    try:
        r = await parse_statement(
            data, case["file"], case.get("password"), settings=settings, spec=spec, client=client, use_cache=(mode == "cache")
        )
    except ParserError as e:
        cr.error = f"{type(e).__name__}: {e}"
        cr.seconds = round(time.monotonic() - t0, 2)
        return cr
    cr.seconds = round(time.monotonic() - t0, 2)
    d = result_to_dict(r)
    cr.n_parsed = len(d["transactions"])
    cr.rows_exact, cr.desc_exact = compare(truth, d["transactions"])
    cr.verdict = r.status
    cr.spec_source = r.spec_source
    cr.failed_checks = [c.name for c in r.validation.checks if c.status == "fail"]
    cr.correct = cr.n_parsed == cr.n_truth and cr.rows_exact == cr.n_truth
    cr.silent_error = r.status == "verified" and not cr.correct
    return cr


async def evaluate(corpus: Path, mode: str = "oracle", settings: Optional[Settings] = None, only: Optional[str] = None) -> dict:
    settings = settings or get_settings()
    corpus = Path(corpus)
    cases = json.loads((corpus / "manifest.json").read_text())
    if only:
        cases = [c for c in cases if only in c["id"]]
    results = []
    for case in cases:  # sequential: one local model, honest timings
        results.append(await run_case(corpus, case, mode, settings))
    n = len(results)
    rows_truth = sum(r.n_truth for r in results)
    summary = {
        "mode": mode,
        "cases": n,
        "correct_cases": sum(r.correct for r in results),
        "verified": sum(r.verdict == "verified" for r in results),
        "verified_and_correct": sum(r.verdict == "verified" and r.correct for r in results),
        "silent_errors": sum(r.silent_error for r in results),
        # verified with right money but narrations mostly wrong (arithmetic can't see these)
        "verified_bad_descriptions": sum(r.verdict == "verified" and r.n_truth and r.desc_exact < 0.95 * r.n_truth for r in results),
        "errors": sum(r.error is not None for r in results),
        "row_accuracy": round(sum(r.rows_exact for r in results) / rows_truth, 4) if rows_truth else None,
        "description_accuracy": round(sum(r.desc_exact for r in results) / rows_truth, 4) if rows_truth else None,
        "p50_seconds": sorted(r.seconds for r in results)[n // 2] if n else None,
        "max_seconds": max((r.seconds for r in results), default=None),
    }
    return {"summary": summary, "cases": [r.__dict__ for r in results]}


def print_report(rep: dict) -> None:
    print(f"{'case':28} {'fmt':5} {'truth':>5} {'got':>5} {'rows ok':>7} {'desc ok':>7} {'verdict':13} {'secs':>6}  notes")
    for c in rep["cases"]:
        notes = c["error"] or ",".join(c["failed_checks"])
        if c["silent_error"]:
            notes = "SILENT ERROR " + notes
        print(f"{c['id']:28} {c['format']:5} {c['n_truth']:>5} {c['n_parsed']:>5} {c['rows_exact']:>7} {c['desc_exact']:>7} {c['verdict']:13} {c['seconds']:>6}  {notes}")
    print()
    for k, v in rep["summary"].items():
        print(f"  {k:22} {v}")


def main(corpus: str, mode: str = "oracle", only: Optional[str] = None, out: Optional[str] = None) -> int:
    rep = asyncio.run(evaluate(Path(corpus), mode, only=only))
    print_report(rep)
    if out:
        Path(out).write_text(json.dumps(rep, indent=1))
    return 0 if rep["summary"]["silent_errors"] == 0 else 1
