"""AuditStore protocol + incremental verification semantics."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zolva.audit import AuditLog, InMemoryAuditStore, scorecard
from zolva.bus import Step
from zolva.dashboard import create_app


def step(sid: str, step_type: str = "user_msg", text: str = "hi") -> Step:
    return Step(type=step_type, session_id=sid, agent="a", data={"text": text})  # type: ignore[arg-type]


# --- the chain logic is storage-independent --------------------------------


def test_chain_works_on_in_memory_store() -> None:
    store = InMemoryAuditStore()
    log = AuditLog(store)
    log.append(step("s1"))
    log.append(step("s1", "response", "ok"))
    log.append(step("s2"))
    assert log.verify()
    assert scorecard(log).sessions == 2
    # tamper a middle row's payload: chain must break
    rid, ts, sid, agent, typ, _data, prev, digest = store._rows[1]
    store._rows[1] = (rid, ts, sid, agent, typ, '{"text": "FORGED"}', prev, digest)
    assert not log.verify()


def test_sqlite_and_memory_stores_agree_on_hashes(tmp_path: Path) -> None:
    mem = AuditLog(InMemoryAuditStore())
    sql = AuditLog(tmp_path / "a.db")
    for log in (mem, sql):
        log.append(step("s1"), ts="2026-07-16T00:00:00+00:00")
        log.append(step("s1", "response", "ok"), ts="2026-07-16T00:00:01+00:00")
    mem_rows = mem._store.rows()
    sql_rows = sql._store.rows()
    assert [r[7] for r in mem_rows] == [r[7] for r in sql_rows]  # identical digests


# --- incremental verification semantics -------------------------------------


def tamper(db: Path, row_id: int, data: str = '{"text": "FORGED"}') -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit SET data = ? WHERE id = ?", (data, row_id))


def test_incremental_catches_tamper_after_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    log = AuditLog(db)
    log.append(step("s1"))
    assert log.verify(incremental=True)  # checkpoint at row 1
    log.append(step("s2"))
    tamper(db, 2)
    assert not log.verify(incremental=True)


def test_incremental_catches_boundary_rewrite(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    log = AuditLog(db)
    log.append(step("s1"))
    log.append(step("s2"))
    assert log.verify(incremental=True)  # checkpoint at row 2
    tamper(db, 2)  # rewrite the proven boundary itself
    assert not log.verify(incremental=True)


def test_incremental_tradeoff_full_pass_still_catches_old_edits(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    log = AuditLog(db)
    log.append(step("s1"))
    log.append(step("s2"))
    assert log.verify(incremental=True)
    tamper(db, 1)  # edit BEFORE the proven boundary
    assert log.verify(incremental=True)  # documented miss: prefix is trusted
    assert not log.verify()  # the default full pass catches it
    assert not log.verify(incremental=True)  # and the checkpoint was not advanced past it


def test_fresh_instance_always_full_verifies(tmp_path: Path) -> None:
    db = tmp_path / "a.db"
    log = AuditLog(db)
    log.append(step("s1"))
    assert log.verify(incremental=True)
    tamper(db, 1)
    assert not AuditLog(db).verify()  # zolva scorecard path


# --- dashboard hybrid cadence ------------------------------------------------


def test_dashboard_periodic_full_pass_catches_old_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zolva.dashboard as dash

    monkeypatch.setattr(dash, "_FULL_VERIFY_EVERY", 3)
    db = tmp_path / "audit.sqlite"
    log = AuditLog(db)
    log.append(step("s1"))
    log.append(step("s1", "response", "ok"))
    client = TestClient(create_app(None, db))

    assert client.get("/api/stats").json()["chain_ok"] is True  # call 0: full
    tamper(db, 1)
    # call 1 is incremental and trusts the proven prefix; the cadence's next
    # full pass must flag the rewrite
    results = [client.get("/api/stats").json()["chain_ok"] for _ in range(3)]
    assert results[-1] is False
