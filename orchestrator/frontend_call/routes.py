"""Opt-in media route on the existing authenticated Backend API / Remote hop."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from aiohttp import web
from .adapters import MediaAdapters
from .config import CallConfig
from .contract import CallError, MAX_BODY
from .ports import HashiPorts
from .service import CallService
from .diagnostics import body_facts, elapsed_ms, emit, error_facts


def register_call_api(api):
    service = CallService(
        CallConfig(Path(api.config_path).parent / "call_profiles.json"),
        HashiPorts(api),
        MediaAdapters(secret_resolver=getattr(api, "connector_secret_resolver", None)),
    )
    api.call_service = service
    api.live_voice_manager.external_call_busy = service.busy
    api.live_voice_manager.external_route_allowed = lambda owner, agent: service.config.route(owner, agent)["route"] == "phone"
    reaper = None
    headers = {
        "Cache-Control": "no-store, private",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    async def handle(request):
        body = None  # Never inspect unauthed content for logging.
        stage = "profile"
        started = time.monotonic()
        try:
            if api._is_governed_profile():
                raise CallError("call_profile_unsupported", 501)
            stage = "auth"
            if not api.admin_token or not api._check_admin_auth(request):
                raise CallError("call_auth_required", 403)
            stage = "owner"
            owner = api._v1_owner_id(request)
            if not owner:
                raise CallError("call_auth_required", 403)
            stage = "content_type"
            if request.content_type != "application/json":
                raise CallError("call_json_required", 415)
            stage = "body_read"
            data, total = [], 0
            async for chunk in request.content.iter_chunked(16384):
                total += len(chunk)
                if total > MAX_BODY:
                    raise CallError("call_body_limit", 413)
                data.append(chunk)
            stage = "json"
            try:
                body = json.loads(b"".join(data))
            except (ValueError, UnicodeError) as exc:
                raise CallError("call_json_invalid") from exc
            # Disabled file prevents new work; end is still available for cleanup.
            if not isinstance(body, dict):
                raise CallError("call_json_invalid")
            if body.get("operation") not in ("end", "route"):
                stage = "config_read"
                service.config.read()
            stage = "invoke"
            result = await service.invoke(owner, body)
            stage = "response"
            return web.json_response(result, headers=headers)
        except asyncio.CancelledError:
            if stage != "invoke":
                emit("http_operation_cancelled", **body_facts(body), generation=service.generation,
                     stage=stage, reason="task_cancelled", duration_ms=elapsed_ms(time.monotonic, started))
            raise
        except CallError as exc:
            if stage != "invoke":
                emit("http_operation_failed", **body_facts(body), generation=service.generation,
                     stage=stage, http_status=exc.status, duration_ms=elapsed_ms(time.monotonic, started),
                     **error_facts(exc, "call_internal_error"))
            return web.json_response(
                {"ok": False, "error_code": exc.code},
                status=exc.status,
                headers=headers,
            )
        except Exception as exc:
            if stage != "invoke":
                emit("http_operation_failed", **body_facts(body), generation=service.generation,
                     stage=stage, http_status=500, duration_ms=elapsed_ms(time.monotonic, started),
                     **error_facts(exc, "call_internal_error"))
            return web.json_response(
                {"ok": False, "error_code": "call_internal_error"},
                status=500,
                headers=headers,
            )

    async def start(app):
        nonlocal reaper

        async def reap():
            while True:
                await asyncio.sleep(10)
                service.expire()

        reaper = asyncio.create_task(reap())

    async def close(app):
        if reaper:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        await service.close()

    api.app.router.add_post("/api/v1/call/operation", handle)
    api.app.on_startup.append(start)
    api.app.on_cleanup.append(close)
    return service
