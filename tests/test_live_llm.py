"""Runs only when a local model server is reachable (ASP_LLM_URL, default LM Studio :1234)."""
import asyncio

import httpx
import pytest

from awesome_statement_parser.config import Settings
from awesome_statement_parser.pipeline import parse_statement

from .conftest import load_case


def _llm_up() -> bool:
    try:
        return httpx.get(Settings().llm_url.rstrip("/") + "/models", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = [pytest.mark.live_llm, pytest.mark.skipif(not _llm_up(), reason="no local LLM server")]


async def test_live_unseen_layout(corpus, cases, tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.use_cache = False
    case = next(c for c in cases if c["profile"] == "tiger_card")
    data, truth, _ = load_case(corpus, case)
    r = await parse_statement(data, case["file"], case["password"], settings=s)
    assert r.timings["total"] < 60
    assert r.status in ("verified", "needs_review", "consistent")
    if r.status == "verified":
        assert len(r.transactions) == len(truth["transactions"])
