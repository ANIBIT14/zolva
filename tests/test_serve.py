"""Reference HTTP entrypoint: routing, HMAC verification, error mapping."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_orchestrator import make_cfg
from zolva.bridge import LLMResponse
from zolva.bridge.fake import FakeAdapter
from zolva.channels import ChannelHub, FakeChannel
from zolva.cli import main
from zolva.orchestrator import AgentApp
from zolva.serve import create_app
from zolva.signing import sign_payload
from zolva.tools import ToolRegistry

AGENT = "collections-agent"


def make_harness(
    *, inbound_secret: str | None = None, replies: int = 3
) -> tuple[TestClient, FakeChannel]:
    app = AgentApp(
        {AGENT: make_cfg(tools=[])},
        registry=ToolRegistry(),
        adapter=FakeAdapter(script=[LLMResponse(text="You owe 4200.")] * replies),
    )
    channel = FakeChannel()
    hub = ChannelHub(app, channels={"webchat": channel}, agents={AGENT: ["webchat"]})
    return TestClient(create_app(app, hub, inbound_secret=inbound_secret)), channel


def test_happy_path_dispatches_and_replies() -> None:
    client, channel = make_harness()
    r = client.post(f"/channels/webchat/{AGENT}", json={"session_id": "c1", "text": "dues?"})
    assert r.status_code == 200
    assert r.json() == {"reply": "You owe 4200."}
    assert channel.sent == [("c1", "You owe 4200.")]


def test_unknown_channel_and_disallowed_route_are_400() -> None:
    client, _ = make_harness()
    r = client.post(f"/channels/nope/{AGENT}", json={"session_id": "c1", "text": "hi"})
    assert r.status_code == 400 and "unknown channel" in r.json()["error"]
    r = client.post("/channels/webchat/other-agent", json={"session_id": "c1", "text": "hi"})
    assert r.status_code == 400 and "not allowed" in r.json()["error"]


def test_missing_session_id_is_400_and_never_echoes_payload() -> None:
    client, _ = make_harness()
    r = client.post(f"/channels/webchat/{AGENT}", json={"text": "pan 4111111111111111"})
    assert r.status_code == 400
    assert "session_id" in r.json()["error"]
    assert "4111" not in r.text  # caller errors must not echo customer content


def test_non_json_body_is_400() -> None:
    client, _ = make_harness()
    r = client.post(f"/channels/webchat/{AGENT}", content=b"not json")
    assert r.status_code == 400 and "JSON" in r.json()["error"]


def test_signature_required_when_secret_set() -> None:
    client, _ = make_harness(inbound_secret="s3cr3t")
    body = json.dumps({"session_id": "c1", "text": "dues?"}).encode()

    r = client.post(f"/channels/webchat/{AGENT}", content=body)
    assert r.status_code == 401  # unsigned

    ts, sig = sign_payload("wrong-secret", body)
    r = client.post(
        f"/channels/webchat/{AGENT}",
        content=body,
        headers={"X-Zolva-Signature": sig, "X-Zolva-Timestamp": ts},
    )
    assert r.status_code == 401  # wrong secret

    ts, sig = sign_payload("s3cr3t", body)
    r = client.post(
        f"/channels/webchat/{AGENT}",
        content=body,
        headers={"X-Zolva-Signature": sig, "X-Zolva-Timestamp": ts},
    )
    assert r.status_code == 200 and r.json()["reply"] == "You owe 4200."


def test_healthz_lists_agent_names_only() -> None:
    client, _ = make_harness()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "agents": [AGENT]}


def test_cli_serve_wires_args(monkeypatch: pytest.MonkeyPatch) -> None:
    import zolva.serve

    captured: dict[str, object] = {}

    def fake_serve(app_spec, channels_path, *, host, port, inbound_secret):  # type: ignore[no-untyped-def]
        captured.update(
            app_spec=app_spec, channels=channels_path, host=host, port=port, secret=inbound_secret
        )

    monkeypatch.setattr(zolva.serve, "serve", fake_serve)
    monkeypatch.setenv("ZOLVA_INBOUND_SECRET", "env-secret")
    rc = main(["serve", "--app", "bank:app", "--channels", "channels.yaml", "--port", "9100"])
    assert rc == 0
    assert captured == {
        "app_spec": "bank:app",
        "channels": "channels.yaml",
        "host": "127.0.0.1",
        "port": 9100,
        "secret": "env-secret",
    }
