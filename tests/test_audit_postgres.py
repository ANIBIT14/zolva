"""Postgres AuditStore: the chain must behave identically to SQLite/in-memory,
and tampering a stored row must break verification.

Runs only when ZOLVA_TEST_PG_DSN points at a throwaway Postgres (CI sets it);
skipped otherwise, since the store's whole job is to be a real server. The
chain logic itself is storage-independent and covered by test_audit_store.
"""

import os
import uuid

import pytest

from zolva.audit import AuditLog
from zolva.bus import Step

DSN = os.environ.get("ZOLVA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ZOLVA_TEST_PG_DSN to test the Postgres store")


def step(sid: str, step_type: str = "user_msg", text: str = "hi") -> Step:
    return Step(type=step_type, session_id=sid, agent="a", data={"text": text})  # type: ignore[arg-type]


@pytest.fixture()
def store():  # type: ignore[no-untyped-def]
    from zolva.audit_postgres import PostgresAuditStore

    table = f"audit_test_{uuid.uuid4().hex[:8]}"
    s = PostgresAuditStore(DSN, table=table)  # type: ignore[arg-type]
    yield s
    with s._lock, s._conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}")


def test_chain_verifies_and_scorecard_counts(store) -> None:  # type: ignore[no-untyped-def]
    log = AuditLog(store)
    log.append(step("s1"))
    log.append(step("s1", "response", "ok"))
    log.append(step("s2"))
    assert log.verify()
    from zolva.audit import scorecard

    assert scorecard(log).sessions == 2


def test_tamper_breaks_the_chain(store) -> None:  # type: ignore[no-untyped-def]
    log = AuditLog(store)
    log.append(step("s1"))
    log.append(step("s2"))
    assert log.verify()
    with store._lock, store._conn.cursor() as cur:
        cur.execute(f"UPDATE {store._table} SET data = %s WHERE id = 1", ('{"text": "FORGED"}',))
    assert not log.verify()
