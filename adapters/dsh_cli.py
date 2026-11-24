"""DeepSeek Harness (DSH) backend adapter driven over ACP v1 JSON-RPC stdio."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import logging
import mimetypes
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from adapters.base import BackendCapabilities, BackendResponse, BaseBackend
from adapters.hashi_mcp import HashiMcpInvocation, prepare_hashi_mcp
from adapters.stream_events import (
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_ERROR,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PROVIDER_ACTIVITY,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from orchestrator.pathing import resolve_dsh_invocation
from orchestrator.process_execution import process_group_kwargs

_ACP_PROTOCOL_VERSION = 1
_ACP_STDOUT_LIMIT = 16 * 1024 * 1024  # 16 MiB per JSON-RPC line
_ACP_RPC_TIMEOUT_SEC = 30.0
_ACP_CANCEL_GRACE_SEC = 5.0
_SUPPORTED_IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
_SUPPORTED_REASONING_EFFORTS = frozenset({"off", "low", "high", "max"})


class _AcpRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


def _normalize_dsh_effort(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"zero", "none", "disabled"}:
        return "off"
    if value in _SUPPORTED_REASONING_EFFORTS:
        return value
    return "high"


def _classify_tool_event(tool_name: str, raw_input: Any) -> tuple[str, str, str]:
    """Map a DSH tool call into a HASHI StreamEvent kind, file_path, and summary."""

    name = str(tool_name or "tool").strip() or "tool"
    lower = name.lower()
    file_path = ""
    command = ""
    if isinstance(raw_input, Mapping):
        for key in ("path", "file_path", "filePath", "target_path", "filename"):
            val = raw_input.get(key)
            if isinstance(val, str) and val.strip():
                file_path = val.strip()
                break
        for key in ("command", "cmd", "script"):
            val = raw_input.get(key)
            if isinstance(val, str) and val.strip():
                command = val.strip()
                break

    if any(tok in lower for tok in ("bash", "shell", "exec", "terminal", "command", "run")):
        preview = command[:160] + ("…" if len(command) > 160 else "") if command else name
        return KIND_SHELL_EXEC, file_path, f"{name}: {preview}" if command else name
    if any(tok in lower for tok in ("edit", "write", "patch", "replace", "create", "delete", "mv", "rm")):
        return KIND_FILE_EDIT, file_path, f"{name} {file_path}".strip()
    if any(tok in lower for tok in ("read", "view", "glob", "grep", "search", "find", "list", "ls")):
        return KIND_FILE_READ, file_path, f"{name} {file_path}".strip()
    return KIND_TOOL_START, file_path, f"{name} {file_path}".strip() if file_path else name


def _format_tool_result_preview(content: Any, limit: int = 400) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        inner = item.get("content") if item.get("type") == "content" else item
        if isinstance(inner, Mapping) and inner.get("type") == "text":
            text = str(inner.get("text") or "").strip()
            if text:
                parts.append(text)
    joined = "\n".join(parts).strip()
    if len(joined) > limit:
        return joined[: limit - 1] + "…"
    return joined


def _select_permission_option(options: Any) -> str:
    """Pick an allow option for full-permission autonomous execution."""

    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, Mapping):
                continue
            kind = str(opt.get("kind") or "").strip().lower()
            opt_id = str(opt.get("optionId") or "").strip()
            if kind in {"allow_always", "allow_once"} and opt_id:
                return opt_id
        for opt in options:
            if not isinstance(opt, Mapping):
                continue
            opt_id = str(opt.get("optionId") or "").strip()
            if "allow" in opt_id.lower():
                return opt_id
    return "allow-once"


def _match_model_option(
    config_options: list[dict[str, Any]],
    desired_model: str | None,
) -> tuple[str | None, str | None]:
    """Match a HASHI model name against ACP configOptions.

    Returns ``(matched_opaque_value, current_opaque_value)``.
    """

    model_opt = next(
        (
            item
            for item in config_options
            if isinstance(item, Mapping) and item.get("id") == "model"
        ),
        None,
    )
    if not isinstance(model_opt, Mapping):
        return None, None

    current_val = str(model_opt.get("currentValue") or "").strip() or None
    target = str(desired_model or "").strip()
    if not target:
        return current_val, current_val

    flat_options: list[tuple[str, str, str]] = []  # (value, group, name)
    for entry in model_opt.get("options") or []:
        if not isinstance(entry, Mapping):
            continue
        if isinstance(entry.get("options"), list):
            group = str(entry.get("group") or "").strip()
            for sub in entry["options"]:
                if isinstance(sub, Mapping) and sub.get("value") is not None:
                    flat_options.append(
                        (
                            str(sub["value"]),
                            group,
                            str(sub.get("name") or "").strip(),
                        )
                    )
        elif entry.get("value") is not None:
            flat_options.append(
                (str(entry["value"]), "", str(entry.get("name") or "").strip())
            )

    target_lower = target.lower()
    # 1. Exact opaque value match (e.g. '["deepseek-official","deepseek-v4-pro"]')
    for val, _group, _name in flat_options:
        if val == target:
            return val, current_val

    # 2. Match decoded [provider, model] tuple or "provider/model"
    for val, group, _name in flat_options:
        provider = group
        model_id = val
        try:
            decoded = json.loads(val)
            if isinstance(decoded, list) and len(decoded) == 2:
                provider = str(decoded[0] or "").strip()
                model_id = str(decoded[1] or "").strip()
        except Exception:
            pass
        if model_id.lower() == target_lower:
            return val, current_val
        if provider and f"{provider}/{model_id}".lower() == target_lower:
            return val, current_val

    # 3. Display name fallback
    for val, _group, name in flat_options:
        if name and name.lower() == target_lower:
            return val, current_val

    return None, current_val


def _match_effort_option(
    config_options: list[dict[str, Any]],
    desired_effort: str | None,
) -> tuple[str | None, str | None]:
    effort_opt = next(
        (
            item
            for item in config_options
            if isinstance(item, Mapping) and item.get("id") == "reasoning_effort"
        ),
        None,
    )
    if not isinstance(effort_opt, Mapping):
        return None, None
    current_val = str(effort_opt.get("currentValue") or "").strip()
    normalized = _normalize_dsh_effort(desired_effort)
    valid_values = {
        str(opt.get("value") or "").strip()
        for opt in (effort_opt.get("options") or [])
        if isinstance(opt, Mapping)
    }
    if normalized in valid_values:
        return normalized, current_val
    return None, current_val


class DshCLIAdapter(BaseBackend):
    """HASHI adapter for DeepSeek Harness (DSH) via persistent ACP stdio."""

    DEFAULT_IDLE_TIMEOUT_SEC = 60 * 60
    SUPPORTED_REASONING_EFFORTS = _SUPPORTED_REASONING_EFFORTS
    DEFAULT_REASONING_EFFORT = "high"

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_commentary_stream=True,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.DSH.{self.config.name}")
        self.cmd_base = getattr(self.global_config, "dsh_cmd", "dsh")
        self._profile = self._extra_str("dsh_profile", "acp") or "acp"
        raw_patches = (self.config.extra or {}).get("dsh_patches") or ()
        self._patches: tuple[str, ...] = (
            tuple(str(p).strip() for p in raw_patches if str(p).strip())
            if isinstance(raw_patches, (list, tuple))
            else ()
        )
        requested_effort = self._extra_str("effort", self.DEFAULT_REASONING_EFFORT)
        self.effort = _normalize_dsh_effort(requested_effort)
        self._session_mode: bool = bool(
            (self.config.extra or {}).get("session_mode", False)
        )
        self._session_id: str | None = None
        self._session_in_current_worker: bool = False
        self._config_options: list[dict[str, Any]] = []
        self._image_prompts_supported: bool = False
        self._hashi_mcp_enabled: bool = False
        self._session_mcp_invocation: HashiMcpInvocation | None = None

        # Persistent ACP stdio worker state
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=60)
        self._next_rpc_id: int = 0
        self._pending_rpcs: dict[int, asyncio.Future[Any]] = {}
        self._initialized: bool = False

        # Active turn state for streaming and graceful /stop cancellation
        self._active_turn_session_id: str | None = None
        self._active_prompt_rpc_id: int | None = None
        self._update_handler: Callable[[dict[str, Any]], None] | None = None

    def _extra_str(self, key: str, default: str = "") -> str:
        extra = getattr(self.config, "extra", {}) or {}
        value = extra.get(key, default)
        return str(value or "").strip()

    def _resolve_launch(self) -> tuple[list[str], Path | None, str]:
        return resolve_dsh_invocation(
            self.cmd_base,
            profile=self._profile,
            patches=self._patches,
        )

    async def initialize(self) -> bool:
        self.logger.info("Initializing DSH (DeepSeek Harness ACP) backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        argv, launch_cwd, label = self._resolve_launch()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                "--help",
                cwd=str(launch_cwd) if launch_cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_group_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                self.logger.error("DSH ACP help check failed (%s): %s", label, err)
                return False
            self.logger.info(
                "DSH ACP backend ready (%s): %s",
                label,
                self._preview_text(stdout, 120),
            )
            return True
        except Exception as exc:
            self.logger.error("DSH ACP backend not accessible (%s): %s", label, exc)
            return False

    def set_session_mode(self, enabled: bool) -> None:
        self._session_mode = bool(enabled)
        if not self._session_mode:
            self._session_id = None
            self._session_in_current_worker = False
        self.logger.info("DSH session mode set to %s", "ON" if enabled else "OFF")

    async def handle_new_session(self) -> bool:
        old_sid = self._session_id
        self._session_id = None
        self._session_in_current_worker = False
        if old_sid and self._is_worker_alive():
            try:
                await self._rpc_call(
                    "session/close",
                    {"sessionId": old_sid},
                    timeout=5.0,
                )
            except Exception as exc:
                self.logger.debug("DSH session/close on reset failed: %s", exc)
        self._close_session_mcp()
        self.logger.info("DSH session reset (previous session_id=%s).", old_sid)
        return True

    def interrupt_nowait(self, reason: str = "USER_STOP") -> bool:
        """Gracefully cancel an active DSH ACP turn without killing the warm worker."""

        session_id = self._active_turn_session_id
        prompt_rpc_id = self._active_prompt_rpc_id
        loop = self._loop
        if (
            session_id
            and loop is not None
            and not loop.is_closed()
            and self._is_worker_alive()
        ):
            try:
                loop.call_soon_threadsafe(
                    self._schedule_graceful_cancel,
                    session_id,
                    prompt_rpc_id,
                    reason,
                )
                return True
            except Exception as exc:
                self.logger.warning(
                    "Failed to schedule graceful DSH cancel (reason=%r): %s",
                    reason,
                    exc,
                )
        return False

    def _schedule_graceful_cancel(
        self,
        session_id: str,
        prompt_rpc_id: int | None,
        reason: str,
    ) -> None:
        asyncio.create_task(
            self._send_cancel_notifications(session_id, prompt_rpc_id, reason)
        )

    async def _send_cancel_notifications(
        self,
        session_id: str,
        prompt_rpc_id: int | None,
        reason: str,
    ) -> None:
        self.logger.info(
            "Sending ACP session/cancel for session=%s reason=%r",
            session_id,
            reason,
        )
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
            if prompt_rpc_id is not None:
                await self._send_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/cancel_request",
                        "params": {"requestId": prompt_rpc_id},
                    }
                )
        except Exception as exc:
            self.logger.debug("Failed to send ACP cancel frame: %s", exc)

    def _is_worker_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._proc.stdin is not None
            and not self._proc.stdin.is_closing()
        )

    def _close_session_mcp(self) -> None:
        inv = self._session_mcp_invocation
        self._session_mcp_invocation = None
        self._hashi_mcp_enabled = False
        if inv is not None:
            try:
                inv.close()
            except Exception as exc:
                self.logger.debug("Error closing DSH HASHI MCP invocation: %s", exc)

    async def _stop_worker(self, reason: str = "shutdown") -> None:
        proc = self._proc
        self._proc = None
        self._initialized = False
        self._session_in_current_worker = False
        self._active_turn_session_id = None
        self._active_prompt_rpc_id = None
        self._update_handler = None

        # Reject any pending RPC futures immediately
        pending = list(self._pending_rpcs.values())
        self._pending_rpcs.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError(f"DSH ACP worker stopped ({reason})"))

        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.stdin.wait_closed(), timeout=2.0)
                    except Exception:
                        pass
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                await self.force_kill_process_tree(proc, self.logger, reason=f"dsh {reason}")

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    async def shutdown(self):
        if self._session_id and self._is_worker_alive():
            try:
                await self._rpc_call(
                    "session/close",
                    {"sessionId": self._session_id},
                    timeout=3.0,
                )
            except Exception:
                pass
        await self._stop_worker(reason="shutdown")
        self._close_session_mcp()

    async def _ensure_worker(self) -> None:
        if self._is_worker_alive() and self._initialized:
            return
        await self._stop_worker(reason="restart")

        argv, launch_cwd, label = self._resolve_launch()
        self.logger.info("Starting persistent DSH ACP worker (%s)...", label)
        self._stderr_tail.clear()
        self._loop = asyncio.get_running_loop()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(launch_cwd) if launch_cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_ACP_STDOUT_LIMIT,
            **process_group_kwargs(),
        )
        self._proc = proc
        self._reader_task = asyncio.create_task(self._reader_loop(proc))
        self._stderr_task = asyncio.create_task(self._stderr_loop(proc))

        init_result = await self._rpc_call(
            "initialize",
            {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                "clientInfo": {"name": "hashi-dsh-adapter", "version": "1.0.0"},
            },
            timeout=_ACP_RPC_TIMEOUT_SEC,
        )
        caps = (
            init_result.get("agentCapabilities", {})
            if isinstance(init_result, Mapping)
            else {}
        )
        prompt_caps = (
            caps.get("promptCapabilities", {}) if isinstance(caps, Mapping) else {}
        )
        self._image_prompts_supported = bool(prompt_caps.get("image", False))
        self._initialized = True
        self.logger.info(
            "DSH ACP worker initialized (pid=%s, image_prompts=%s)",
            proc.pid,
            self._image_prompts_supported,
        )

    async def _send_frame(self, payload: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError("DSH ACP worker stdin is closed")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            proc.stdin.write(raw)
            await proc.stdin.drain()

    async def _rpc_call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = _ACP_RPC_TIMEOUT_SEC,
        on_rpc_id: Callable[[int], None] | None = None,
    ) -> Any:
        self._next_rpc_id += 1
        rpc_id = self._next_rpc_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending_rpcs[rpc_id] = fut
        if on_rpc_id is not None:
            on_rpc_id(rpc_id)
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_rpcs.pop(rpc_id, None)

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    self.logger.debug("Ignoring non-JSON line from DSH stdout: %s", text[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch_incoming(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("DSH ACP reader loop error: %s", exc)
        finally:
            stderr_preview = " | ".join(list(self._stderr_tail)[-5:])
            err_msg = "DSH ACP worker exited unexpectedly"
            if stderr_preview:
                err_msg += f" (stderr: {stderr_preview})"
            for fut in list(self._pending_rpcs.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(err_msg))
            self._pending_rpcs.clear()
            self._initialized = False
            self._session_in_current_worker = False

    async def _stderr_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                raw_line = await proc.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    self.logger.debug("[dsh stderr] %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        has_method = isinstance(msg.get("method"), str)

        # 1. Response to our outgoing request
        if has_id and not has_method:
            rpc_id = msg["id"]
            fut = self._pending_rpcs.get(rpc_id)
            if fut is not None and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"] if isinstance(msg["error"], Mapping) else {}
                    fut.set_exception(
                        _AcpRpcError(
                            int(err.get("code", -32000)),
                            str(err.get("message", "unknown ACP error")),
                            err.get("data"),
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # 2. Server-to-client request (e.g. session/request_permission)
        if has_id and has_method:
            method = msg["method"]
            req_id = msg["id"]
            params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
            if method == "session/request_permission":
                option_id = _select_permission_option(params.get("options"))
                self.logger.debug(
                    "Auto-approving DSH permission request (session=%s, option=%s)",
                    params.get("sessionId"),
                    option_id,
                )
                await self._send_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": option_id,
                            }
                        },
                    }
                )
                return
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
            return

        # 3. Server notification (e.g. session/update)
        if has_method and msg["method"] == "session/update":
            params = msg.get("params")
            if isinstance(params, dict):
                update = params.get("update")
                if (
                    isinstance(update, Mapping)
                    and update.get("sessionUpdate") == "config_option_update"
                    and isinstance(update.get("configOptions"), list)
                ):
                    self._config_options = list(update["configOptions"])
                handler = self._update_handler
                if handler is not None:
                    handler(params)

    def _build_session_mcp_servers(self) -> list[dict[str, Any]]:
        self._close_session_mcp()
        if getattr(self, "tool_registry", None) is None:
            return []
        try:
            descriptor = prepare_hashi_mcp(self, backend="dsh-cli")
        except Exception as exc:
            self.logger.warning("Skipping HASHI MCP for DSH session: %s", exc)
            return []
        if not descriptor:
            return []
        if isinstance(descriptor, HashiMcpInvocation):
            self._session_mcp_invocation = descriptor
        command_path = str(Path(descriptor["command"]).resolve())
        project_root = str(Path(descriptor["cwd"]).resolve())
        existing_pythonpath = os.environ.get("PYTHONPATH", "").strip()
        pythonpath = (
            f"{project_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root
        )
        return [
            {
                "name": str(descriptor.get("name") or "hashi_tools"),
                "command": command_path,
                "args": [str(a) for a in (descriptor.get("args") or [])],
                "env": [{"name": "PYTHONPATH", "value": pythonpath}],
            }
        ]

    async def _ensure_session(self) -> str:
        workdir = str(self.effective_workdir.resolve())

        if not self._session_mode and self._session_id and self._session_in_current_worker:
            old_sid = self._session_id
            self._session_id = None
            self._session_in_current_worker = False
            try:
                await self._rpc_call("session/close", {"sessionId": old_sid}, timeout=5.0)
            except Exception:
                pass
            self._close_session_mcp()

        if self._session_mode and self._session_id and self._session_in_current_worker:
            return self._session_id

        mcp_servers = self._build_session_mcp_servers()

        # If we have a persisted session_id from before a worker restart, try session/resume first
        if self._session_mode and self._session_id and not self._session_in_current_worker:
            try:
                resumed = await self._rpc_call(
                    "session/resume",
                    {
                        "sessionId": self._session_id,
                        "cwd": workdir,
                        "mcpServers": mcp_servers,
                    },
                    timeout=_ACP_RPC_TIMEOUT_SEC,
                )
                if isinstance(resumed, Mapping) and isinstance(resumed.get("configOptions"), list):
                    self._config_options = list(resumed["configOptions"])
                self._session_in_current_worker = True
                self.logger.info("Resumed DSH ACP session %s", self._session_id)
                return self._session_id
            except Exception as exc:
                self.logger.warning(
                    "DSH session/resume failed for %s (%s); starting fresh session.",
                    self._session_id,
                    exc,
                )
                self._session_id = None
                self._session_in_current_worker = False

        # Start a new ACP session (fallback to mcpServers=[] if MCP startup fails)
        try:
            created = await self._rpc_call(
                "session/new",
                {"cwd": workdir, "mcpServers": mcp_servers},
                timeout=_ACP_RPC_TIMEOUT_SEC,
            )
        except Exception as exc:
            if not mcp_servers:
                raise
            self.logger.warning(
                "DSH session/new with HASHI MCP failed (%s); retrying without HASHI MCP.",
                exc,
            )
            self._close_session_mcp()
            created = await self._rpc_call(
                "session/new",
                {"cwd": workdir, "mcpServers": []},
                timeout=_ACP_RPC_TIMEOUT_SEC,
            )

        sid = str((created or {}).get("sessionId") or "").strip()
        if not sid:
            raise RuntimeError(f"DSH session/new returned no sessionId: {created!r}")
        if isinstance(created, Mapping) and isinstance(created.get("configOptions"), list):
            self._config_options = list(created["configOptions"])
        self._session_id = sid
        self._session_in_current_worker = True
        self.logger.info("Created DSH ACP session %s (cwd=%s)", sid, workdir)
        return sid

    async def _sync_session_config(
        self,
        session_id: str,
        *,
        desired_effort: str | None = None,
    ) -> None:
        # 1. Model selection
        desired_model = str(getattr(self.config, "model", "") or "").strip()
        matched_model, current_model = _match_model_option(
            self._config_options, desired_model
        )
        if matched_model and matched_model != current_model:
            resp = await self._rpc_call(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "model",
                    "value": matched_model,
                },
                timeout=_ACP_RPC_TIMEOUT_SEC,
            )
            if isinstance(resp, Mapping) and isinstance(resp.get("configOptions"), list):
                self._config_options = list(resp["configOptions"])
            self.logger.info("DSH model switched to %s", matched_model)

        # 2. Reasoning effort selection
        effective_effort = _normalize_dsh_effort(
            desired_effort if desired_effort is not None else self.effort
        )
        matched_effort, current_effort = _match_effort_option(
            self._config_options, effective_effort
        )
        if matched_effort and matched_effort != current_effort:
            resp = await self._rpc_call(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "reasoning_effort",
                    "value": matched_effort,
                },
                timeout=_ACP_RPC_TIMEOUT_SEC,
            )
            if isinstance(resp, Mapping) and isinstance(resp.get("configOptions"), list):
                self._config_options = list(resp["configOptions"])
            self.logger.info("DSH reasoning_effort switched to %s", matched_effort)

    def _build_prompt_blocks(
        self,
        prompt: str,
        request_content: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if prompt:
            blocks.append({"type": "text", "text": prompt})

        # Attach inline images only when DSH advertised image prompt support
        if self._image_prompts_supported and isinstance(request_content, Mapping):
            raw_blocks = request_content.get("content_blocks") or request_content.get("attachments") or []
            if isinstance(raw_blocks, list):
                for item in raw_blocks:
                    if not isinstance(item, Mapping):
                        continue
                    path_str = str(
                        item.get("path") or item.get("file_path") or ""
                    ).strip()
                    if not path_str:
                        continue
                    img_path = Path(path_str)
                    if not img_path.is_file():
                        continue
                    mime = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
                    if not mime:
                        mime = (mimetypes.guess_type(str(img_path))[0] or "").lower()
                    if mime not in _SUPPORTED_IMAGE_MIMES:
                        continue
                    try:
                        raw_bytes = img_path.read_bytes()
                        blocks.append(
                            {
                                "type": "image",
                                "mimeType": mime,
                                "data": base64.b64encode(raw_bytes).decode("ascii"),
                            }
                        )
                    except Exception as exc:
                        self.logger.debug("Skipping unreadable image %s: %s", img_path, exc)

        if not blocks:
            blocks.append({"type": "text", "text": " "})
        return blocks

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> BackendResponse:
        async with self._request_lock:
            return await self._generate_response_locked(
                prompt,
                request_id,
                is_retry=is_retry,
                silent=silent,
                on_stream_event=on_stream_event,
                request_content=request_content,
                reasoning_effort=reasoning_effort,
            )

    async def _generate_response_locked(
        self,
        prompt: str,
        request_id: str,
        *,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> BackendResponse:
        started = time.monotonic()
        self._touch_activity()
        try:
            await self._ensure_worker()
            session_id = await self._ensure_session()
            await self._sync_session_config(
                session_id,
                desired_effort=reasoning_effort,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            self.logger.error("DSH setup failed for %s: %s", request_id, exc)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=f"DSH backend setup failed: {exc}",
            )

        # Turn-level streaming state
        held_message_id: str | None = None
        held_text_parts: list[str] = []
        tool_titles: dict[str, str] = {}
        tool_call_count = 0
        latest_usage: dict[str, int] | None = None

        def _flush_held_as_commentary() -> None:
            nonlocal held_message_id, held_text_parts
            if not held_text_parts:
                return
            commentary = "".join(held_text_parts).strip()
            held_text_parts = []
            held_message_id = None
            if commentary and on_stream_event is not None:
                self._schedule_stream_event(
                    on_stream_event,
                    StreamEvent(
                        kind=KIND_COMMENTARY,
                        summary=commentary,
                        delivery_class=DELIVERY_USER_COMMENTARY,
                        origin="dsh-cli:commentary",
                    ),
                )

        def _handle_update(params: dict[str, Any]) -> None:
            nonlocal held_message_id, latest_usage, tool_call_count
            if params.get("sessionId") != session_id:
                return
            update = params.get("update")
            if not isinstance(update, Mapping):
                return
            self._touch_activity()
            utype = str(update.get("sessionUpdate") or "")

            if utype == "agent_thought_chunk":
                content = update.get("content")
                if isinstance(content, Mapping) and content.get("type") == "text":
                    thought = str(content.get("text") or "")
                    if thought and on_stream_event is not None:
                        self._schedule_stream_event(
                            on_stream_event,
                            StreamEvent(
                                kind=KIND_THINKING,
                                summary=thought,
                                raw_delta=thought,
                                delivery_class=DELIVERY_REASONING,
                                origin="dsh-cli:thought",
                            ),
                        )
                return

            if utype == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, Mapping) and content.get("type") == "text":
                    chunk = str(content.get("text") or "")
                    if not chunk:
                        return
                    msg_id = str(update.get("messageId") or "")
                    if held_message_id and msg_id and msg_id != held_message_id and held_text_parts:
                        _flush_held_as_commentary()
                    if msg_id:
                        held_message_id = msg_id
                    held_text_parts.append(chunk)
                    if on_stream_event is not None:
                        self._schedule_stream_event(
                            on_stream_event,
                            StreamEvent(
                                kind=KIND_PROVIDER_ACTIVITY,
                                summary="DSH generating response",
                                origin="dsh-cli:activity",
                            ),
                        )
                return

            if utype == "tool_call":
                # Any assistant text emitted right before a tool call is commentary!
                _flush_held_as_commentary()
                tool_call_count += 1
                call_id = str(update.get("toolCallId") or "")
                title = str(update.get("title") or "tool")
                if call_id:
                    tool_titles[call_id] = title
                raw_input = update.get("rawInput")
                kind, file_path, summary = _classify_tool_event(title, raw_input)
                detail = ""
                if raw_input is not None:
                    try:
                        detail = json.dumps(raw_input, ensure_ascii=False)[:600]
                    except Exception:
                        detail = str(raw_input)[:600]
                if on_stream_event is not None:
                    self._schedule_stream_event(
                        on_stream_event,
                        StreamEvent(
                            kind=kind,
                            summary=summary,
                            detail=detail,
                            tool_name=title,
                            file_path=file_path,
                            delivery_class=DELIVERY_TECHNICAL,
                            origin="dsh-cli:tool",
                        ),
                    )
                return

            if utype == "tool_call_update":
                call_id = str(update.get("toolCallId") or "")
                title = tool_titles.get(call_id) or str(update.get("title") or "tool")
                status = str(update.get("status") or "completed")
                preview = _format_tool_result_preview(update.get("content"))
                if on_stream_event is not None:
                    self._schedule_stream_event(
                        on_stream_event,
                        StreamEvent(
                            kind=KIND_ERROR if status == "failed" else KIND_TOOL_END,
                            summary=f"{title} ({status})",
                            detail=preview,
                            tool_name=title,
                            delivery_class=DELIVERY_TECHNICAL,
                            origin="dsh-cli:tool",
                        ),
                    )
                return

            if utype == "usage_update":
                used = update.get("used")
                size = update.get("size")
                if isinstance(used, int) and isinstance(size, int):
                    latest_usage = {"used": used, "size": size}
                return

        self._active_turn_session_id = session_id
        self._active_prompt_rpc_id = None
        self._update_handler = _handle_update

        prompt_blocks = self._build_prompt_blocks(prompt, request_content)
        prompt_task = asyncio.create_task(
            self._rpc_call(
                "session/prompt",
                {"sessionId": session_id, "prompt": prompt_blocks},
                timeout=None,
                on_rpc_id=lambda rid: setattr(self, "_active_prompt_rpc_id", rid),
            )
        )

        try:
            timeout_kind = await self._wait_for_task_with_timeouts(
                prompt_task,
                started_monotonic=started,
            )
            if timeout_kind is not None:
                await self._settle_cancelled_prompt(
                    session_id,
                    prompt_task,
                    reason=f"timeout:{timeout_kind}:{request_id}",
                )
                duration_ms = (time.monotonic() - started) * 1000.0
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    is_success=False,
                    error=self._timeout_diagnostic(timeout_kind, started_monotonic=started),
                )
            result = await prompt_task
        except asyncio.CancelledError:
            await self._settle_cancelled_prompt(session_id, prompt_task, reason=f"cancel:{request_id}")
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            self.logger.error("DSH ACP prompt failed (%s): %s", request_id, exc)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=f"DSH ACP error: {exc}",
            )
        finally:
            self._active_turn_session_id = None
            self._active_prompt_rpc_id = None
            self._update_handler = None
            await self._drain_stream_event_tasks()

        final_text = "".join(held_text_parts).strip()
        stop_reason = (
            str(result.get("stopReason") or "").strip()
            if isinstance(result, Mapping)
            else ""
        )
        duration_ms = (time.monotonic() - started) * 1000.0

        if stop_reason == "cancelled":
            return BackendResponse(
                text=final_text,
                duration_ms=duration_ms,
                is_success=False,
                error="Request cancelled.",
            )

        if final_text and on_stream_event is not None:
            self._schedule_stream_event(
                on_stream_event,
                StreamEvent(
                    kind=KIND_TEXT_DELTA,
                    summary=final_text,
                    raw_delta=final_text,
                    origin="dsh-cli:final",
                ),
            )
            await self._drain_stream_event_tasks()

        if not final_text:
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=f"DSH returned empty response (stopReason={stop_reason or 'unknown'})",
            )

        stream_metadata: dict[str, Any] = {
            "session_id": session_id,
            "stop_reason": stop_reason or "end_turn",
        }
        if latest_usage is not None:
            stream_metadata["context_usage"] = latest_usage

        return BackendResponse(
            text=final_text,
            duration_ms=duration_ms,
            is_success=True,
            stop_reason=stop_reason or "end_turn",
            tool_call_count=tool_call_count,
            stream_metadata=stream_metadata,
        )

    async def _settle_cancelled_prompt(
        self,
        session_id: str,
        prompt_task: asyncio.Task[Any],
        *,
        reason: str,
    ) -> None:
        """Send ACP session/cancel and wait briefly so the session remains reusable."""

        if not prompt_task.done():
            await self._send_cancel_notifications(
                session_id,
                self._active_prompt_rpc_id,
                reason,
            )
            try:
                await asyncio.wait_for(asyncio.shield(prompt_task), timeout=_ACP_CANCEL_GRACE_SEC)
                return
            except Exception:
                pass
            prompt_task.cancel()
            try:
                await prompt_task
            except BaseException:
                pass
            # Worker did not settle cancel cleanly; recycle it so the next turn resumes cleanly
            self.logger.warning(
                "DSH ACP prompt did not settle within %.1fs after cancel; recycling worker",
                _ACP_CANCEL_GRACE_SEC,
            )
            await self._stop_worker(reason=reason)
