"""Dashboard plugin: read-only API over configs + audit log, live-tail cursor."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zolva.audit import AuditLog
from zolva.bus import Step
from zolva.dashboard import create_app, sessions, session_steps, stats, topology

AGENTS_YAML = """\
name: collections-agent
instructions: collections.md
model: { provider: openai, name: gpt-5 }
tools: [get_dues, send_payment_link]
handoffs: [human-escalation]
"""

SUPPORT_YAML = """\
name: support-agent
instructions: support.md
model: { provider: anthropic, name: sonnet-4-5 }
tools: [get_card_status]
handoffs: [collections-agent, human-escalation]
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    (d / "collections.yaml").write_text(AGENTS_YAML)
    (d / "collections.md").write_text("Be respectful.\nLook up dues first.")
    (d / "support.yaml").write_text(SUPPORT_YAML)
    (d / "support.md").write_text("Answer card questions.")
    return d


@pytest.fixture
def audit_db(tmp_path: Path) -> Path:
    db = tmp_path / "audit.sqlite"
    log = AuditLog(db)

    def step(sid: str, agent: str, step_type: str, data: dict) -> None:  # type: ignore[type-arg]
        log.append(Step(type=step_type, session_id=sid, agent=agent, data=data))  # type: ignore[arg-type]

    # s1: resolved with a tool call
    step("s1", "collections-agent", "user_msg", {"text": "dues?"})
    step("s1", "collections-agent", "model_call", {"provider": "openai", "model": "gpt-5"})
    step(
        "s1", "collections-agent", "tool_call", {"name": "get_dues", "args": {"customer_id": "c1"}}
    )
    step("s1", "collections-agent", "response", {"text": "You owe 4200."})
    # s2: escalated
    step("s2", "support-agent", "user_msg", {"text": "let me talk to a human"})
    step("s2", "support-agent", "handover", {"reason": "customer request"})
    # s3: active (no response yet)
    step("s3", "support-agent", "user_msg", {"text": "card lost"})
    return db


@pytest.fixture
def client(config_dir: Path, audit_db: Path) -> TestClient:
    return TestClient(create_app(config_dir, audit_db))


def test_topology_lists_agents_tools_handoffs(client: TestClient) -> None:
    data = client.get("/api/topology").json()
    by_name = {a["name"]: a for a in data["agents"]}
    assert by_name["collections-agent"]["tools"] == ["get_dues", "send_payment_link"]
    assert by_name["support-agent"]["handoffs"] == ["collections-agent", "human-escalation"]
    assert by_name["collections-agent"]["instructions_preview"] == "Be respectful."


def test_topology_bad_config_reports_error_not_500(audit_db: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "nope", audit_db))
    data = client.get("/api/topology").json()
    assert data["agents"] == [] and "error" in data


def test_topology_without_config_dir(audit_db: Path) -> None:
    client = TestClient(create_app(None, audit_db))
    assert client.get("/api/topology").json() == {"agents": []}


def test_sessions_summaries_and_outcomes(client: TestClient) -> None:
    data = client.get("/api/sessions").json()
    assert data["cursor"] == 7
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert by_id["s1"]["outcome"] == "resolved" and by_id["s1"]["steps"] == 4
    assert by_id["s2"]["outcome"] == "escalated"
    assert by_id["s3"]["outcome"] == "active"
    # newest activity first
    assert [s["session_id"] for s in data["sessions"]] == ["s3", "s2", "s1"]


def test_sessions_cursor_is_a_live_tail(client: TestClient, audit_db: Path) -> None:
    cursor = client.get("/api/sessions").json()["cursor"]
    assert client.get(f"/api/sessions?after_id={cursor}").json()["sessions"] == []
    log = AuditLog(audit_db)
    log.append(Step(type="response", session_id="s3", agent="support-agent", data={"text": "ok"}))
    data = client.get(f"/api/sessions?after_id={cursor}").json()
    assert [s["session_id"] for s in data["sessions"]] == ["s3"]
    assert data["sessions"][0]["outcome"] == "resolved"
    assert data["cursor"] == cursor + 1


def test_session_steps_ordered_with_parsed_data(client: TestClient) -> None:
    steps = client.get("/api/sessions/s1/steps").json()["steps"]
    assert [s["type"] for s in steps] == ["user_msg", "model_call", "tool_call", "response"]
    assert steps[2]["data"] == {"name": "get_dues", "args": {"customer_id": "c1"}}


def test_stats_scorecard_tools_and_chain(client: TestClient) -> None:
    data = client.get("/api/stats").json()
    assert data["chain_ok"] is True
    assert data["scorecard"]["sessions"] == 3
    assert data["scorecard"]["resolved"] == 1 and data["scorecard"]["escalated"] == 1
    assert data["tools"] == [{"name": "get_dues", "calls": 1}]
    assert data["total_steps"] == 7
    assert data["step_types"]["user_msg"] == 3
    assert data["handover_reasons"] == [{"reason": "customer request", "count": 1}]
    assert len(data["activity"]) == 1  # all seeded today


def test_stats_detects_tampering(client: TestClient, audit_db: Path) -> None:
    with sqlite3.connect(audit_db) as conn:
        conn.execute('UPDATE audit SET data = \'{"text": "FORGED"}\' WHERE id = 1')
    assert client.get("/api/stats").json()["chain_ok"] is False


def test_missing_db_serves_empty_states(config_dir: Path, tmp_path: Path) -> None:
    client = TestClient(create_app(config_dir, tmp_path / "absent.sqlite"))
    assert client.get("/api/sessions").json() == {"cursor": 0, "sessions": []}
    assert client.get("/api/sessions/x/steps").json()["steps"] == []
    st = client.get("/api/stats").json()
    assert st["scorecard"]["sessions"] == 0 and st["chain_ok"] is True
    # and the viewer never created the file: read-only promise holds
    assert not (tmp_path / "absent.sqlite").exists()


def test_index_serves_embedded_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Zolva" in r.text and "/api/sessions" in r.text


def test_dashboard_queries_do_not_write(client: TestClient, audit_db: Path) -> None:
    before = audit_db.read_bytes()
    for url in ("/api/sessions", "/api/sessions/s1/steps", "/api/stats", "/api/topology"):
        client.get(url)
    assert audit_db.read_bytes() == before


def test_cli_dashboard_wires_args(monkeypatch: pytest.MonkeyPatch) -> None:
    import zolva.dashboard
    from zolva.cli import main

    captured: dict = {}  # type: ignore[type-arg]

    def fake_serve(config_dir, audit_db, *, host, port):  # type: ignore[no-untyped-def]
        captured.update(config_dir=config_dir, audit_db=audit_db, host=host, port=port)

    monkeypatch.setattr(zolva.dashboard, "serve", fake_serve)
    assert main(["dashboard", "agents/", "--audit", "a.sqlite", "--port", "9000"]) == 0
    assert captured == {
        "config_dir": "agents/",
        "audit_db": "a.sqlite",
        "host": "127.0.0.1",
        "port": 9000,
    }


def test_demo_seed_produces_verifiable_chain(tmp_path: Path) -> None:
    from examples.dashboard_demo.seed import seed
    from zolva.audit import scorecard

    db = tmp_path / "demo.sqlite"
    rows = seed(db_path=db, n_sessions=40, days=3)
    log = AuditLog(db)
    assert log.verify()
    sc = scorecard(log)
    assert sc.sessions == 40 and sc.resolved > 0 and sc.escalated > 0
    data = sessions(str(db), limit=500)
    assert len(data["sessions"]) == 40
    assert rows == stats(str(db))["total_steps"]


def test_query_functions_direct(audit_db: Path, config_dir: Path) -> None:
    """The module-level functions work without FastAPI (core install path)."""
    assert topology(str(config_dir))["agents"]
    assert sessions(str(audit_db))["cursor"] == 7
    assert session_steps(str(audit_db), "s2")["steps"][-1]["type"] == "handover"
    assert stats(str(audit_db))["chain_ok"] is True
