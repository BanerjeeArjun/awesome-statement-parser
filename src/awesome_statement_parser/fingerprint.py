"""Layout fingerprints + a local cache of learned layouts.

A layout is identified by the normalised text of its transaction-table header block
(column names are what make a bank's layout distinctive). Generic headers collide across
banks ("Date | Description | Debit | Credit | Balance"), so a cached spec is reused only
if all of its summary labels also appear in the new document — and only ever on trust
that the validator then confirms it. A spec is stored only after a statement verified.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Document, LayoutSpec, Line
from .values import date_prefix, looks_like_money

_lock = threading.Lock()


def _norm_tokens(text: str) -> str:
    return " ".join(t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and not t.isdigit())


def block_key(lines: list[Line]) -> str:
    text = _norm_tokens(" ".join(ln.text for ln in lines))
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _headerish(ln: Line) -> bool:
    """Could this line be (part of) a column-header block? Same gate find_headers uses."""
    if not ln.words or date_prefix(ln.text) or any(looks_like_money(w.text) for w in ln.words):
        return False
    return any(re.search(r"[A-Za-z]", w.text) for w in ln.words)


def candidate_keys(doc: Document, max_pages: int = 2) -> list[str]:
    from .table import find_table_start  # local import: table imports nothing from here

    start = find_table_start(doc)
    if start is not None:
        max_pages = max(max_pages, start.page + 1)  # the header sits just above the first rows
    keys: list[str] = []
    for page in doc.pages[:max_pages]:
        lines = page.lines
        for i, ln in enumerate(lines):
            if not _headerish(ln):
                continue
            for size in (1, 2, 3):
                block = lines[i : i + size]
                if len(block) == size and all(_headerish(b) for b in block):
                    k = block_key(block)
                    if k not in keys:
                        keys.append(k)
    return keys


def _doc_text(doc: Document) -> str:
    return _norm_tokens(" ".join(ln.text for ln in doc.all_lines()))


class LayoutCache:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "layouts": {}}
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"version": 1, "layouts": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def lookup(self, doc: Document) -> list[tuple[str, LayoutSpec]]:
        data = self._load()["layouts"]
        if not data:
            return []
        text = _doc_text(doc)
        out = []
        keys = candidate_keys(doc)
        from .models import ColumnSpec
        from .table import columns_from_data, geometry_key

        derived = columns_from_data(doc)
        if derived:  # header-less layouts are keyed by column geometry
            probe = LayoutSpec("bank_account", [ColumnSpec("", r) for _s, r in derived], column_spans=[s for s, _r in derived])
            keys.append(geometry_key(probe))
        for key in keys:
            for entry in data.get(key, []):
                spec = LayoutSpec.from_dict(entry["spec"])
                labels = [_norm_tokens(f.label) for f in spec.fields]
                if all(l in text for l in labels if l):
                    out.append((key, spec))
        return out

    def store(self, key: str, spec: LayoutSpec) -> None:
        clean = LayoutSpec.from_dict(spec.to_dict())
        clean.header_line = None
        for f in clean.fields:
            f.value = None  # per-statement values never go in the cache
        with _lock:
            data = self._load()
            entries = data["layouts"].setdefault(key, [])
            payload = clean.to_dict()
            def same(a: dict, b: dict) -> bool:  # column positions drift; compare everything else
                strip = lambda d: {k: v for k, v in d.items() if k != "column_spans"}
                return strip(a) == strip(b)

            now = datetime.now(timezone.utc).isoformat()
            for e in entries:
                if same(e["spec"], payload):
                    e["spec"] = payload
                    e["uses"] = e.get("uses", 0) + 1
                    e["last_used"] = now
                    break
            else:
                entries.append({"spec": payload, "uses": 1, "created": now, "last_used": now})
            entries.sort(key=lambda e: e.get("last_used", ""), reverse=True)
            del entries[5:]
            self._save(data)

    def all(self) -> dict:
        return self._load()["layouts"]


def key_for_hit(doc: Document, line_ids: list[str]) -> Optional[str]:
    lines = [doc.line(i) for i in line_ids]
    lines = [l for l in lines if l is not None]
    return block_key(lines) if lines else None
