"""Minimal async client for an OpenAI-compatible local server (LM Studio by default).

Strict json_schema structured output, temperature 0. LM Studio rejects
response_format=json_object, and in strict mode every property must be listed in
`required` (optional values are expressed as ["type", "null"]).
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from .config import Settings
from .errors import LLMUnavailable


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Optional[str] = settings.llm_model or None

    async def model(self) -> str:
        if self._model:
            return self._model
        ids = await self.list_models()
        gemma = [m for m in ids if "gemma" in m.lower() and "embed" not in m.lower()]
        chat = [m for m in ids if "embed" not in m.lower()]
        if not (gemma or chat):
            raise LLMUnavailable(f"No model is loaded in the LLM server at {self.settings.llm_url}.")
        self._model = (gemma or chat)[0]
        return self._model

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.settings.llm_url.rstrip('/')}/models")
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise LLMUnavailable(
                f"Cannot reach the local LLM server at {self.settings.llm_url}. Start LM Studio and load a Gemma model "
                f"(or set ASP_LLM_URL / ASP_LLM_MODEL)."
            ) from e

    async def complete_json(self, system: str, user: str, schema: dict, name: str, max_tokens: int = 1500) -> dict:
        model = await self.model()
        body = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_s) as c:
                r = await c.post(f"{self.settings.llm_url.rstrip('/')}/chat/completions", json=body)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
        except httpx.TimeoutException as e:
            raise LLMUnavailable(f"The LLM did not answer within {self.settings.llm_timeout_s:.0f}s.") from e
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"LLM request failed: {e}") from e
        return parse_json(content)


def parse_json(content: str) -> dict:
    """Some local models wrap JSON in ``` fences even in json_schema mode."""
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise
