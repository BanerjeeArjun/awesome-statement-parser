"""Settings from environment variables (prefix ASP_). No config file required."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"ASP_{name}", default)


@dataclass
class Settings:
    llm_url: str = field(default_factory=lambda: _env("LLM_URL", "http://localhost:1234/v1"))
    # Empty -> pick the first loaded model whose id contains "gemma", else the first loaded model.
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "") or "")
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT", "45")))
    # Whole-parse budget; the LLM repair round is skipped when it would exceed it.
    time_budget_s: float = field(default_factory=lambda: float(_env("TIME_BUDGET", "60")))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(Path.home() / ".awesome-statement-parser"))).expanduser())
    use_cache: bool = field(default_factory=lambda: _env("USE_CACHE", "1") != "0")

    @property
    def layouts_path(self) -> Path:
        return self.data_dir / "layouts.json"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


def get_settings() -> Settings:
    return Settings()
