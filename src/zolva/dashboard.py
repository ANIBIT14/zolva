"""Dashboard plugin: read-only local web UI over agent configs + the audit log.

`zolva dashboard agents/ --audit audit.sqlite` serves a single-page viewer:
the agent/tool topology from config, a live-tailing session feed with full
step transcripts (queries in, model/tool calls out), tool stats, and the
SARR scorecard with a chain-verification badge.

Requires the optional extra: pip install "zolva[dashboard]".

All dashboard queries open the audit DB read-only (sqlite `mode=ro`), so the
viewer can never touch the hash chain. Binds to 127.0.0.1 by default; put
your own auth/proxy in front before exposing it beyond localhost.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from zolva.audit import AuditLog, scorecard
from zolva.config import ConfigError, load_agents

_HTML = Path(__file__).with_name("dashboard.html")


def _ro_conn(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def topology(config_dir: str | None) -> dict[str, Any]:
    if not config_dir:
        return {"agents": []}
    try:
        agents = load_agents(config_dir)
    except ConfigError as e:
        return {"agents": [], "error": str(e)}
    return {
        "agents": [
            {
                "name": cfg.name,
                "provider": cfg.model.provider,
                "model": cfg.model.name,
                "tools": cfg.tools,
                "handoffs": cfg.handoffs,
                "guardrails": cfg.guardrails,
                "evals": cfg.evals,
                "instructions_preview": cfg.instructions.strip().splitlines()[0][:160]
                if cfg.instructions.strip()
                else "",
            }
            for cfg in agents.values()
        ]
    }


def sessions(audit_db: str, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    """Session summaries with new activity past `after_id`.

    `cursor` is the global max audit row id; the UI polls with the last cursor
    it saw and merges returned sessions, which makes live tail one cheap query.
    """
    if not Path(audit_db).is_file():
        return {"cursor": 0, "sessions": []}
    with closing(_ro_conn(audit_db)) as conn:
        (cursor,) = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()
        rows = conn.execute(
            "SELECT session_id, COUNT(*), MIN(ts), MAX(ts), MAX(id), "
            "GROUP_CONCAT(DISTINCT type), "
            "(SELECT agent FROM audit b WHERE b.session_id = a.session_id "
            " ORDER BY b.id DESC LIMIT 1) "
            "FROM audit a GROUP BY session_id HAVING MAX(id) > ? "
            "ORDER BY MAX(id) DESC LIMIT ?",
            (after_id, max(1, min(limit, 500))),
        ).fetchall()
    out = []
    for session_id, steps, started, last_ts, last_id, types_csv, agent in rows:
        types = set((types_csv or "").split(","))
        if "handover" in types:
            outcome = "escalated"
        elif "response" in types:
            outcome = "resolved"
        else:
            outcome = "active"
        out.append(
            {
                "session_id": session_id,
                "agent": agent,
                "steps": steps,
                "started": started,
                "last_ts": last_ts,
                "last_id": last_id,
                "outcome": outcome,
            }
        )
    return {"cursor": cursor, "sessions": out}


def session_steps(audit_db: str, session_id: str) -> dict[str, Any]:
    if not Path(audit_db).is_file():
        return {"session_id": session_id, "steps": []}
    with closing(_ro_conn(audit_db)) as conn:
        rows = conn.execute(
            "SELECT id, ts, agent, type, data FROM audit WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    return {
        "session_id": session_id,
        "steps": [
            {"id": row_id, "ts": ts, "agent": agent, "type": step_type, "data": json.loads(data)}
            for row_id, ts, agent, step_type, data in rows
        ],
    }


def stats(audit_db: str) -> dict[str, Any]:
    if not Path(audit_db).is_file():
        return {
            "chain_ok": True,
            "scorecard": {
                "sessions": 0,
                "resolved": 0,
                "escalated": 0,
                "sarr": 0.0,
                "containment": 0.0,
            },
            "total_steps": 0,
            "step_types": {},
            "tools": [],
            "agents": [],
            "handover_reasons": [],
            "activity": [],
        }
    with closing(_ro_conn(audit_db)) as conn:
        (total_steps,) = conn.execute("SELECT COUNT(*) FROM audit").fetchone()
        step_types = dict(conn.execute("SELECT type, COUNT(*) FROM audit GROUP BY type").fetchall())
        tools = [
            {"name": name, "calls": calls}
            for name, calls in conn.execute(
                "SELECT json_extract(data, '$.name'), COUNT(*) FROM audit "
                "WHERE type = 'tool_call' GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
        ]
        agents = [
            {"agent": agent, "sessions": n_sessions, "steps": n_steps}
            for agent, n_sessions, n_steps in conn.execute(
                "SELECT agent, COUNT(DISTINCT session_id), COUNT(*) FROM audit "
                "GROUP BY agent ORDER BY 3 DESC"
            ).fetchall()
        ]
        handover_reasons = [
            {"reason": reason, "count": count}
            for reason, count in conn.execute(
                "SELECT json_extract(data, '$.reason'), COUNT(*) FROM audit "
                "WHERE type = 'handover' GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
            ).fetchall()
        ]
        activity = [
            {"day": day, "steps": steps}
            for day, steps in conn.execute(
                "SELECT substr(ts, 1, 10), COUNT(*) FROM audit GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]
    log = AuditLog(audit_db)  # read paths only: verify() + scorecard()
    return {
        "chain_ok": log.verify(),
        "scorecard": scorecard(log).model_dump(),
        "total_steps": total_steps,
        "step_types": step_types,
        "tools": tools,
        "agents": agents,
        "handover_reasons": handover_reasons,
        "activity": activity,
    }


def create_app(config_dir: str | Path | None, audit_db: str | Path) -> Any:
    """Build the FastAPI app. Returns Any so core installs (no fastapi) can
    import this module's query functions without the extra."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    cfg = str(config_dir) if config_dir else None
    db = str(audit_db)
    app = FastAPI(title="Zolva Dashboard", openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML.read_text()

    @app.get("/api/topology")
    def api_topology() -> dict[str, Any]:
        return topology(cfg)

    @app.get("/api/sessions")
    def api_sessions(after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        return sessions(db, after_id=after_id, limit=limit)

    @app.get("/api/sessions/{session_id}/steps")
    def api_session_steps(session_id: str) -> dict[str, Any]:
        return session_steps(db, session_id)

    @app.get("/api/stats")
    def api_stats() -> dict[str, Any]:
        return stats(db)

    return app


def serve(
    config_dir: str | Path | None,
    audit_db: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8600,
) -> None:
    import uvicorn

    print(f"zolva dashboard: http://{host}:{port}  (audit={audit_db}, read-only)")
    uvicorn.run(create_app(config_dir, audit_db), host=host, port=port, log_level="warning")
