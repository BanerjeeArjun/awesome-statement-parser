import json
from pathlib import Path

import pytest

from awesome_statement_parser.config import Settings
from awesome_statement_parser.synth import generate_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    """Two statements per synthetic layout (PDF/CSV/XLSX, some password-protected)."""
    out = tmp_path_factory.mktemp("corpus")
    generate_corpus(out, per_profile=2, seed=11, max_txns=80, password_rate=0.3)
    return out


@pytest.fixture(scope="session")
def cases(corpus) -> list[dict]:
    return json.loads((corpus / "manifest.json").read_text())


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.llm_url = "http://127.0.0.1:9/v1"  # nothing listens here: tests never reach a real model
    s.llm_timeout_s = 1
    return s


def load_case(corpus: Path, case: dict):
    data = (corpus / case["file"]).read_bytes()
    truth = json.loads((corpus / f"{case['id']}.truth.json").read_text())
    oracle = json.loads((corpus / f"{case['id']}.spec.json").read_text())
    return data, truth, oracle


class FakeLLM:
    """Answers the layout question with a fixed spec dict and counts calls."""

    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = 0

    async def complete_json(self, system, user, schema, name, max_tokens=1500):
        self.calls += 1
        return json.loads(json.dumps(self.answer))
