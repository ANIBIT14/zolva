from pathlib import Path

from zolva.bridge import Message
from zolva.sessions import InMemorySessionStore, SqliteSessionStore


async def test_inmemory_roundtrip_and_isolation() -> None:
    store = InMemorySessionStore()
    await store.append("s1", [Message(role="user", content="a")])
    await store.append("s2", [Message(role="user", content="OTHER")])
    await store.append("s1", [Message(role="assistant", content="b")])
    hist = await store.history("s1")
    assert [m.content for m in hist] == ["a", "b"]
    assert await store.history("unknown") == []


async def test_sqlite_roundtrip_persists(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    store = SqliteSessionStore(db)
    await store.append(
        "s1", [Message(role="user", content="a"), Message(role="assistant", content="b")]
    )
    reopened = SqliteSessionStore(db)
    hist = await reopened.history("s1")
    assert [m.content for m in hist] == ["a", "b"]
    assert hist[1].role == "assistant"


async def test_sqlite_preserves_tool_calls(tmp_path: Path) -> None:
    from zolva.bridge import ToolCall

    store = SqliteSessionStore(tmp_path / "s.db")
    msg = Message(
        role="assistant", content="", tool_calls=[ToolCall(id="1", name="t", args={"x": 1})]
    )
    await store.append("s1", [msg])
    hist = await store.history("s1")
    assert hist[0].tool_calls[0].args == {"x": 1}
