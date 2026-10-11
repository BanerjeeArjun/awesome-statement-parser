"""API + review flow through FastAPI's TestClient (background tasks run inline)."""
import json

import pytest
from fastapi.testclient import TestClient

from awesome_statement_parser import server
from awesome_statement_parser.fingerprint import LayoutCache
from awesome_statement_parser.models import LayoutSpec
from awesome_statement_parser.pipeline import parse_statement
from awesome_statement_parser.store import JobStore

from .conftest import FakeLLM, load_case


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "store", JobStore(settings.jobs_dir))
    return TestClient(server.create_app())


async def _learn(corpus, case, settings):
    data, _t, oracle = load_case(corpus, case)
    r = await parse_statement(data, case["file"], case["password"], settings=settings, client=FakeLLM(oracle))
    assert r.status == "verified"


async def test_upload_review_edit_approve(client, corpus, cases, settings):
    case = next(c for c in cases if c["format"] == "csv")
    await _learn(corpus, case, settings)  # layout known -> no model needed in the request
    data, truth, _ = load_case(corpus, case)

    r = client.post("/api/jobs", files={"file": (case["file"], data)})
    assert r.status_code == 200
    job_id = r.json()["id"]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "parsed", job.get("error")
    assert job["result"]["status"] == "verified"
    assert job["grid"]  # spreadsheet preview rows
    txns = job["review"]["transactions"]
    assert len(txns) == len(truth["transactions"])

    # an edit that breaks reconciliation is reported, not saved silently
    edited = json.loads(json.dumps(txns))
    edited[0]["amount"] = str(float(edited[0]["amount"]) + 1)
    edited[0]["edited"] = True
    v = client.post(f"/api/jobs/{job_id}/validate", json={"transactions": edited}).json()
    assert v["verdict"] == "needs_review"

    # approving the untouched rows
    r = client.post(f"/api/jobs/{job_id}/approve", json={"transactions": txns})
    assert r.json()["status"] == "approved"
    csv_text = client.get(f"/api/jobs/{job_id}/export.csv").text
    assert csv_text.splitlines()[0] == "date,description,debit,credit,balance,reference"
    assert len(csv_text.strip().splitlines()) == len(txns) + 1


async def test_password_flow_and_page_images(client, corpus, cases, settings):
    case = next(c for c in cases if c["password"])
    await _learn(corpus, case, settings)
    data, _t, _o = load_case(corpus, case)
    job_id = client.post("/api/jobs", files={"file": (case["file"], data)}).json()["id"]
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "password_required"
    client.post(f"/api/jobs/{job_id}/password", json={"password": case["password"]})
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "parsed"
    img = client.get(f"/api/jobs/{job_id}/pages/1.png")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"


def test_ui_pages_served(client):
    assert "Awesome Statement Parser" in client.get("/").text
    assert client.get("/review/0123456789abcdef").status_code == 404


def test_health_reports_unavailable_model(client):
    assert client.get("/api/health").json()["llm"] == "unavailable"
