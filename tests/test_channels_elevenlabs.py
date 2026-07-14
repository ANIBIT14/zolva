"""ElevenLabs voice channel: endpoint facts pinned to the provider's documentation.

TTS: POST /v1/text-to-speech/{voice_id}?output_format=..., `xi-api-key` header,
JSON {"text", "model_id"}, binary audio response
(elevenlabs.io/docs/api-reference/text-to-speech/convert).
Webhook signature: `ElevenLabs-Signature: t={unix},v0={hmac}` over "{t}.{body}"
(elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks).
"""

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest

from zolva.channels import ChannelError, ChannelHub, InboundMessage
from zolva.channels_elevenlabs import ElevenLabsChannel, verify_elevenlabs_signature
from zolva.config import ConfigError


def _channel(handler: httpx.MockTransport | None = None, **kwargs: str) -> ElevenLabsChannel:
    return ElevenLabsChannel(
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        api_key="xi-key",
        delivery_url="https://gateway.bank.internal/voice/play",
        delivery_secret="s3cr3t",
        transport=handler,
        **kwargs,
    )


async def test_send_calls_documented_tts_endpoint_then_delivers_signed_audio() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.elevenlabs.io":
            return httpx.Response(200, content=b"AUDIO-BYTES")
        return httpx.Response(200)

    ch = _channel(httpx.MockTransport(handler))
    await ch.send("call-1", "Your dues are 4200 rupees.")

    tts, delivery = calls
    assert (
        str(tts.url) == "https://api.elevenlabs.io/v1/text-to-speech/JBFqnCBsd6RMkjVDRZzb"
        "?output_format=mp3_44100_128"
    )
    assert tts.headers["xi-api-key"] == "xi-key"
    assert json.loads(tts.content) == {
        "text": "Your dues are 4200 rupees.",
        "model_id": "eleven_multilingual_v2",
    }
    assert str(delivery.url) == "https://gateway.bank.internal/voice/play"
    assert delivery.content == b"AUDIO-BYTES"
    assert delivery.headers["content-type"] == "audio/mpeg"
    assert delivery.headers["x-zolva-session"] == "call-1"
    ts = delivery.headers["x-zolva-timestamp"]
    expected = hmac.new(b"s3cr3t", ts.encode() + b"." + b"AUDIO-BYTES", hashlib.sha256).hexdigest()
    assert delivery.headers["x-zolva-signature"] == expected


async def test_model_and_output_format_are_configurable() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"a")

    ch = _channel(
        httpx.MockTransport(handler), model_id="eleven_flash_v2", output_format="mp3_22050_32"
    )
    await ch.send("c1", "hi")
    assert seen[0].url.params["output_format"] == "mp3_22050_32"
    assert json.loads(seen[0].content)["model_id"] == "eleven_flash_v2"


async def test_tts_failure_raises_channel_error() -> None:
    ch = _channel(httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(ChannelError, match="tts failed"):
        await ch.send("c1", "hi")


async def test_delivery_failure_raises_channel_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.elevenlabs.io":
            return httpx.Response(200, content=b"a")
        return httpx.Response(503)

    ch = _channel(httpx.MockTransport(handler))
    with pytest.raises(ChannelError, match="delivery failed"):
        await ch.send("c1", "hi")


async def test_receive_parses_generic_transcript_payload() -> None:
    ch = _channel()
    msg = await ch.receive({"session_id": "call-9", "text": "what do I owe?", "lang": "en"})
    assert msg == InboundMessage(session_id="call-9", text="what do I owe?", meta={"lang": "en"})


# ---- ElevenLabs-Signature verification (t={unix},v0={hmac} over "{t}.{body}") ----


def _sign(body: bytes, secret: str, ts: int) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={digest}"


def test_verify_signature_accepts_valid_header() -> None:
    body = b'{"type": "post_call_transcription"}'
    ts = int(time.time())
    verify_elevenlabs_signature(body, _sign(body, "whsec", ts), "whsec")


def test_verify_signature_rejects_tampered_body() -> None:
    ts = int(time.time())
    header = _sign(b"original", "whsec", ts)
    with pytest.raises(ChannelError, match="signature mismatch"):
        verify_elevenlabs_signature(b"tampered", header, "whsec")


def test_verify_signature_rejects_stale_timestamp() -> None:
    body = b"x"
    ts = int(time.time()) - 3600
    with pytest.raises(ChannelError, match="timestamp"):
        verify_elevenlabs_signature(body, _sign(body, "whsec", ts), "whsec")


def test_verify_signature_rejects_malformed_header() -> None:
    with pytest.raises(ChannelError, match="malformed"):
        verify_elevenlabs_signature(b"x", "not-a-signature", "whsec")


# ---- config wiring: adapter: elevenlabs ----


def test_from_config_builds_elevenlabs_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EL_KEY", "xi-key")
    monkeypatch.setenv("VOICE_SECRET", "s3cr3t")
    p = tmp_path / "channels.yaml"
    p.write_text(
        "channels:\n"
        "  voice:\n"
        "    adapter: elevenlabs\n"
        "    voice_id: JBFqnCBsd6RMkjVDRZzb\n"
        "    api_key: ${ENV:EL_KEY}\n"
        "    delivery_url: https://gateway.bank.internal/voice/play\n"
        "    delivery_secret: ${ENV:VOICE_SECRET}\n"
        "agents:\n"
        "  collections-agent: [voice]\n"
    )
    import examples.mockbank.bank  # noqa: F401
    from zolva import AgentApp

    agents_dir = Path(__file__).parent.parent / "examples" / "mockbank" / "agents"
    hub = ChannelHub.from_config(p, AgentApp.from_config(agents_dir))
    assert isinstance(hub._channels["voice"], ElevenLabsChannel)  # noqa: SLF001


def test_from_config_rejects_inline_elevenlabs_key(tmp_path: Path) -> None:
    p = tmp_path / "channels.yaml"
    p.write_text(
        "channels:\n"
        "  voice: { adapter: elevenlabs, voice_id: v, api_key: raw-key,\n"
        "           delivery_url: https://x, delivery_secret: raw }\n"
        "agents: {}\n"
    )
    from zolva import AgentApp
    from zolva.config import load_agents  # noqa: F401

    agents_dir = Path(__file__).parent.parent / "examples" / "mockbank" / "agents"
    with pytest.raises(ConfigError, match="inline credential"):
        ChannelHub.from_config(p, AgentApp.from_config(agents_dir))
