"""Redaction plugin: provider sees masked content, session/audit keep the truth."""

from pathlib import Path

import pytest

from tests.test_orchestrator import make_cfg
from zolva.bridge import LLMResponse, Message, ToolCall
from zolva.bridge.fake import FakeAdapter
from zolva.config import ConfigError
from zolva.orchestrator import AgentApp
from zolva.redaction import BUILTIN_PATTERNS, RedactingAdapter, Redactor
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


@pytest.mark.parametrize(
    ("kind", "sample"),
    [
        ("card", "pay with 4111 1111 1111 1111 today"),
        ("card", "pay with 4111-1111-1111-1111 today"),
        ("email", "reach me at first.last+x@bank.co.in please"),
        ("phone", "call +91 98765 43210 now"),
        ("aadhaar", "id is 1234 5678 9012 ok"),
        ("ssn", "ssn 123-45-6789 here"),
    ],
)
def test_builtin_patterns_mask_and_leave_neighbors(kind: str, sample: str) -> None:
    r = Redactor({kind: BUILTIN_PATTERNS[kind]})
    out = r.redact(sample)
    assert f"[REDACTED:{kind}]" in out
    first, last = sample.split(" ", 1)[0], sample.rsplit(" ", 1)[-1]
    assert out.startswith(first) and out.endswith(last)


def test_redact_messages_returns_new_objects() -> None:
    r = Redactor({"email": BUILTIN_PATTERNS["email"]})
    original = [Message(role="user", content="mail a@b.com")]
    out = r.redact_messages(original)
    assert out[0].content == "mail [REDACTED:email]"
    assert original[0].content == "mail a@b.com"  # input untouched


def test_invalid_regex_names_the_pattern() -> None:
    with pytest.raises(ConfigError, match="broken"):
        Redactor({"broken": "["})


def test_from_file_unknown_builtin_and_empty(tmp_path: Path) -> None:
    p = tmp_path / "redaction.yaml"
    p.write_text("builtin: [nope]\n")
    with pytest.raises(ConfigError, match="unknown builtin pattern 'nope'"):
        Redactor.from_file(p)
    p.write_text("builtin: []\n")
    with pytest.raises(ConfigError, match="enables nothing"):
        Redactor.from_file(p)


def test_from_file_builtin_plus_custom(tmp_path: Path) -> None:
    p = tmp_path / "redaction.yaml"
    p.write_text('builtin: [card]\ncustom: { loan_ref: "LN-\\\\d{6}" }\n')
    r = Redactor.from_file(p)
    out = r.redact("card 4111111111111111 for LN-123456")
    assert "[REDACTED:card]" in out and "[REDACTED:loan_ref]" in out


async def test_provider_sees_masked_session_keeps_raw() -> None:
    fake = FakeAdapter(script=[LLMResponse(text="noted")])
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=fake,
        redactor=Redactor({"card": BUILTIN_PATTERNS["card"]}),
    )
    reply = await app.run(AGENT, "s1", "my card is 4111 1111 1111 1111")
    assert reply == "noted"
    sent = fake.calls[0]["messages"]
    assert any("[REDACTED:card]" in m.content for m in sent)
    assert all("4111" not in m.content for m in sent)
    history = await app.sessions.history("s1")
    assert any("4111 1111 1111 1111" in m.content for m in history)  # truth preserved


async def test_tool_results_are_redacted_on_second_call() -> None:
    reg = ToolRegistry()

    @reg.register
    def get_contact(customer_id: str) -> dict[str, str]:
        """Contact details."""
        return {"email": "cust@bank.example"}

    fake = FakeAdapter(
        script=[
            LLMResponse(
                tool_calls=[ToolCall(id="1", name="get_contact", args={"customer_id": "c1"})]
            ),
            LLMResponse(text="done"),
        ]
    )
    app = AgentApp(
        {AGENT: make_cfg(tools=["get_contact"])},
        registry=reg,
        adapter=fake,
        redactor=Redactor({"email": BUILTIN_PATTERNS["email"]}),
    )
    assert await app.run(AGENT, "s1", "what's my email on file?") == "done"
    second_call = fake.calls[1]["messages"]
    tool_msgs = [m for m in second_call if m.role == "tool"]
    assert tool_msgs and "[REDACTED:email]" in tool_msgs[0].content
    assert all("cust@bank.example" not in m.content for m in second_call)


async def test_system_instructions_are_redacted_too() -> None:
    fake = FakeAdapter(script=[LLMResponse(text="hi")])
    wrapped = RedactingAdapter(fake, Redactor({"email": BUILTIN_PATTERNS["email"]}))
    await wrapped.complete(model="m", system="escalate to ops@bank.example", messages=[], tools=[])
    assert fake.calls[0]["system"] == "escalate to [REDACTED:email]"


def test_from_config_wires_redaction(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "name: collections-agent\ninstructions: a.md\nmodel: { provider: test, name: m }\n"
    )
    (tmp_path / "a.md").write_text("Be polite.")
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies/redaction.yaml").write_text("builtin: [card, email]\n")
    app = AgentApp.from_config(tmp_path, redaction="policies/redaction.yaml")
    assert app._redactor is not None
    with pytest.raises(ConfigError, match="redaction file not found"):
        AgentApp.from_config(tmp_path, redaction="policies/missing.yaml")
