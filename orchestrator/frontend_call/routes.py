"""Default-enabled media route on the authenticated Backend API / Remote hop."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from aiohttp import web
from orchestrator.voice_manager import VoiceManager
from .adapters import MediaAdapters
from .config import CallConfig
from .contract import CallError, MAX_BODY
from .ports import HashiPorts
from .service import CallService
from .tts import CatalogCallTTS
from .video_tts import CallVideoTTS, VideoSpeechClient
from .diagnostics import body_facts, diagnostic_context, elapsed_ms, emit, error_facts


def register_call_api(api):
    global_config = getattr(api, "global_config", None)
    bridge_home = Path(
        getattr(global_config, "bridge_home", None) or Path(api.config_path).parent
    )
    project_root = Path(
        getattr(global_config, "project_root", None)
        or Path(__file__).resolve().parents[2]
    )
    media_root = Path(
        getattr(global_config, "base_media_dir", None) or bridge_home / "media"
    )
    catalog_path = project_root / "voice_catalog.json"
    catalog_options = (
        {"voice_catalog_path": catalog_path, "project_root": project_root}
        if global_config is not None
        else {}
    )
    config = CallConfig(
        Path(api.config_path).parent / "call_profiles.json",
        **catalog_options,
    )
    config.initialize()
    tts_synthesizer = None
    if global_config is not None:
        tts_manager = VoiceManager(
            bridge_home / "state" / "call_tts",
            media_root,
            ffmpeg_cmd="ffmpeg",
            secrets=getattr(api, "secrets", None),
            catalog_path=catalog_path,
            project_root=project_root,
        )
        tts_synthesizer = CatalogCallTTS(
            tts_manager,
            temp_root=media_root / "call_tts",
            secrets=getattr(api, "secrets", None),
        )
    service = CallService(
        config,
        HashiPorts(api),
        MediaAdapters(secret_resolver=getattr(api, "connector_secret_resolver", None)),
        tts_synthesizer=tts_synthesizer,
        video_synthesizer=CallVideoTTS(
            bridge_home / "compute_services.json",
            client=VideoSpeechClient(
                secret_resolver=getattr(api, "connector_secret_resolver", None)
            ),
        ),
    )
    api.call_service = service
    api.live_voice_manager.external_call_busy = service.busy
    from orchestrator.frontend_optional_egress import PhoneCallFrontendEvents
    api.live_voice_manager.frontend_call_state = PhoneCallFrontendEvents(api)
    reaper = None
    headers = {
        "Cache-Control": "no-store, private",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    async def handle_scoped(request):
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
                emit(
                    "http_operation_cancelled",
                    **body_facts(body),
                    generation=service.generation,
                    stage=stage,
                    reason="task_cancelled",
                    duration_ms=elapsed_ms(time.monotonic, started),
                )
            raise
        except CallError as exc:
            if stage != "invoke":
                emit(
                    "http_operation_failed",
                    **body_facts(body),
                    generation=service.generation,
                    stage=stage,
                    http_status=exc.status,
                    duration_ms=elapsed_ms(time.monotonic, started),
                    **error_facts(exc, "call_internal_error"),
                )
            response = {"ok": False, "error_code": exc.code}
            if exc.code.startswith(("call_video_", "call_output_")):
                from orchestrator import ui_language
                response.update(
                    fallback_route="phone",
                    message=ui_language.tr("call.video_unavailable",
                                           locale=(body or {}).get("locale")),
                )
            return web.json_response(response, status=exc.status, headers=headers)
        except Exception as exc:
            if stage != "invoke":
                emit(
                    "http_operation_failed",
                    **body_facts(body),
                    generation=service.generation,
                    stage=stage,
                    http_status=500,
                    duration_ms=elapsed_ms(time.monotonic, started),
                    **error_facts(exc, "call_internal_error"),
                )
            return web.json_response(
                {"ok": False, "error_code": "call_internal_error"},
                status=500,
                headers=headers,
            )

    async def handle(request):
        # Propagated IDs join browser, Workbench, Remote and Call Function
        # records. The diagnostics allowlist rejects malformed values.
        correlation_id = request.headers.get("x-hashi-correlation-id")
        trace_id = request.headers.get("x-hashi-trace-id") or correlation_id
        span_id = request.headers.get("x-hashi-span-id")
        with diagnostic_context(
            correlation_id=correlation_id,
            trace_id=trace_id,
            span_id=span_id,
        ):
            return await handle_scoped(request)

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
