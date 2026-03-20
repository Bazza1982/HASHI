from __future__ import annotations
import json
import os
import time
import asyncio
import logging
from pathlib import Path

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    StreamCallback, StreamEvent,
    KIND_THINKING, KIND_TOOL_START, KIND_TOOL_END,
    KIND_FILE_READ, KIND_FILE_EDIT, KIND_SHELL_EXEC,
    KIND_TEXT_DELTA, KIND_ERROR,
)


class ClaudeCLIAdapter(BaseBackend):
    MAX_PROMPT_ARG_CHARS = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 1800

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Claude.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self.cmd_base = self.global_config.claude_cmd
        self.system_prompt_source = None
        self.effort = (self.config.extra or {}).get("effort", "low")
        self.access_root = str(self.config.resolve_access_root())
        # Session persistence for fixed mode
        self._session_id: str | None = None
        self._session_mode: bool = (self.config.extra or {}).get("session_mode", False)

    def _resolve_system_prompt_source(self) -> Path | None:
        candidates = []
        if self.config.system_md:
            candidates.append(Path(self.config.system_md))
        candidates.append(self.config.workspace_dir / "CLAUDE.md")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    async def initialize(self) -> bool:
        self.logger.info("Initializing Claude CLI backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.system_prompt_source = self._resolve_system_prompt_source()
        if self.system_prompt_source:
            self.logger.info(f"Detected system prompt source at {self.system_prompt_source}")

        try:
            proc = await asyncio.create_subprocess_exec(
                self.cmd_base,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                self.logger.error(f"Claude CLI version check failed: {err}")
                return False
            version = stdout.decode(errors="replace").strip()
            self.logger.info(f"Claude CLI version: {version}")
            return True
        except Exception as e:
            self.logger.error(f"Claude CLI not accessible: {e}")
            return False

    async def handle_new_session(self) -> bool:
        old_sid = self._session_id
        self._session_id = None
        self.logger.info(f"Claude session reset (previous session_id={old_sid}).")
        return True

    def set_session_mode(self, enabled: bool):
        """Enable/disable session persistence (fixed mode)."""
        self._session_mode = enabled
        self._session_id = None  # always clear on mode switch to avoid resuming a stale/non-persistent session
        self.logger.info(f"Session mode set to {'ON' if enabled else 'OFF'}")

    def should_bootstrap_on_startup(self) -> bool:
        return False

    def get_startup_bootstrap_prompt(self) -> str | None:
        return None

    # ------------------------------------------------------------------
    # Stream-JSON event parsing
    # ------------------------------------------------------------------

    def _emit_stream_event(self, se: StreamEvent, on_stream_event: StreamCallback) -> None:
        """Fire-and-forget emit of a StreamEvent via the async callback."""
        if on_stream_event is None:
            return
        asyncio.create_task(on_stream_event(se))

    def _parse_stream_json_line(self, raw: str, on_stream_event: StreamCallback) -> str | None:
        """
        Parse a single stream-json line from Claude CLI.
        Returns final response text if this is a result event, else None.

        Claude CLI stream-json emits NDJL with these top-level types:
          {"type":"system", ...}
          {"type":"stream_event","event":{"type":"content_block_start"|"content_block_delta"|...}}
          {"type":"assistant", ...}           (complete message, ignored when streaming)
          {"type":"result","result":"final text","duration_ms":...,"cost_usd":...}
        """
        raw = raw.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None

        etype = obj.get("type", "")

        # --- System event: capture session_id for resume ---
        if etype == "system":
            sid = obj.get("session_id")
            if sid:
                self._session_id = sid
                self.logger.info(f"Captured session_id: {sid}")
            return None

        # --- Result event: contains the final assembled text ---
        if etype == "result":
            # Also check for session_id in result
            sid = obj.get("session_id")
            if sid:
                self._session_id = sid
            return obj.get("result", "")

        # --- Stream event: wrapped Claude API streaming events ---
        if etype == "stream_event":
            event = obj.get("event") or {}
            return self._handle_stream_event(event, on_stream_event)

        return None

    def _handle_stream_event(self, event: dict, on_stream_event: StreamCallback) -> None:
        """Dispatch a Claude API streaming event to our StreamEvent system."""
        event_type = event.get("type", "")

        # --- content_block_start: tool_use begins ---
        if event_type == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                tool_name = cb.get("name", "unknown")
                kind = KIND_TOOL_START
                summary = tool_name
                file_path = ""
                if tool_name in ("Read", "read"):
                    kind = KIND_FILE_READ
                    summary = f"Read: ..."
                elif tool_name in ("Edit", "edit", "Write", "write"):
                    kind = KIND_FILE_EDIT
                    summary = f"{tool_name}: ..."
                elif tool_name in ("Bash", "bash"):
                    kind = KIND_SHELL_EXEC
                    summary = "Cmd: ..."
                elif tool_name in ("Grep", "grep"):
                    summary = "Grep: ..."
                elif tool_name in ("Glob", "glob"):
                    summary = "Glob: ..."
                elif tool_name in ("Agent", "agent"):
                    summary = "Agent: ..."
                self._emit_stream_event(
                    StreamEvent(kind=kind, summary=summary, tool_name=tool_name, file_path=file_path),
                    on_stream_event,
                )
            elif cb.get("type") == "thinking":
                self._emit_stream_event(
                    StreamEvent(kind=KIND_THINKING, summary="Thinking..."),
                    on_stream_event,
                )
            return None

        # --- content_block_delta: text, tool input, or thinking chunks ---
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")

            if delta_type == "text_delta":
                text = (delta.get("text") or "")[:200]
                if text:
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_TEXT_DELTA, summary=text),
                        on_stream_event,
                    )
            elif delta_type == "thinking_delta":
                text = (delta.get("thinking") or "")[:200]
                if text:
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_THINKING, summary=f"Thinking: {text}"),
                        on_stream_event,
                    )
            elif delta_type == "input_json_delta":
                # Tool input streaming — emit as progress
                partial = (delta.get("partial_json") or "")[:100]
                if partial:
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_TOOL_START, summary=f"... {partial}"),
                        on_stream_event,
                    )
            return None

        # --- content_block_stop: tool finished ---
        if event_type == "content_block_stop":
            self._emit_stream_event(
                StreamEvent(kind=KIND_TOOL_END, summary="→ done"),
                on_stream_event,
            )
            return None

        return None

    # ------------------------------------------------------------------
    # Main generate_response — streaming via readline
    # ------------------------------------------------------------------

    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        prompt_arg = prompt
        stdin_data = None
        if "\n" in prompt or "\r" in prompt or len(prompt) > self.MAX_PROMPT_ARG_CHARS:
            prompt_arg = "."
            stdin_data = prompt.encode("utf-8")
            self.logger.info(
                f"Prompt for {request_id} requires stdin transport; sending full prompt via stdin."
            )

        # Always use stream-json so timeout enforcement can follow real backend
        # activity even when verbose/thinking display is disabled.
        use_streaming = True
        output_format = "stream-json"

        cmd = [
            self.cmd_base,
            "-p",
            prompt_arg,
            "--output-format",
            output_format,
            "--model",
            self.config.model,
            "--effort",
            self.effort,
            "--dangerously-skip-permissions",
            "--add-dir",
            self.access_root,
        ]
        # Session persistence: use --resume when in session mode with an active session
        if self._session_mode and self._session_id:
            cmd.extend(["--resume", self._session_id])
        elif not self._session_mode:
            cmd.append("--no-session-persistence")
        # stream-json needs --verbose and --include-partial-messages for
        # intermediate events (tool calls, text deltas, thinking).
        if use_streaming:
            cmd.append("--verbose")
            cmd.append("--include-partial-messages")

        try:
            started = time.perf_counter()
            self.logger.info(
                f"Launching Claude request {request_id} "
                f"(stateless=True, retry={is_retry}, stdin={stdin_data is not None}, "
                f"streaming={use_streaming}, prompt_len={len(prompt)}, cwd={self.config.workspace_dir})"
            )
            _extra_kwargs = {}
            if os.name != "nt":
                _extra_kwargs["start_new_session"] = True
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.workspace_dir),
                limit=1024 * 1024,  # 1 MB readline buffer (default 64 KB too small for large JSON events)
                **_extra_kwargs,
            )
            self.current_proc = proc  # keep ref for shutdown/kill
            self.logger.info(
                f"Claude subprocess started for {request_id} "
                f"(pid={proc.pid}, cmd={cmd})"
            )
            self._touch_activity()

            return await self._read_streaming(
                proc, request_id, started, stdin_data, cmd, is_retry, silent, on_stream_event,
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Generation cancelled for {request_id}")
            if self.current_proc:
                await self.force_kill_process_tree(
                    self.current_proc,
                    logger=self.logger,
                    reason=f"cancelled:{request_id}",
                )
            raise
        except Exception as e:
            return BackendResponse(text="", duration_ms=0, error=str(e), is_success=False)

    # ------------------------------------------------------------------
    # Streaming path: readline loop with stream-json parsing
    # ------------------------------------------------------------------

    async def _read_streaming(
        self,
        proc,
        request_id: str,
        started: float,
        stdin_data: bytes | None,
        cmd: list[str],
        is_retry: bool,
        silent: bool,
        on_stream_event: StreamCallback,
    ) -> BackendResponse:
        """Read stdout line-by-line, parse stream-json events, assemble final response."""
        # Send stdin if needed
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()

        result_text = ""
        stderr_lines: list[bytes] = []
        timeout_kind: str | None = None

        async def _read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._touch_activity()
                stderr_lines.append(line)

        stderr_task = asyncio.create_task(_read_stderr())

        async def _read_stdout():
            nonlocal result_text
            line_count = 0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                self._touch_activity()
                line_count += 1
                decoded = line.decode(errors="replace")
                fragment = self._parse_stream_json_line(decoded, on_stream_event)
                if fragment is not None:
                    result_text = fragment  # result event overwrites with final text
            self.logger.info(f"[stream-json] stdout EOF after {line_count} lines")

        stdout_task = asyncio.create_task(_read_stdout())
        self._active_read_tasks = [stdout_task, stderr_task]

        hard_deadline = started + self.HARD_TIMEOUT_SEC
        while proc.returncode is None:
            remaining_hard = hard_deadline - time.perf_counter()
            if remaining_hard <= 0:
                timeout_kind = "hard"
                break
            wait_slice = min(5.0, remaining_hard)
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_slice)
            except asyncio.TimeoutError:
                idle_for = time.time() - (self.last_activity_at or time.time())
                if idle_for >= self.IDLE_TIMEOUT_SEC:
                    timeout_kind = "idle"
                    break

        if timeout_kind is not None:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            pid = getattr(proc, "pid", "unknown")
            detail = (
                f"idle for {self.IDLE_TIMEOUT_SEC}s with no output"
                if timeout_kind == "idle"
                else f"exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s"
            )
            self.logger.error(
                f"Claude request {request_id} {timeout_kind}-timed out "
                f"(pid={pid}, duration_ms={duration_ms}, detail={detail})"
            )
            await self.force_kill_process_tree(
                proc, logger=self.logger,
                reason=f"{timeout_kind}-timeout:{request_id}",
            )
            self.current_proc = None
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            self._active_read_tasks = []
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=(
                    f"Claude CLI was idle for {self.IDLE_TIMEOUT_SEC}s with no output."
                    if timeout_kind == "idle"
                    else f"Claude CLI exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s."
                ),
                is_success=False,
            )

        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        self._active_read_tasks = []
        await proc.wait()
        returncode = proc.returncode
        self.current_proc = None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        self.logger.info(
            f"Claude request {request_id} exited "
            f"(returncode={returncode}, duration_ms={duration_ms}, streaming=True)"
        )

        if returncode != 0:
            stderr_text = b"".join(stderr_lines).decode(errors="replace").strip()
            # Handle --no-session-persistence fallback
            if (
                "--no-session-persistence" in " ".join(cmd)
                and "no-session-persistence" in stderr_text.lower()
                and not is_retry
            ):
                fallback_cmd = [c for c in cmd if c != "--no-session-persistence"]
                # Fallback uses blocking communicate(), so force text output format
                try:
                    fmt_idx = fallback_cmd.index("--output-format")
                    fallback_cmd[fmt_idx + 1] = "text"
                except (ValueError, IndexError):
                    pass
                self.logger.warning(
                    f"Claude CLI did not accept --no-session-persistence for {request_id}; "
                    f"retrying once without it."
                )
                return await self._generate_without_flag(
                    fallback_cmd, request_id, stdin_data, silent=silent,
                )
            return BackendResponse(
                text="", duration_ms=duration_ms,
                error=stderr_text or f"Claude CLI exited with code {returncode} (no stderr)",
                is_success=False,
            )

        # if result_text and not silent:
            # self.emit_console_text(result_text + "\n", self.logger)

        return BackendResponse(text=result_text, duration_ms=duration_ms, is_success=True)

    # ------------------------------------------------------------------
    # Blocking path: original communicate() for non-streaming
    # ------------------------------------------------------------------

    async def _read_blocking(
        self,
        proc,
        request_id: str,
        started: float,
        stdin_data: bytes | None,
        cmd: list[str],
        is_retry: bool,
        silent: bool,
    ) -> BackendResponse:
        """Original blocking communicate() path — used when no stream callback."""
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=self.HARD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            pid = getattr(proc, "pid", "unknown")
            proc_snapshot = await self._describe_process(pid) if pid != "unknown" else "<unknown pid>"
            self.logger.error(
                f"Claude request {request_id} timed out "
                f"(pid={pid}, duration_ms={duration_ms}, stateless=True, "
                f"retry={is_retry}, stdin={stdin_data is not None}, "
                f"cmd={cmd}, process_snapshot={self._preview_text(proc_snapshot, 700)})"
            )
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason=f"timeout:{request_id}",
            )
            self.current_proc = None
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error="Claude CLI timed out while generating a response.",
                is_success=False,
            )

        returncode = proc.returncode
        self.current_proc = None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self.logger.info(
            f"Claude request {request_id} exited "
            f"(returncode={returncode}, duration_ms={duration_ms}, "
            f"stdout_bytes={len(stdout_data)}, stderr_bytes={len(stderr_data)})"
        )

        if returncode != 0:
            err_msg = stderr_data.decode(errors="replace").strip()
            if (
                "--no-session-persistence" in " ".join(cmd)
                and "no-session-persistence" in err_msg.lower()
                and not is_retry
            ):
                fallback_cmd = [c for c in cmd if c != "--no-session-persistence"]
                self.logger.warning(
                    f"Claude CLI did not accept --no-session-persistence for {request_id}; retrying once without it."
                )
                return await self._generate_without_flag(
                    fallback_cmd,
                    request_id,
                    stdin_data,
                    silent=silent,
                )
            return BackendResponse(
                text="", duration_ms=duration_ms,
                error=err_msg or f"Claude CLI exited with code {returncode} (no stderr)",
                is_success=False,
            )

        response = stdout_data.decode(errors="replace").strip()
        # if response and not silent:
            # self.emit_console_text(response + "\n", self.logger)

        return BackendResponse(text=response, duration_ms=duration_ms, is_success=True)

    # ------------------------------------------------------------------
    # Fallback: retry without --no-session-persistence
    # ------------------------------------------------------------------

    async def _generate_without_flag(
        self,
        cmd: list[str],
        request_id: str,
        stdin_data: bytes | None,
        silent: bool = False,
    ) -> BackendResponse:
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.config.workspace_dir),
            limit=1024 * 1024,
        )
        self.current_proc = proc
        self._touch_activity()
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=self.HARD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason=f"fallback-timeout:{request_id}",
            )
            self.current_proc = None
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error="Claude CLI timed out while retrying fallback invocation.",
                is_success=False,
            )
        returncode = proc.returncode
        self.current_proc = None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if returncode != 0:
            err_msg = stderr_data.decode(errors="replace").strip()
            return BackendResponse(
                text="", duration_ms=duration_ms,
                error=err_msg or f"Claude CLI exited with code {returncode} (no stderr)",
                is_success=False,
            )
        response = stdout_data.decode(errors="replace").strip()
        # if response and not silent:
            # self.emit_console_text(response + "\n", self.logger)
        return BackendResponse(text=response, duration_ms=duration_ms, is_success=True)

    async def shutdown(self):
        if self.current_proc:
            await self.force_kill_process_tree(
                self.current_proc,
                logger=self.logger,
                reason="backend_shutdown",
            )
            self.current_proc = None
        for task in self._active_read_tasks:
            if not task.done():
                task.cancel()
        self._active_read_tasks = []
