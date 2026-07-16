"""Reference HTTP entrypoint: channel webhooks in, agent replies out.

`zolva serve --app app:app --channels channels.yaml` exposes every declared
channel route as `POST /channels/{channel}/{agent}`. Inbound requests are
HMAC-verified when ZOLVA_INBOUND_SECRET is set (same scheme as outbound
webhooks: timestamp inside the MAC, see zolva.signing).

Requires the optional extra: pip install "zolva[dashboard]".

This is a reference entrypoint, not a hardened edge: put your gateway or
reverse proxy (TLS, auth, rate limits) in front before exposing it beyond
localhost. Provider-specific webhook signatures (e.g. a vendor's own header
scheme) belong in that channel's adapter, not here.
"""

# no `from __future__ import annotations`: FastAPI must evaluate the Request
# annotation inside create_app's scope, stringified annotations break routing
import json
from typing import Any

from zolva.channels import ChannelError, ChannelHub
from zolva.orchestrator import AgentApp
from zolva.signing import SignatureError, verify_zolva_signature


def create_app(app: AgentApp, hub: ChannelHub, *, inbound_secret: str | None = None) -> Any:
    """Build the FastAPI app. Returns Any so core installs can import this
    module's helpers without the extra."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    api = FastAPI(title="Zolva Channels", openapi_url=None, docs_url=None, redoc_url=None)

    @api.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "agents": sorted(hub._agents)}

    @api.post("/channels/{channel}/{agent}")
    async def inbound(channel: str, agent: str, request: Request) -> JSONResponse:
        body = await request.body()
        if inbound_secret is not None:
            try:
                verify_zolva_signature(
                    body,
                    request.headers.get("X-Zolva-Signature", ""),
                    request.headers.get("X-Zolva-Timestamp", ""),
                    inbound_secret,
                )
            except SignatureError as e:
                return JSONResponse({"error": str(e)}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        try:
            reply = await hub.dispatch(channel, agent, payload)
        except ChannelError as e:
            # caller errors (unknown route, bad payload); never echo the payload back
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"reply": reply})

    return api


def serve(
    app_spec: str,
    channels_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8700,
    inbound_secret: str | None = None,
) -> None:
    import uvicorn

    from zolva.cli import _load_app

    app = _load_app(app_spec)
    hub = ChannelHub.from_config(channels_path, app)
    sig = "ON" if inbound_secret else "OFF (set ZOLVA_INBOUND_SECRET)"
    print(f"zolva serve: http://{host}:{port}  channels={channels_path}  signature-verify={sig}")
    uvicorn.run(
        create_app(app, hub, inbound_secret=inbound_secret),
        host=host,
        port=port,
        log_level="warning",
    )
