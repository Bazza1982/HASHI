from __future__ import annotations
"""
Local OpenAI-compatible API gateway.

Exposes:
  POST /v1/chat/completions
  GET  /v1/models

Routes requests to local CLI backends (gemini-cli, claude-cli, codex-cli) and
xAI HTTP backend (xai-api).
Runs on its own port (default 18801), independent from Telegram and workbench.

Adapter instances are separate from Telegram runtimes — no shared queue.
Supports stateless mode (client manages history) and optional server-side
session cache (in-memory, TTL-based) for clients that don't resend full history.
"""

import asyncio
from datetime import datetime
import json
import logging
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from adapters.xai_imagine import (
    DEFAULT_IMAGINE_MODEL,
    DEFAULT_IMAGINE_VIDEO_MODEL,
    XaiOAuthCredentialError,
    generate_xai_image,
    generate_xai_video,
    is_imagine_image_model,
    is_imagine_video_model,
)
from adapters.registry import get_backend_class
from orchestrator.model_catalog import (
    AVAILABLE_CLAUDE_MODELS,
    AVAILABLE_CODEX_MODELS,
    AVAILABLE_GEMINI_MODELS,
    AVAILABLE_XAI_API_MODELS,
)
from orchestrator.api_gateway_config import load_api_gateway_config
from orchestrator.api_gateway_preflight import check_gateway_engines
from adapters.stream_events import StreamEvent, KIND_TEXT_DELTA

logger = logging.getLogger("BridgeU.APIGateway")

# ── Constants ────────────────────────────────────────────────────────────────

SESSION_TTL_SEC = 1800  # 30 minutes
MAX_EXTERNAL_TOOLS = 128
MAX_EXTERNAL_TOOL_BYTES = 1024 * 1024

_ENGINE_FOR_MODEL: dict[str, str] = {}
for _m in AVAILABLE_GEMINI_MODELS:
    _ENGINE_FOR_MODEL[_m] = "gemini-cli"
for _m in AVAILABLE_CLAUDE_MODELS:
    _ENGINE_FOR_MODEL[_m] = "claude-cli"
for _m in AVAILABLE_CODEX_MODELS:
    _ENGINE_FOR_MODEL[_m] = "codex-cli"
for _m in AVAILABLE_XAI_API_MODELS:
    _ENGINE_FOR_MODEL[_m] = "xai-api"

_ALL_MODELS = list(_ENGINE_FOR_MODEL.keys())
_GATEWAY_ENGINES = sorted(set(_ENGINE_FOR_MODEL.values()))
DEFAULT_API_MODEL = AVAILABLE_CODEX_MODELS[0] if AVAILABLE_CODEX_MODELS else (_ALL_MODELS[0] if _ALL_MODELS else "")


def _engine_owned_by(engine: str) -> str:
    return engine.replace("-cli", "").replace("-api", "")


def available_gateway_models() -> list[str]:
    return list(_ALL_MODELS)


def default_gateway_model() -> str:
    return DEFAULT_API_MODEL

# ── Terminal colours ──────────────────────────────────────────────────────────

_C_API_IN  = "\033[38;5;110m"   # muted steel blue - incoming request
_C_API_OUT = "\033[38;5;109m"   # blue-green       - outgoing response
_C_RESET   = "\033[0m"


def _print_api_in(model: str, preview: str):
    ts = datetime.now().strftime("%H:%M:%S")
    text = f"{_C_API_IN}[api {ts}] <- {model}  {preview}{_C_RESET}"
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, OSError):
        print(text.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace"), flush=True)


def _print_api_out(model: str, elapsed_s: float, chars: int, stream: bool):
    ts = datetime.now().strftime("%H:%M:%S")
    mode = "stream" if stream else "sync"
    text = f"{_C_API_OUT}[api {ts}] -> {model}  ({mode}, {elapsed_s:.2f}s, {chars} chars){_C_RESET}"
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, OSError):
        print(text.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace"), flush=True)


# ── Prompt assembly ───────────────────────────────────────────────────────────

def _messages_to_prompt(messages: list[dict]) -> str:
    """Flatten OpenAI messages[] into a single prompt string for CLI backends."""
    system_parts = []
    history_parts = []

    for msg in messages:
        role = (msg.get("role") or "").lower()
        content = msg.get("content") or ""
        if isinstance(content, list):
            # multi-part content — extract text parts only
            content = " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            history_parts.append(f"User: {content}")
        elif role == "assistant":
            history_parts.append(f"Assistant: {content}")

    parts = []
    if system_parts:
        parts.append("\n\n".join(system_parts))
    if history_parts:
        parts.append("\n".join(history_parts))
    return "\n\n".join(parts)


def _uses_external_tool_protocol(body: dict[str, Any], messages: list[dict]) -> bool:
    raw_tools = body.get("tools")
    if raw_tools not in (None, []):
        return True
    tool_choice = body.get("tool_choice")
    if tool_choice not in (None, "none"):
        return True
    return any(
        str(message.get("role") or "").lower() == "tool"
        or "tool_calls" in message
        for message in messages
    )


def _external_tool_error(
    message: str,
    *,
    code: str,
    param: str | None = None,
    status: int = 400,
) -> web.Response:
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "param": param,
                "code": code,
            }
        },
        status=status,
    )


def _validate_external_tool_request(
    body: dict[str, Any],
    messages: list[dict],
) -> tuple[list[dict] | None, web.Response | None]:
    if not all(isinstance(message, dict) for message in messages):
        return None, _external_tool_error(
            "every messages item must be an object",
            code="invalid_messages",
            param="messages",
        )

    for index, message in enumerate(messages):
        role = str(message.get("role") or "").strip().lower()
        if not role:
            return None, _external_tool_error(
                f"messages[{index}].role is required",
                code="invalid_messages",
                param=f"messages[{index}].role",
            )
        if role == "tool" and not str(message.get("tool_call_id") or "").strip():
            return None, _external_tool_error(
                f"messages[{index}].tool_call_id is required for tool messages",
                code="invalid_tool_message",
                param=f"messages[{index}].tool_call_id",
            )
        if "tool_calls" in message and not isinstance(message.get("tool_calls"), list):
            return None, _external_tool_error(
                f"messages[{index}].tool_calls must be an array",
                code="invalid_tool_calls",
                param=f"messages[{index}].tool_calls",
            )

    raw_tools = body.get("tools", [])
    if raw_tools is None:
        raw_tools = []
    if not isinstance(raw_tools, list):
        return None, _external_tool_error(
            "tools must be an array",
            code="invalid_tools",
            param="tools",
        )
    if len(raw_tools) > MAX_EXTERNAL_TOOLS:
        return None, _external_tool_error(
            f"tools exceeds the limit of {MAX_EXTERNAL_TOOLS}",
            code="too_many_tools",
            param="tools",
        )
    try:
        tool_bytes = len(json.dumps(raw_tools, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return None, _external_tool_error(
            "tools must contain JSON-compatible values",
            code="invalid_tools",
            param="tools",
        )
    if tool_bytes > MAX_EXTERNAL_TOOL_BYTES:
        return None, _external_tool_error(
            "tools payload exceeds the 1 MiB limit",
            code="tools_too_large",
            param="tools",
        )

    tool_names: set[str] = set()
    for index, tool in enumerate(raw_tools):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            return None, _external_tool_error(
                f"tools[{index}] must be an OpenAI function tool",
                code="invalid_tool_schema",
                param=f"tools[{index}]",
            )
        function = tool.get("function")
        if not isinstance(function, dict):
            return None, _external_tool_error(
                f"tools[{index}].function must be an object",
                code="invalid_tool_schema",
                param=f"tools[{index}].function",
            )
        name = str(function.get("name") or "").strip()
        if not name:
            return None, _external_tool_error(
                f"tools[{index}].function.name is required",
                code="invalid_tool_schema",
                param=f"tools[{index}].function.name",
            )
        if name in tool_names:
            return None, _external_tool_error(
                f"duplicate tool name '{name}'",
                code="duplicate_tool_name",
                param=f"tools[{index}].function.name",
            )
        tool_names.add(name)
        parameters = function.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            return None, _external_tool_error(
                f"tools[{index}].function.parameters must be an object",
                code="invalid_tool_schema",
                param=f"tools[{index}].function.parameters",
            )

    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        if isinstance(tool_choice, str):
            if tool_choice not in {"auto", "none", "required"}:
                return None, _external_tool_error(
                    "tool_choice must be auto, none, required, or a named function",
                    code="invalid_tool_choice",
                    param="tool_choice",
                )
        elif isinstance(tool_choice, dict):
            choice_function = tool_choice.get("function")
            choice_name = (
                str(choice_function.get("name") or "").strip()
                if isinstance(choice_function, dict)
                else ""
            )
            if tool_choice.get("type") != "function" or not choice_name:
                return None, _external_tool_error(
                    "named tool_choice must identify a function",
                    code="invalid_tool_choice",
                    param="tool_choice",
                )
            if choice_name not in tool_names:
                return None, _external_tool_error(
                    f"tool_choice refers to unknown tool '{choice_name}'",
                    code="invalid_tool_choice",
                    param="tool_choice",
                )
        else:
            return None, _external_tool_error(
                "tool_choice must be a string or object",
                code="invalid_tool_choice",
                param="tool_choice",
            )

    if (
        body.get("parallel_tool_calls") is not None
        and not isinstance(body.get("parallel_tool_calls"), bool)
    ):
        return None, _external_tool_error(
            "parallel_tool_calls must be a boolean",
            code="invalid_parallel_tool_calls",
            param="parallel_tool_calls",
        )
    if body.get("n") not in (None, 1):
        return None, _external_tool_error(
            "external tool passthrough currently supports n=1 only",
            code="unsupported_choice_count",
            param="n",
        )
    return list(raw_tools), None


# ── Session cache ─────────────────────────────────────────────────────────────

class _SessionCache:
    def __init__(self, ttl: int = SESSION_TTL_SEC):
        self._ttl = ttl
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str) -> list[dict] | None:
        entry = self._store.get(session_id)
        if entry is None:
            return None
        if time.time() - entry["ts"] > self._ttl:
            del self._store[session_id]
            return None
        return entry["messages"]

    def set(self, session_id: str, messages: list[dict]):
        self._store[session_id] = {"messages": list(messages), "ts": time.time()}

    def append(self, session_id: str, role: str, content: str):
        messages = self.get(session_id) or []
        messages.append({"role": role, "content": content})
        self.set(session_id, messages)

    def purge_expired(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            del self._store[k]


# ── Adapter pool ──────────────────────────────────────────────────────────────

class _AdapterPool:
    """Lazily initialised per-engine adapter instances, separate from Telegram runtimes."""

    def __init__(self, global_config, secrets: dict, workspace_root: Path):
        self._global_config = global_config
        self._secrets = secrets
        self._workspace_root = workspace_root
        self._adapters: dict[str, Any] = {}
        self._init_locks: dict[str, asyncio.Lock] = {}

    async def get(self, engine: str, model: str) -> Any:
        if engine not in self._init_locks:
            self._init_locks[engine] = asyncio.Lock()

        async with self._init_locks[engine]:
            if engine in self._adapters:
                return self._adapters[engine]

            adapter = await self._create(engine, model)
            self._adapters[engine] = adapter
            return adapter

    async def _create(self, engine: str, model: str) -> Any:
        from orchestrator.config import AgentConfig

        workspace = self._workspace_root / "api-gateway" / engine
        workspace.mkdir(parents=True, exist_ok=True)

        cfg = AgentConfig(
            name=f"api-{engine}",
            engine=engine,
            workspace_dir=workspace,
            system_md=None,
            model=model,
            is_active=True,
            access_scope="project",
            extra={"process_timeout": 300},
            project_root=self._global_config.project_root,
        )
        BackendClass = get_backend_class(engine)
        if engine == "xai-api":
            api_key = {
                "xai_api_key": self._secrets.get("xai_api_key")
                or self._secrets.get("XAI_API_KEY"),
                "xai_oauth_refresh_token": self._secrets.get("xai_oauth_refresh_token"),
            }
        else:
            api_key = self._secrets.get(f"{engine}_key")
        adapter = BackendClass(cfg, self._global_config, api_key)
        ok = await adapter.initialize()
        if not ok:
            raise RuntimeError(f"Failed to initialize API adapter for {engine}")
        logger.info(f"API adapter initialised: {engine} (model={model})")
        return adapter

    async def update_model(self, engine: str, model: str):
        """Update the model on an existing adapter."""
        adapter = self._adapters.get(engine)
        if adapter and hasattr(adapter, "config"):
            adapter.config.model = model

    async def shutdown(self):
        for engine, adapter in self._adapters.items():
            try:
                await adapter.shutdown()
                logger.info(f"API adapter shut down: {engine}")
            except Exception as e:
                logger.warning(f"Error shutting down API adapter {engine}: {e}")
        self._adapters.clear()


# ── Gateway server ────────────────────────────────────────────────────────────

class APIGatewayServer:
    def __init__(self, global_config, secrets: dict, workspace_root: Path, default_model: str | None = None):
        self.global_config = global_config
        self._secrets = secrets
        self.port: int = getattr(global_config, "api_gateway_port", 18801)
        self.bind_host: str | None = None
        gateway_config = load_api_gateway_config(global_config)
        self.enabled: bool = bool(gateway_config.get("enabled", False))
        selected_default = str(default_model or gateway_config.get("default_model") or "").strip()
        self.default_model = selected_default if selected_default in _ENGINE_FOR_MODEL else DEFAULT_API_MODEL
        self._pool = _AdapterPool(global_config, secrets, workspace_root)
        self._sessions = _SessionCache()
        self._engine_status: dict[str, dict] = {}
        self._runner = None
        self._site = None
        self.refresh_engine_status()

        self.app = web.Application(client_max_size=8 * 1024 * 1024)
        self.app.router.add_get("/v1/models", self.handle_models)
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        self.app.router.add_post("/v1/images/generations", self.handle_image_generations)
        self.app.router.add_post("/v1/videos/generations", self.handle_video_generations)
        self.app.router.add_get("/health", self.handle_health)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def refresh_engine_status(self) -> dict[str, dict]:
        self._engine_status = check_gateway_engines(
            self.global_config,
            self._secrets,
            _GATEWAY_ENGINES,
        )
        return self._engine_status

    def _available_models(self) -> list[str]:
        models: list[str] = []
        for model in _ALL_MODELS:
            engine = _ENGINE_FOR_MODEL[model]
            status = self._engine_status.get(engine) or {}
            if status.get("available", True):
                models.append(model)
        return models

    def _engine_available(self, engine: str) -> tuple[bool, str]:
        status = self._engine_status.get(engine) or {}
        return bool(status.get("available", True)), str(status.get("reason") or "")

    async def start(self):
        self.refresh_engine_status()
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self.bind_host = self._select_bind_host()
        self._site = web.TCPSite(self._runner, self.bind_host, self.port)
        await self._site.start()
        available = [e for e, s in self._engine_status.items() if s.get("available")]
        unavailable = [e for e, s in self._engine_status.items() if not s.get("available")]
        logger.info(
            "API Gateway preflight: available=%s unavailable=%s",
            available,
            unavailable,
        )

    async def stop(self):
        await self._pool.shutdown()
        if self._runner:
            await self._runner.cleanup()

    def set_default_model(self, model: str) -> None:
        normalized = str(model or "").strip()
        if normalized not in _ENGINE_FOR_MODEL:
            raise ValueError(f"unknown API gateway model: {model}")
        self.default_model = normalized

    def _select_bind_host(self) -> str:
        configured = str(getattr(self.global_config, "api_host", "") or "127.0.0.1").strip()
        if configured not in {"127.0.0.1", "localhost"}:
            return configured
        for candidate in ("10.255.255.254",):
            if self._host_can_bind(candidate):
                return candidate
        return "127.0.0.1"

    @staticmethod
    def _host_can_bind(host: str) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, 0))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    # ── Route: GET /v1/models ─────────────────────────────────────────────────

    async def handle_models(self, request: web.Request) -> web.Response:
        now = int(time.time())
        data = [
            {
                "id": model,
                "object": "model",
                "created": now,
                "owned_by": _engine_owned_by(_ENGINE_FOR_MODEL[model]),
            }
            for model in self._available_models()
        ]
        return web.json_response({"object": "list", "data": data})

    # ── Route: GET /health ────────────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        initialized = sorted(self._pool._adapters.keys())
        available_engines = [
            engine for engine, status in self._engine_status.items() if status.get("available")
        ]
        overall = "ok" if available_engines else "degraded"
        return web.json_response(
            {
                "status": overall,
                "enabled": self.enabled,
                "engines": initialized,
                "engine_status": self._engine_status,
                "available_engines": available_engines,
                "available_models": self._available_models(),
                "default_model": self.default_model,
                "default_model_available": self.default_model in self._available_models(),
                "bind_host": self.bind_host,
                "port": self.port,
            }
        )

    # ── Route: POST /v1/chat/completions ──────────────────────────────────────

    async def handle_chat_completions(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be an object"}, status=400)

        model = str(body.get("model") or "").strip() or self.default_model

        engine = _ENGINE_FOR_MODEL.get(model)
        if engine is None:
            return web.json_response(
                {"error": f"unknown model '{model}'. Use GET /v1/models to list available models."},
                status=400,
            )

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return web.json_response({"error": "messages is required"}, status=400)
        if not all(isinstance(message, dict) for message in messages):
            return web.json_response(
                {"error": "every messages item must be an object"},
                status=400,
            )

        stream: bool = bool(body.get("stream", False))
        external_tool_mode = _uses_external_tool_protocol(body, messages)

        external_tools: list[dict] = []
        if external_tool_mode:
            if engine != "xai-api":
                return _external_tool_error(
                    f"model '{model}' does not support external tool passthrough; "
                    "only xAI /chat/completions models are enabled",
                    code="external_tool_passthrough_unsupported",
                    param="model",
                )
            validated_tools, validation_error = _validate_external_tool_request(body, messages)
            if validation_error is not None:
                return validation_error
            external_tools = validated_tools or []

        engine_ok, engine_reason = self._engine_available(engine)
        if not engine_ok:
            return web.json_response(
                {"error": f"backend unavailable for '{model}': {engine_reason}"},
                status=503,
            )

        # Session cache support — client may pass session_id in extra_body or top-level
        extra_body = body.get("extra_body") or {}
        if not isinstance(extra_body, dict):
            return web.json_response({"error": "extra_body must be an object"}, status=400)
        session_id: str | None = (
            extra_body.get("session_id")
            or body.get("session_id")
        )

        if external_tool_mode and session_id:
            return _external_tool_error(
                "session_id is not supported for external tool passthrough; "
                "the caller must send the complete tool conversation",
                code="external_tools_session_unsupported",
                param="session_id",
            )

        if session_id:
            cached = self._sessions.get(session_id)
            if cached:
                # Prepend cached history, then append new messages
                # Avoid duplicating the last user turn
                combined = list(cached)
                for msg in messages:
                    if not any(
                        m.get("role") == msg.get("role") and m.get("content") == msg.get("content")
                        for m in combined[-4:]
                    ):
                        combined.append(msg)
                messages = combined

        prompt = ""
        if not external_tool_mode:
            prompt = _messages_to_prompt(messages)
            if not prompt.strip():
                return web.json_response({"error": "empty prompt after assembly"}, status=400)

        # Terminal: show incoming
        user_preview = next(
            (str(m.get("content") or "")[:120] for m in reversed(messages) if m.get("role") == "user"),
            prompt[:120] or "[external tool request]",
        )
        _print_api_in(model, user_preview)

        request_id = f"apireq-{uuid.uuid4().hex[:8]}"

        try:
            adapter = await self._pool.get(engine, model)
            # Update model on adapter in case it changed
            await self._pool.update_model(engine, model)
        except Exception as e:
            logger.error(f"Adapter init failed for {engine}: {e}")
            return web.json_response({"error": f"backend unavailable: {e}"}, status=503)

        t_start = time.time()

        if external_tool_mode:
            supports_passthrough = getattr(adapter, "supports_external_tool_passthrough", None)
            if not callable(supports_passthrough) or not supports_passthrough(model):
                return _external_tool_error(
                    f"model '{model}' does not use xAI /chat/completions",
                    code="external_tool_passthrough_unsupported",
                    param="model",
                )
            if stream:
                return await self._handle_external_tool_streaming(
                    adapter,
                    messages,
                    external_tools,
                    body,
                    request_id,
                    model,
                    t_start,
                    request,
                )
            return await self._handle_external_tool_sync(
                adapter,
                messages,
                external_tools,
                body,
                request_id,
                model,
                t_start,
            )

        if stream:
            return await self._handle_streaming(
                adapter, prompt, request_id, model, session_id, messages, t_start, request
            )
        else:
            return await self._handle_sync(
                adapter, prompt, request_id, model, session_id, messages, t_start
            )

    def _xai_base_url(self) -> str:
        return str(getattr(self.global_config, "xai_api_base_url", "") or "").strip()

    def _hermes_home(self) -> str | None:
        return str(getattr(self.global_config, "hermes_home", "") or "").strip() or None

    def _xai_static_key(self) -> str | None:
        return str(
            self._secrets.get("xai_api_key")
            or self._secrets.get("XAI_API_KEY")
            or ""
        ).strip() or None

    def _xai_refresh_token(self) -> str | None:
        return str(self._secrets.get("xai_oauth_refresh_token") or "").strip() or None

    def _validate_xai_media_model(self, model: str, *, kind: str) -> web.Response | None:
        engine = _ENGINE_FOR_MODEL.get(model)
        if engine != "xai-api":
            return web.json_response(
                {"error": f"unknown {kind} model '{model}'. Use GET /v1/models to list available models."},
                status=400,
            )
        engine_ok, engine_reason = self._engine_available(engine)
        if not engine_ok:
            return web.json_response(
                {"error": f"backend unavailable for '{model}': {engine_reason}"},
                status=503,
            )
        return None

    # ── Route: POST /v1/images/generations ───────────────────────────────────

    async def handle_image_generations(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        model = str(body.get("model") or DEFAULT_IMAGINE_MODEL).strip()
        if not is_imagine_image_model(model):
            return web.json_response({"error": f"model '{model}' is not an image model"}, status=400)
        model_error = self._validate_xai_media_model(model, kind="image")
        if model_error is not None:
            return model_error

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return web.json_response({"error": "prompt is required"}, status=400)

        try:
            result = await generate_xai_image(
                prompt=prompt,
                model=model,
                bearer_token=self._xai_static_key(),
                oauth_refresh_token=self._xai_refresh_token(),
                hermes_home=self._hermes_home(),
                base_url=self._xai_base_url(),
                aspect_ratio=body.get("aspect_ratio"),
                resolution=body.get("resolution"),
                n=int(body.get("n") or 1),
                response_format=str(body.get("response_format") or "url"),
            )
        except XaiOAuthCredentialError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("xAI image generation failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "created": int(time.time()),
                "model": result.model,
                "data": [{"url": url} for url in result.urls],
            }
        )

    # ── Route: POST /v1/videos/generations ───────────────────────────────────

    async def handle_video_generations(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        model = str(body.get("model") or DEFAULT_IMAGINE_VIDEO_MODEL).strip()
        if not is_imagine_video_model(model):
            return web.json_response({"error": f"model '{model}' is not a video model"}, status=400)
        model_error = self._validate_xai_media_model(model, kind="video")
        if model_error is not None:
            return model_error

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return web.json_response({"error": "prompt is required"}, status=400)

        try:
            result = await generate_xai_video(
                prompt=prompt,
                model=model,
                bearer_token=self._xai_static_key(),
                oauth_refresh_token=self._xai_refresh_token(),
                hermes_home=self._hermes_home(),
                base_url=self._xai_base_url(),
                image_url=body.get("image_url"),
            )
        except XaiOAuthCredentialError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("xAI video generation failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

        payload = dict(result.raw)
        payload.setdefault("id", result.request_id)
        payload.setdefault("request_id", result.request_id)
        payload.setdefault("model", result.model)
        payload.setdefault("object", "video.generation")
        return web.json_response(payload)

    # ── External xAI tool-call passthrough ────────────────────────────────────

    @staticmethod
    def _external_finish_reason(response) -> str:
        reason = str(getattr(response, "stop_reason", "") or "").strip()
        if getattr(response, "tool_calls", None) and reason in {"", "stop"}:
            return "tool_calls"
        return reason or "stop"

    @staticmethod
    def _external_usage(response) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    async def _handle_external_tool_sync(
        self,
        adapter,
        messages: list[dict],
        tools: list[dict],
        body: dict[str, Any],
        request_id: str,
        model: str,
        t_start: float,
    ) -> web.Response:
        try:
            response = await adapter.generate_external_tool_response(
                messages,
                tools,
                request_id,
                tool_choice=body.get("tool_choice"),
                parallel_tool_calls=body.get("parallel_tool_calls"),
                use_streaming=False,
                request_options=body,
                model=model,
            )
        except Exception as exc:
            logger.error("External tool backend error for %s: %s", request_id, exc)
            return _external_tool_error(
                str(exc),
                code="external_tool_backend_error",
                status=500,
            )

        if not response.is_success:
            error = response.error or "backend error"
            logger.error("External tool backend failure %s: %s", request_id, error)
            return _external_tool_error(
                error,
                code="external_tool_backend_error",
                status=502,
            )

        text = response.text or ""
        tool_calls = list(response.tool_calls or [])
        message: dict[str, Any] = {
            "role": "assistant",
            "content": text if text else (None if tool_calls else ""),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        elapsed = time.time() - t_start
        _print_api_out(model, elapsed, len(text), stream=False)
        return web.json_response(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(t_start),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": self._external_finish_reason(response),
                    }
                ],
                "usage": self._external_usage(response),
            }
        )

    async def _handle_external_tool_streaming(
        self,
        adapter,
        messages: list[dict],
        tools: list[dict],
        body: dict[str, Any],
        request_id: str,
        model: str,
        t_start: float,
        request: web.Request,
    ) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        try:
            await resp.prepare(request)
        except Exception as exc:
            logger.debug("External tool stream prepare failed for %s: %s", request_id, exc)
            return resp

        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        collected_text: list[str] = []

        async def write_chunk(delta: dict[str, Any], finish_reason: str | None = None, **extra):
            chunk: dict[str, Any] = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(t_start),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }
                ],
            }
            chunk.update(extra)
            try:
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            except Exception:
                pass

        await write_chunk({"role": "assistant"})

        async def on_event(event: StreamEvent):
            if event.kind == KIND_TEXT_DELTA and event.summary:
                collected_text.append(event.summary)
                await write_chunk({"content": event.summary})

        try:
            response = await adapter.generate_external_tool_response(
                messages,
                tools,
                request_id,
                tool_choice=body.get("tool_choice"),
                parallel_tool_calls=body.get("parallel_tool_calls"),
                use_streaming=True,
                request_options=body,
                on_stream_event=on_event,
                model=model,
            )
        except Exception as exc:
            logger.error("External tool streaming backend error for %s: %s", request_id, exc)
            response = None
            error = str(exc)
        else:
            error = response.error if not response.is_success else None

        if error:
            try:
                payload = {
                    "error": {
                        "message": error,
                        "type": "server_error",
                        "code": "external_tool_backend_error",
                    }
                }
                await resp.write(f"data: {json.dumps(payload)}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
            except Exception:
                pass
            return resp

        full_text = response.text or ""
        if full_text and not collected_text:
            collected_text.append(full_text)
            await write_chunk({"content": full_text})

        tool_call_deltas: list[dict[str, Any]] = []
        for index, tool_call in enumerate(response.tool_calls or []):
            function = tool_call.get("function") or {}
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            tool_call_deltas.append(
                {
                    "index": index,
                    "id": str(tool_call.get("id") or ""),
                    "type": str(tool_call.get("type") or "function"),
                    "function": {
                        "name": str(function.get("name") or ""),
                        "arguments": arguments,
                    },
                }
            )
        if tool_call_deltas:
            await write_chunk({"tool_calls": tool_call_deltas})

        elapsed = time.time() - t_start
        final_text = full_text or "".join(collected_text)
        _print_api_out(model, elapsed, len(final_text), stream=True)
        await write_chunk(
            {},
            self._external_finish_reason(response),
            usage=self._external_usage(response),
        )
        try:
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except Exception:
            pass
        return resp

    # ── Legacy text-only sync response ────────────────────────────────────────

    async def _handle_sync(
        self,
        adapter,
        prompt: str,
        request_id: str,
        model: str,
        session_id: str | None,
        messages: list[dict],
        t_start: float,
    ) -> web.Response:
        try:
            response = await adapter.generate_response(
                prompt, request_id, is_retry=False, silent=True
            )
        except Exception as e:
            logger.error(f"Backend error for {request_id}: {e}")
            return web.json_response({"error": str(e)}, status=500)

        elapsed = time.time() - t_start

        if not response.is_success:
            err = response.error or "backend error"
            logger.error(f"API gateway backend failure {request_id}: {err}")
            return web.json_response({"error": err}, status=500)

        text = response.text or ""
        _print_api_out(model, elapsed, len(text), stream=False)

        if session_id:
            new_messages = list(messages) + [{"role": "assistant", "content": text}]
            self._sessions.set(session_id, new_messages)

        payload = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(t_start),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(prompt.split()),
                "completion_tokens": len(text.split()),
                "total_tokens": len(prompt.split()) + len(text.split()),
            },
        }
        return web.json_response(payload)

    # ── Streaming response ────────────────────────────────────────────────────

    async def _handle_streaming(
        self,
        adapter,
        prompt: str,
        request_id: str,
        model: str,
        session_id: str | None,
        messages: list[dict],
        t_start: float,
        request: web.Request,
    ) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        try:
            await resp.prepare(request)
        except Exception as e:
            logger.debug(f"Stream prepare failed for {request_id} (client disconnected?): {e}")
            return resp

        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        collected_text: list[str] = []

        async def on_event(event: StreamEvent):
            if event.kind == KIND_TEXT_DELTA and event.summary:
                collected_text.append(event.summary)
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(t_start),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": event.summary},
                            "finish_reason": None,
                        }
                    ],
                }
                try:
                    await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
                except Exception:
                    pass

        try:
            response = await adapter.generate_response(
                prompt, request_id, is_retry=False, silent=True,
                on_stream_event=on_event,
            )
        except Exception as e:
            logger.error(f"Streaming backend error for {request_id}: {e}")
            try:
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
            except Exception:
                pass
            return resp

        elapsed = time.time() - t_start

        # If adapter didn't stream deltas, send full text as one chunk
        full_text = response.text or ""
        if full_text and not collected_text:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(t_start),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": full_text},
                        "finish_reason": None,
                    }
                ],
            }
            try:
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            except Exception:
                pass

        final_text = full_text or "".join(collected_text)
        _print_api_out(model, elapsed, len(final_text), stream=True)

        if session_id and final_text:
            new_messages = list(messages) + [{"role": "assistant", "content": final_text}]
            self._sessions.set(session_id, new_messages)

        # Final chunk with finish_reason
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(t_start),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        try:
            await resp.write(f"data: {json.dumps(final_chunk)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except Exception:
            pass
        return resp
