from __future__ import annotations
"""
Local OpenAI-compatible API gateway.

Exposes:
  POST /v1/chat/completions
  GET  /v1/models

Routes requests to local CLI backends (gemini-cli, claude-cli, codex-cli).
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

from adapters.registry import get_backend_class
from orchestrator.model_catalog import (
    AVAILABLE_CLAUDE_MODELS,
    AVAILABLE_CODEX_MODELS,
    AVAILABLE_GEMINI_MODELS,
)
from adapters.stream_events import StreamEvent, KIND_TEXT_DELTA

logger = logging.getLogger("BridgeU.APIGateway")

# ── Constants ────────────────────────────────────────────────────────────────

SESSION_TTL_SEC = 1800  # 30 minutes

_ENGINE_FOR_MODEL: dict[str, str] = {}
for _m in AVAILABLE_GEMINI_MODELS:
    _ENGINE_FOR_MODEL[_m] = "gemini-cli"
for _m in AVAILABLE_CLAUDE_MODELS:
    _ENGINE_FOR_MODEL[_m] = "claude-cli"
for _m in AVAILABLE_CODEX_MODELS:
    _ENGINE_FOR_MODEL[_m] = "codex-cli"

_ALL_MODELS = list(_ENGINE_FOR_MODEL.keys())
DEFAULT_API_MODEL = AVAILABLE_CODEX_MODELS[0] if AVAILABLE_CODEX_MODELS else (_ALL_MODELS[0] if _ALL_MODELS else "")


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
        self.port: int = getattr(global_config, "api_gateway_port", 18801)
        self.bind_host: str | None = None
        selected_default = str(default_model or "").strip()
        self.default_model = selected_default if selected_default in _ENGINE_FOR_MODEL else DEFAULT_API_MODEL
        self._pool = _AdapterPool(global_config, secrets, workspace_root)
        self._sessions = _SessionCache()
        self._runner = None
        self._site = None

        self.app = web.Application(client_max_size=8 * 1024 * 1024)
        self.app.router.add_get("/v1/models", self.handle_models)
        self.app.router.add_post("/v1/chat/completions", self.handle_chat_completions)
        self.app.router.add_get("/health", self.handle_health)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self.bind_host = self._select_bind_host()
        self._site = web.TCPSite(self._runner, self.bind_host, self.port)
        await self._site.start()

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
                "owned_by": _ENGINE_FOR_MODEL[model].replace("-cli", ""),
            }
            for model in _ALL_MODELS
        ]
        return web.json_response({"object": "list", "data": data})

    # ── Route: GET /health ────────────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "engines": list(self._pool._adapters.keys()),
                "default_model": self.default_model,
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

        model = str(body.get("model") or "").strip() or self.default_model
        if not model:
            return web.json_response({"error": "model is required"}, status=400)

        engine = _ENGINE_FOR_MODEL.get(model)
        if engine is None:
            return web.json_response(
                {"error": f"unknown model '{model}'. Use GET /v1/models to list available models."},
                status=400,
            )

        messages: list[dict] = body.get("messages") or []
        if not messages:
            return web.json_response({"error": "messages is required"}, status=400)

        stream: bool = bool(body.get("stream", False))

        # Session cache support — client may pass session_id in extra_body or top-level
        session_id: str | None = (
            (body.get("extra_body") or {}).get("session_id")
            or body.get("session_id")
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

        prompt = _messages_to_prompt(messages)
        if not prompt.strip():
            return web.json_response({"error": "empty prompt after assembly"}, status=400)

        # Terminal: show incoming
        user_preview = next(
            (str(m.get("content") or "")[:120] for m in reversed(messages) if m.get("role") == "user"),
            prompt[:120],
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

        if stream:
            return await self._handle_streaming(
                adapter, prompt, request_id, model, session_id, messages, t_start, request
            )
        else:
            return await self._handle_sync(
                adapter, prompt, request_id, model, session_id, messages, t_start
            )

    # ── Sync response ─────────────────────────────────────────────────────────

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
