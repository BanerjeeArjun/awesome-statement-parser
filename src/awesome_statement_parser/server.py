"""FastAPI app: JSON API + the minimal review UI.

`router` is self-contained so a host app (CentsAI) can mount it:
    from awesome_statement_parser.server import router
    app.include_router(router, prefix="/statement-parser")

Run standalone:  asp serve   (or uvicorn awesome_statement_parser.server:app)
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import __version__
from .config import get_settings
from .errors import LLMUnavailable, ParserError, PasswordRequired, ScannedDocument, UnsupportedFormat
from .extract import GRID_W, extract
from .fingerprint import LayoutCache
from .llm import LLMClient
from .models import Check, LayoutSpec
from .pipeline import parse_statement
from .serialize import anchor_from_dict, info_from_dict, result_to_dict, to_jsonable, txn_from_dict, unaccounted_from_dict
from .store import JobStore, now
from .validate import validate

log = logging.getLogger("asp.server")
WEB = Path(__file__).parent / "web"
MAX_UPLOAD = 25 * 1024 * 1024

settings = get_settings()
store = JobStore(settings.jobs_dir)
_one_at_a_time = asyncio.Semaphore(1)  # one local model: parse sequentially

router = APIRouter()


# ---------------------------------------------------------------------------
# background parsing
# ---------------------------------------------------------------------------


def _render_pages(path: Path, job_id: str, password: Optional[str]) -> None:
    import liteparse

    kwargs = {"quiet": True, "dpi": 110}
    if password:
        kwargs["password"] = password
    for shot in liteparse.LiteParse(**kwargs).screenshot(str(path)):
        store.page_path(job_id, shot.page_num).write_bytes(shot.image_bytes)


def _grid_rows(data: bytes, filename: str, password: Optional[str]) -> list[list[str]]:
    doc = extract(data, filename, password)
    rows = []
    for ln in doc.pages[0].lines:
        row: list[str] = []
        for w in ln.words:
            c = int(round((w.x0 - 4) / GRID_W))
            row.extend([""] * (c + 1 - len(row)))
            row[c] = w.text
        rows.append(row)
    return rows


async def run_job(job_id: str, password: Optional[str]) -> None:
    async with _one_at_a_time:
        job = store.get(job_id)
        if job is None:
            return
        job["status"], job["error"] = "parsing", None
        job["started"] = now()
        store.save(job)
        path = store.source_path(job)
        data = path.read_bytes()
        try:
            result = await parse_statement(data, job["filename"], password, settings=settings)
        except PasswordRequired as e:
            job["status"], job["error"] = "password_required", str(e)
            store.save(job)
            return
        except (UnsupportedFormat, ScannedDocument, LLMUnavailable, ParserError) as e:
            job["status"], job["error"] = "error", str(e)
            store.save(job)
            return
        except Exception as e:  # never leave a job stuck in "parsing"
            log.exception("parse failed")
            job["status"], job["error"] = "error", f"Unexpected error: {e}"
            store.save(job)
            return
        try:
            if result.document_kind == "pdf":
                await asyncio.to_thread(_render_pages, path, job_id, password)
            else:
                job["grid"] = await asyncio.to_thread(_grid_rows, data, job["filename"], password)
        except Exception:
            log.exception("preview rendering failed")
        res = result_to_dict(result)
        job["result"] = res
        job["review"] = {"transactions": res["transactions"], "statement": res["statement"], "validation": res["validation"], "resolved_lines": []}
        job["status"] = "parsed"
        store.save(job)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ReviewBody(BaseModel):
    transactions: list[dict]
    statement: Optional[dict] = None
    resolved_lines: list[str] = []


class RejectBody(BaseModel):
    reason: str = ""


class PasswordBody(BaseModel):
    password: str


def _job_or_404(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


def revalidate(result: dict, body: ReviewBody) -> dict:
    info = info_from_dict(body.statement or result["statement"])
    txns = [txn_from_dict(t) for t in body.transactions]
    anchors = [anchor_from_dict(a) for a in result.get("anchors", [])]
    resolved = set(body.resolved_lines)
    unacc = [unaccounted_from_dict(u) for u in result.get("unaccounted", []) if u["line_id"] not in resolved]
    carried = [
        Check(c["name"], c["status"], c["message"], c.get("rows", []), c.get("details", {}))
        for c in result["validation"]["checks"]
        if c["name"] in ("column_semantics", "statement_type", "outside_table")
    ]
    report = validate(info, txns, anchors, unacc, [], extra_checks=carried)
    return to_jsonable(report)


def _summary(job: dict) -> dict:
    res = job.get("result") or {}
    st = res.get("statement") or {}
    review = job.get("review") or {}
    return {
        "id": job["id"], "filename": job["filename"], "status": job["status"], "created": job["created"],
        "updated": job["updated"], "error": job.get("error"),
        "verdict": (review.get("validation") or res.get("validation") or {}).get("verdict"),
        "bank_name": st.get("bank_name"), "statement_type": st.get("statement_type"),
        "period_start": st.get("period_start"), "period_end": st.get("period_end"),
        "transactions": len(review.get("transactions") or res.get("transactions") or []),
        "seconds": (res.get("timings") or {}).get("total"),
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.get("/api/health")
async def health():
    client = LLMClient(settings)
    try:
        models = await client.list_models()
        model = await client.model()
        return {"version": __version__, "llm": "ok", "llm_url": settings.llm_url, "model": model, "models": models}
    except LLMUnavailable as e:
        return {"version": __version__, "llm": "unavailable", "llm_url": settings.llm_url, "detail": str(e)}


@router.post("/api/jobs")
async def create_job(background: BackgroundTasks, file: UploadFile = File(...), password: Optional[str] = Form(None)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (25 MB max)")
    job = store.create(file.filename or "statement", data)
    background.add_task(run_job, job["id"], password or None)
    return _summary(job)


@router.get("/api/jobs")
async def list_jobs():
    return [_summary(j) for j in store.list()]


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _job_or_404(job_id)
    return {**job, "summary": _summary(job)}


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    _job_or_404(job_id)
    store.delete(job_id)
    return {"deleted": job_id}


@router.post("/api/jobs/{job_id}/password")
async def submit_password(job_id: str, body: PasswordBody, background: BackgroundTasks):
    job = _job_or_404(job_id)
    if job["status"] not in ("password_required", "error"):
        raise HTTPException(409, f"Job is {job['status']}")
    job["status"] = "queued"
    store.save(job)
    background.add_task(run_job, job_id, body.password)
    return _summary(job)


@router.get("/api/jobs/{job_id}/file")
async def get_file(job_id: str):
    job = _job_or_404(job_id)
    return FileResponse(store.source_path(job), filename=job["filename"])


@router.get("/api/jobs/{job_id}/pages/{n}.png")
async def get_page(job_id: str, n: int):
    _job_or_404(job_id)
    p = store.page_path(job_id, n)
    if not p.exists():
        raise HTTPException(404, "Page image not available")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/api/jobs/{job_id}/validate")
async def validate_edits(job_id: str, body: ReviewBody):
    job = _job_or_404(job_id)
    if not job.get("result"):
        raise HTTPException(409, "Job has no parse result")
    return revalidate(job["result"], body)


@router.put("/api/jobs/{job_id}/review")
async def save_review(job_id: str, body: ReviewBody):
    job = _job_or_404(job_id)
    if not job.get("result"):
        raise HTTPException(409, "Job has no parse result")
    report = revalidate(job["result"], body)
    job["review"] = {"transactions": body.transactions, "statement": body.statement or job["result"]["statement"],
                     "validation": report, "resolved_lines": body.resolved_lines}
    store.save(job)
    return {"validation": report}


@router.post("/api/jobs/{job_id}/approve")
async def approve(job_id: str, body: ReviewBody):
    job = _job_or_404(job_id)
    if not job.get("result"):
        raise HTTPException(409, "Job has no parse result")
    report = revalidate(job["result"], body)
    job["review"] = {"transactions": body.transactions, "statement": body.statement or job["result"]["statement"],
                     "validation": report, "resolved_lines": body.resolved_lines, "approved_at": now()}
    job["status"] = "approved"
    store.save(job)
    # A layout the user confirmed without edits is worth remembering.
    res = job["result"]
    if res.get("spec") and res.get("fingerprint") and not any(t.get("edited") for t in body.transactions):
        try:
            LayoutCache(settings.layouts_path).store(res["fingerprint"], LayoutSpec.from_dict(res["spec"]))
        except Exception:
            log.exception("could not cache layout")
    return _summary(job)


@router.post("/api/jobs/{job_id}/reject")
async def reject(job_id: str, body: RejectBody):
    job = _job_or_404(job_id)
    job["status"] = "rejected"
    job["review"] = {**(job.get("review") or {}), "rejected_reason": body.reason, "rejected_at": now()}
    store.save(job)
    return _summary(job)


@router.get("/api/jobs/{job_id}/export.{fmt}")
async def export(job_id: str, fmt: str):
    job = _job_or_404(job_id)
    src = job.get("review") or job.get("result")
    if not src:
        raise HTTPException(409, "Nothing to export")
    stem = Path(job["filename"]).stem
    if fmt == "json":
        payload = {"statement": src["statement"], "transactions": src["transactions"], "validation": src.get("validation"), "status": job["status"]}
        return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "description", "debit", "credit", "balance", "reference"])
        for t in src["transactions"]:
            amt = t.get("amount") or ""
            w.writerow([t.get("date") or "", t.get("description") or "",
                        amt if t.get("direction") == "debit" else "", amt if t.get("direction") == "credit" else "",
                        t.get("balance") or "", t.get("reference") or ""])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'})
    raise HTTPException(404, "Use export.csv or export.json")


@router.post("/api/parse")
async def parse_now(file: UploadFile = File(...), password: Optional[str] = Form(None)):
    """Synchronous parse for programmatic callers (no job, no UI)."""
    data = await file.read()
    try:
        async with _one_at_a_time:
            result = await parse_statement(data, file.filename or "statement", password or None, settings=settings)
    except PasswordRequired as e:
        raise HTTPException(401, str(e))
    except (UnsupportedFormat, ScannedDocument) as e:
        raise HTTPException(422, str(e))
    except LLMUnavailable as e:
        raise HTTPException(503, str(e))
    return result_to_dict(result)


@router.get("/api/layouts")
async def layouts():
    return LayoutCache(settings.layouts_path).all()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((WEB / "index.html").read_text())


@router.get("/review/{job_id}", response_class=HTMLResponse)
async def review_page(job_id: str):
    _job_or_404(job_id)
    return HTMLResponse((WEB / "review.html").read_text())


def create_app() -> FastAPI:
    app = FastAPI(title="Awesome Statement Parser", version=__version__)
    app.include_router(router)
    return app


app = create_app()
