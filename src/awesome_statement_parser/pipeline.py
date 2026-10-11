"""The orchestrator: extract -> layout spec (cache | LLM) -> table -> summary -> validate.

Order of attempts, stopping at the first VERIFIED result:
  1. specs from the layout cache whose fingerprint matches (no LLM call at all);
  2. one LLM layout call;
  3. cheap deterministic variants of that spec (debit/credit swapped, other sign convention);
  4. one LLM repair call that is told what failed — only if the time budget allows.
The best-scoring attempt is returned. A spec is cached only once a statement verifies.
"""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Optional

from .config import Settings, get_settings
from .errors import LayoutNotUnderstood, LLMUnavailable
from .extract import extract
from .fingerprint import LayoutCache, key_for_hit
from .llm import LLMClient
from .models import ColumnSpec, Document, LayoutSpec, ParseResult, ValidationReport
from .semantics import classify_statement_type, column_semantics, statement_type_check
from .spec import infer_spec
from .summary import build_statement_info
from .table import apply_spec, columns_from_data, geometry_key, resolve_directions
from .validate import validate

_RANK = {"verified": 3, "consistent": 2, "needs_review": 1, "failed": 0}


def run_spec(doc: Document, spec: LayoutSpec, source: str) -> ParseResult:
    spec = copy.deepcopy(spec)
    table = apply_spec(doc, spec)
    spec.date_format = table.date_format
    txn_lines = {lid for t in table.transactions if t.source for lid in t.source.line_ids}
    exclude = frozenset(txn_lines | {lid for h in table.header_hits for lid in h.line_ids})
    dates = [t.date for t in table.transactions]
    semantic = [column_semantics(spec, table.printed_headers), statement_type_check(doc, spec)]

    def evaluate(defaults: bool):
        txns = copy.deepcopy(table.transactions)
        info, warnings = build_statement_info(doc, spec, dates, exclude, defaults=defaults)
        if spec.statement_type == "bank_account":
            resolve_directions(txns, table.anchors, info.opening_balance)
        report = validate(info, txns, table.anchors, table.unaccounted, table.outside_money_lines, extra_checks=semantic)
        return info, warnings, txns, report

    info, warnings, txns, report = evaluate(defaults=False)
    warnings = table.notes + warnings
    if report.verdict != "verified":
        # the spec may simply have missed a printed field: try the common labels, keep them only if they help
        alt = evaluate(defaults=True)
        if _RANK[alt[3].verdict] > _RANK[report.verdict]:
            info, warnings, txns, report = alt
            warnings = table.notes + warnings
            warnings.append("Some statement totals were found by common labels the layout did not name.")
    fp = key_for_hit(doc, table.header_hits[0].line_ids) if table.header_hits else geometry_key(spec)
    if not table.header_hits and spec.column_spans:
        warnings.append("The table has no readable column header; columns were read from the rows themselves.")
    elif not table.header_hits:
        warnings.append("The table header named by the layout was not found in the document.")
    return ParseResult(
        status=report.verdict,
        statement=info,
        transactions=txns,
        validation=report,
        spec=spec,
        fingerprint=fp,
        spec_source=source,
        anchors=table.anchors,
        unaccounted=table.unaccounted,
        warnings=warnings,
        pages=[{"number": p.number, "width": p.width, "height": p.height} for p in doc.pages],
        document_kind=doc.kind,
    )


def score(r: ParseResult) -> tuple:
    fails = sum(1 for c in r.validation.checks if c.status == "fail")
    return (_RANK[r.status], -fails, len(r.transactions))


def variants(spec: LayoutSpec) -> list[tuple[str, LayoutSpec]]:
    out = []
    roles = spec.roles()
    if "debit" in roles and "credit" in roles:
        v = copy.deepcopy(spec)
        for c in v.columns:
            c.role = {"debit": "credit", "credit": "debit"}.get(c.role, c.role)
        out.append(("variant:swap_debit_credit", v))
    v = copy.deepcopy(spec)
    v.statement_type = "credit_card" if spec.statement_type == "bank_account" else "bank_account"
    out.append(("variant:statement_type", v))
    return out


def failure_feedback(r: ParseResult) -> str:
    lines = [f"- {c.name}: {c.message}" for c in r.validation.checks if c.status == "fail"]
    lines.append(f"- columns used: " + ", ".join(f"{c.header!r}={c.role}" for c in r.spec.columns))
    return "\n".join(lines[:12])


def _failed_result(doc: Document, message: str, source: str) -> ParseResult:
    from .models import Check, StatementInfo

    return ParseResult(
        status="failed",
        statement=StatementInfo(statement_type="bank_account"),
        transactions=[],
        validation=ValidationReport("failed", [Check("layout", "fail", message)], message),
        spec=None,
        fingerprint=None,
        spec_source=source,
        pages=[{"number": p.number, "width": p.width, "height": p.height} for p in doc.pages],
        document_kind=doc.kind,
    )


async def parse_document(
    doc: Document,
    *,
    settings: Optional[Settings] = None,
    client: Optional[LLMClient] = None,
    spec: Optional[LayoutSpec] = None,
    use_cache: Optional[bool] = None,
    started: Optional[float] = None,
) -> ParseResult:
    settings = settings or get_settings()
    started = started or time.monotonic()
    use_cache = settings.use_cache if use_cache is None else use_cache
    cache = LayoutCache(settings.layouts_path)
    timings: dict = {}
    attempts: list[ParseResult] = []

    def done(r: ParseResult) -> ParseResult:
        timings["total"] = round(time.monotonic() - started, 3)
        r.timings = {**r.timings, **timings}
        r.warnings = list(dict.fromkeys(r.warnings))
        if r.status == "verified" and r.fingerprint and r.spec and use_cache and r.spec_source != "cache":
            cache.store(r.fingerprint, r.spec)
        r.timings["attempts"] = [f"{a.spec_source}:{a.status}" for a in attempts]
        return r

    def best() -> ParseResult:
        return max(attempts, key=score)

    if spec is not None:
        attempts.append(run_spec(doc, spec, "provided"))
        return done(best())

    def rows_only(base: LayoutSpec):
        """Columns read from the rows themselves (and the debit/credit-swapped reading)."""
        derived = columns_from_data(doc)
        if not derived:
            return
        ds = copy.deepcopy(base)
        ds.columns = [ColumnSpec(f"({role})", role) for _span, role in derived]
        ds.column_spans = [span for span, _role in derived]
        for name, v in [("columns_from_rows", ds)] + [("columns_from_rows+" + n, x) for n, x in variants(ds)]:
            yield run_spec(doc, v, name)

    # 1. layout cache
    if use_cache:
        t = time.monotonic()
        for _key, cached in cache.lookup(doc):
            r = run_spec(doc, cached, "cache")
            attempts.append(r)
            if r.status == "verified":
                break
        timings["cache"] = round(time.monotonic() - t, 3)
        if attempts and best().status == "verified":
            return done(best())

    # 2. LLM layout call
    client = client or LLMClient(settings)
    t = time.monotonic()
    try:
        try:
            llm_spec = await infer_spec(doc, client)
        except LayoutNotUnderstood as first_error:
            # unusable answer (e.g. a summary box taken for the table): one more try, told why
            if time.monotonic() - started + (time.monotonic() - t) * 1.2 > settings.time_budget_s:
                raise
            llm_spec = await infer_spec(
                doc, client,
                feedback=f"- {first_error} The header you chose is not the transaction table's header: pick the "
                "line directly above the first row that starts with a date, and list that table's columns.",
            )
    except (LLMUnavailable, LayoutNotUnderstood) as e:
        timings["llm"] = round(time.monotonic() - t, 3)
        # No usable answer from the model: the columns can still be read from the rows, and
        # the arithmetic decides whether that reading is trustworthy.
        guess = classify_statement_type(doc)[0] or "bank_account"
        for vr in rows_only(LayoutSpec(statement_type=guess, columns=[])):
            vr.warnings.append(f"Model step failed ({e}); columns were read from the rows.")
            attempts.append(vr)
            if vr.status == "verified":
                return done(vr)
        if attempts and (isinstance(e, LayoutNotUnderstood) or best().status in ("verified", "consistent")):
            r = best()
            r.warnings.append(f"LLM step skipped: {e}")
            return done(r)
        if isinstance(e, LLMUnavailable):
            raise
        return done(_failed_result(doc, str(e), "llm"))
    timings["llm"] = round(time.monotonic() - t, 3)
    t = time.monotonic()
    r = run_spec(doc, llm_spec, "llm")
    attempts.append(r)
    timings["apply"] = round(time.monotonic() - t, 3)
    if r.status == "verified":
        return done(r)

    # 3. deterministic variants (milliseconds)
    for name, v in variants(r.spec):  # the effective spec: may hold columns read from the rows
        vr = run_spec(doc, v, name)
        attempts.append(vr)
        if vr.status == "verified":
            return done(vr)

    # 3b. the model's columns may have matched some other table (a summary box) on a page
    # whose real header is an image: read the columns from the rows instead (+ swapped)
    if not r.spec.column_spans:
        for vr in rows_only(r.spec):
            attempts.append(vr)
            if vr.status == "verified":
                return done(vr)

    # 4. one LLM repair round, if time allows (a call takes roughly as long as the first)
    elapsed = time.monotonic() - started
    if elapsed + max(timings["llm"], 5) * 1.2 < settings.time_budget_s and best().status != "consistent":
        t = time.monotonic()
        try:
            repaired = await infer_spec(doc, client, feedback=failure_feedback(best()))
            rr = run_spec(doc, repaired, "llm_repair")
            attempts.append(rr)
            if rr.status != "verified":
                for name, v in variants(rr.spec):
                    attempts.append(run_spec(doc, v, "llm_repair+" + name))
        except (LLMUnavailable, LayoutNotUnderstood) as e:
            best().warnings.append(f"Repair round failed: {e}")
        timings["llm_repair"] = round(time.monotonic() - t, 3)
    return done(best())


async def parse_statement(
    data: bytes,
    filename: str,
    password: Optional[str] = None,
    *,
    settings: Optional[Settings] = None,
    client: Optional[LLMClient] = None,
    spec: Optional[LayoutSpec] = None,
    use_cache: Optional[bool] = None,
) -> ParseResult:
    """Parse a statement file. Raises PasswordRequired / UnsupportedFormat / ScannedDocument / LLMUnavailable."""
    started = time.monotonic()
    doc = await asyncio.to_thread(extract, data, filename, password)
    t_extract = round(time.monotonic() - started, 3)
    r = await parse_document(doc, settings=settings, client=client, spec=spec, use_cache=use_cache, started=started)
    r.timings["extract"] = t_extract
    return r


def parse_statement_sync(data: bytes, filename: str, password: Optional[str] = None, **kw) -> ParseResult:
    return asyncio.run(parse_statement(data, filename, password, **kw))
