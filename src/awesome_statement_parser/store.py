"""File-based job store for the review UI: <data_dir>/jobs/<id>/{job.json, source.<ext>, pages/N.png}.

Passwords are never written to disk. Page images are rendered once at parse time (while
the password is in memory), so an encrypted PDF can be previewed without keeping it.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16}", job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def create(self, filename: str, data: bytes) -> dict:
        job_id = secrets.token_hex(8)
        d = self.root / job_id
        (d / "pages").mkdir(parents=True)
        ext = Path(filename).suffix.lower() or ".bin"
        (d / f"source{ext}").write_bytes(data)
        job = {"id": job_id, "filename": Path(filename).name, "source": f"source{ext}", "status": "queued",
               "created": now(), "updated": now(), "result": None, "error": None, "review": None}
        self.save(job)
        return job

    def save(self, job: dict) -> None:
        job["updated"] = now()
        d = self._dir(job["id"])
        with _lock:
            tmp = d / "job.json.tmp"
            tmp.write_text(json.dumps(job))
            tmp.replace(d / "job.json")

    def get(self, job_id: str) -> Optional[dict]:
        try:
            p = self._dir(job_id) / "job.json"
        except KeyError:
            return None
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def source_path(self, job: dict) -> Path:
        return self._dir(job["id"]) / job["source"]

    def page_path(self, job_id: str, n: int) -> Path:
        return self._dir(job_id) / "pages" / f"{int(n)}.png"

    def list(self, limit: int = 50) -> list[dict]:
        jobs = []
        for d in self.root.iterdir():
            p = d / "job.json"
            if p.exists():
                try:
                    jobs.append(json.loads(p.read_text()))
                except ValueError:
                    continue
        jobs.sort(key=lambda j: j["created"], reverse=True)
        return jobs[:limit]

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self._dir(job_id), ignore_errors=True)
