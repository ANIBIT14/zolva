"""Feedback loop plugin: production signal → failure queue → permanent eval case.

Capture is automatic for escalations (bus `handover` steps) and manual via
`FeedbackQueue.record()` (thumbs-downs). Promotion to an eval cohort is
human-in-the-loop on purpose — auto-promotion poisons golden sets.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from zolva.bridge import Message
from zolva.bus import Step, Verdict
from zolva.config import ConfigError
from zolva.orchestrator import AgentApp


class Failure(BaseModel):
    id: int
    session_id: str
    agent: str
    kind: str  # "escalation" | "thumbs_down" | ...
    note: str
    transcript: list[Message]
    status: str  # "pending" | "accepted" | "rejected"


class FeedbackQueue:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._app: AgentApp | None = None
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS failures ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                "agent TEXT NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL, "
                "transcript TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def attach(self, app: AgentApp) -> None:
        """Auto-capture every escalation: an escalation is tomorrow's eval case."""
        self._app = app
        app.bus.on(self._observe)

    async def _observe(self, step: Step) -> Verdict | None:
        if step.type == "handover":
            await self._insert(
                step.session_id, step.agent, "escalation", str(step.data.get("reason", ""))
            )
        return None

    async def record(self, session_id: str, agent: str, signal: str, note: str = "") -> None:
        await self._insert(session_id, agent, signal, note)

    async def _insert(self, session_id: str, agent: str, kind: str, note: str) -> None:
        transcript: list[Message] = []
        if self._app is not None:
            transcript = await self._app.sessions.history(session_id)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO failures (session_id, agent, kind, note, transcript) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    agent,
                    kind,
                    note,
                    json.dumps([m.model_dump() for m in transcript]),
                ),
            )

    def _rows(self, status: str) -> list[Failure]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, session_id, agent, kind, note, transcript, status "
                "FROM failures WHERE status = ? ORDER BY id",
                (status,),
            ).fetchall()
        return [
            Failure(
                id=r[0],
                session_id=r[1],
                agent=r[2],
                kind=r[3],
                note=r[4],
                transcript=[Message.model_validate(m) for m in json.loads(r[5])],
                status=r[6],
            )
            for r in rows
        ]

    def pending(self) -> list[Failure]:
        return self._rows("pending")

    def accepted(self) -> list[Failure]:
        return self._rows("accepted")

    def accept(self, failure_id: int, cohort_path: str | Path, expect: str) -> None:
        """Promote a failure to a PERMANENT eval case — the bug can never silently return."""
        failure = self._get(failure_id)
        user_msgs = [m.content for m in failure.transcript if m.role == "user"]
        if not user_msgs:
            raise ConfigError(f"failure {failure_id}: no user message in transcript to promote")
        path = Path(cohort_path)
        if path.exists():
            cohort: dict[str, Any] = yaml.safe_load(path.read_text())
        else:
            cohort = {
                "cohort": path.stem,
                "agent": failure.agent,
                "grader": "judge",
                "min_pass_rate": 1.0,
                "cases": [],
            }
        cohort.setdefault("cases", []).append({"input": user_msgs[-1], "expect": expect})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(cohort, sort_keys=False, allow_unicode=True))
        self._set_status(failure_id, "accepted")

    def reject(self, failure_id: int) -> None:
        self._set_status(failure_id, "rejected")

    def _get(self, failure_id: int) -> Failure:
        for f in self.pending():
            if f.id == failure_id:
                return f
        raise ConfigError(f"no pending failure with id {failure_id}")

    def _set_status(self, failure_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE failures SET status = ? WHERE id = ?", (status, failure_id))

    def export_dataset(self, out_path: str | Path) -> int:
        """Accepted failures as fine-tuning JSONL — the SFT/DPO on-ramp, no training code."""
        rows = self.accepted()
        with open(out_path, "w") as f:
            for failure in rows:
                f.write(
                    json.dumps(
                        {
                            "messages": [m.model_dump() for m in failure.transcript],
                            "kind": failure.kind,
                            "note": failure.note,
                        }
                    )
                    + "\n"
                )
        return len(rows)
