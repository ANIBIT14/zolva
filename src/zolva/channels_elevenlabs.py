"""ElevenLabs voice channel: replies synthesized per turn, delivered to your call gateway.

Endpoint facts are pinned to the provider's documentation, not guessed:

- TTS: ``POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=...``
  with the ``xi-api-key`` header and JSON body ``{"text", "model_id"}``; the response
  body is the audio bytes. (docs/api-reference/text-to-speech/convert)
- Post-call webhook signature: ``ElevenLabs-Signature: t={unix},v0={hmac}`` where the
  MAC is HMAC-SHA256 over ``"{timestamp}.{body}"`` with the webhook's shared secret.
  (docs/eleven-agents/workflows/post-call-webhooks)

Zolva never holds the phone call. Your telephony gateway (or ElevenLabs' agents
platform) does; this adapter synthesizes each reply and hands the audio to the
gateway URL you configure, HMAC-signed with Zolva's own scheme so the gateway can
authenticate it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

from zolva.channels import ChannelAdapter, ChannelError, InboundMessage, _parse_inbound
from zolva.signing import sign_payload

_DEFAULT_MODEL = "eleven_multilingual_v2"
_DEFAULT_FORMAT = "mp3_44100_128"


class ElevenLabsChannel(ChannelAdapter):
    def __init__(
        self,
        voice_id: str,
        api_key: str,
        delivery_url: str,
        delivery_secret: str,
        model_id: str = _DEFAULT_MODEL,
        output_format: str = _DEFAULT_FORMAT,
        api_base: str = "https://api.elevenlabs.io",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._voice_id = voice_id
        self._api_key = api_key
        self._delivery_url = delivery_url
        self._delivery_secret = delivery_secret
        self._model_id = model_id
        self._output_format = output_format
        self._api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0)

    async def receive(self, raw: dict[str, Any]) -> InboundMessage:
        """Inbound turns arrive as {session_id, text} from your gateway's STT."""
        return _parse_inbound(raw)

    async def send(self, session_id: str, text: str) -> None:
        try:
            r = await self._client.post(
                f"{self._api_base}/v1/text-to-speech/{self._voice_id}",
                params={"output_format": self._output_format},
                headers={"xi-api-key": self._api_key},
                json={"text": text, "model_id": self._model_id},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ChannelError(f"elevenlabs tts failed: {e}") from e
        audio = r.content
        ts, sig = sign_payload(self._delivery_secret, audio)
        try:
            r = await self._client.post(
                self._delivery_url,
                content=audio,
                headers={
                    "Content-Type": "audio/mpeg",
                    "X-Zolva-Session": session_id,
                    "X-Zolva-Signature": sig,
                    "X-Zolva-Timestamp": ts,
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ChannelError(f"channel delivery failed: {e}") from e


def verify_elevenlabs_signature(
    body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 1800,
    now: int | None = None,
) -> None:
    """Verify an ``ElevenLabs-Signature`` header (``t={unix},v0={hex hmac}``).

    Raises ChannelError on any failure. The MAC is HMAC-SHA256 over
    ``"{timestamp}.{body}"``; the timestamp is rejected outside the tolerance
    window (the provider's own examples use 30 minutes). Prefer the ElevenLabs
    SDK's ``webhooks.construct_event`` where the SDK is available; this exists
    so a webhook receiver needs no extra dependency.
    """
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts, v0 = parts["t"], parts["v0"]
        ts_int = int(ts)
    except (ValueError, KeyError) as e:
        raise ChannelError(f"malformed ElevenLabs-Signature header: {e!r}") from None
    current = now if now is not None else int(time.time())
    if abs(current - ts_int) > tolerance_seconds:
        raise ChannelError("ElevenLabs-Signature timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(v0, expected):
        raise ChannelError("ElevenLabs-Signature signature mismatch")
