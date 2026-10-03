"""Opt-in media route on the existing authenticated Backend API / Remote hop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from aiohttp import web
from .adapters import MediaAdapters
from .config import CallConfig
from .contract import CallError, MAX_BODY
from .ports import HashiPorts
from .service import CallService


def register_call_api(api):
    service = CallService(
        CallConfig(Path(api.config_path).parent / "call_profiles.json"),
        HashiPorts(api),
        MediaAdapters(secret_resolver=getattr(api, "connector_secret_resolver", None)),
    )
    api.call_service = service
    api.live_voice_manager.external_call_busy = service.busy
    reaper = None
    headers = {
        "Cache-Control": "no-store, private",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    async def handle(request):
        try:
            if api._is_governed_profile():
                raise CallError("call_profile_unsupported", 501)
            if not api.admin_token or not api._check_admin_auth(request):
                raise CallError("call_auth_required", 403)
            owner = api._v1_owner_id(request)
            if not owner:
                raise CallError("call_auth_required", 403)
            if request.content_type != "application/json":
                raise CallError("call_json_required", 415)
            data, total = [], 0
            async for chunk in request.content.iter_chunked(16384):
                total += len(chunk)
                if total > MAX_BODY:
                    raise CallError("call_body_limit", 413)
                data.append(chunk)
            try:
                body = json.loads(b"".join(data))
            except (ValueError, UnicodeError) as exc:
                raise CallError("call_json_invalid") from exc
            # Disabled file prevents new work; end is still available for cleanup.
            if not isinstance(body, dict):
                raise CallError("call_json_invalid")
            if body.get("operation") != "end":
                service.config.read()
            return web.json_response(await service.invoke(owner, body), headers=headers)
        except CallError as exc:
            return web.json_response(
                {"ok": False, "error_code": exc.code},
                status=exc.status,
                headers=headers,
            )
        except Exception:
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
