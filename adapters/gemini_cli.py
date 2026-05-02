from __future__ import annotations
import re
import json
import os
import signal
import time
import asyncio
import logging
from pathlib import Path

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    StreamCallback, StreamEvent,
    KIND_THINKING, KIND_TOOL_START, KIND_TOOL_END,
    KIND_FILE_READ, KIND_FILE_EDIT, KIND_SHELL_EXEC,
    KIND_TEXT_DELTA, KIND_PROGRESS, KIND_ERROR,
)


class GeminiCLIAdapter(BaseBackend):
    MAX_PROMPT_ARG_CHARS = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 1800

    # Heuristic patterns to detect tool/file activity from Gemini CLI stderr.
    # Gemini CLI doesn't emit structured events, but it does log to stderr.
    _STDERR_PATTERNS = [
        (re.compile(r"(?:Reading|read)\s+(.+)", re.IGNORECASE), KIND_FILE_READ, "Read"),
        (re.compile(r"(?:Writing|Editing|wrote|edited)\s+(.+)", re.IGNORECASE), KIND_FILE_EDIT, "Edit"),
        (re.compile(r"(?:Running|Executing|shell|bash|command)\s*:?\s*(.+)", re.IGNORECASE), KIND_SHELL_EXEC, "Bash"),
        (re.compile(r"(?:Searching|grep|rg|find)\s+(.+)", re.IGNORECASE), KIND_TOOL_START, "Search"),
        (re.compile(r"(?:Thinking|thinking)", re.IGNORECASE), KIND_THINKING, ""),
    ]

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Gemini.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self.cmd_base = self.global_config.gemini_cmd
        self.system_md_path = None
        self.access_root = str(self.config.resolve_access_root())

    def _resolve_system_md_path(self) -> Path | None:
        candidates = []
        if self.config.system_md:
            candidates.append(Path(self.config.system_md))
        candidates.extend(
            [
                self.config.workspace_dir / "agent.md",
                self.config.workspace_dir / "AGENT.md",
                self.config.workspace_dir / "gemini.md",
                self.config.workspace_dir / "GEMINI.md",
            ]
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    async def initialize(self) -> bool:
        self.logger.info("Initializing Gemini CLI backend (stateless mode)...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.system_md_path = self._resolve_system_md_path()

        # Verify the Gemini CLI is actually accessible (like claude_cli does)
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
                self.logger.error(f"Gemini CLI version check failed: {err}")
                return False
            version = stdout.decode(errors="replace").strip()
            self.logger.info(f"Gemini CLI version: {version}")
        except Exception as e:
            self.logger.error(f"Gemini CLI not accessible: {e}")
            return False

        return True

    async def handle_new_session(self) -> bool:
        self.logger.info("Gemini backend is stateless. /new acknowledged.")
        return True

    def _parse_stderr_line(self, line: str, on_stream_event: StreamCallback) -> None:
        """Heuristically parse a Gemini stderr line and emit a StreamEvent."""
        if on_stream_event is None:
            return
        line = line.strip()
        if not line:
            return
        for pattern, kind, tool_name in self._STDERR_PATTERNS:
            m = pattern.search(line)
            if m:
                detail = m.group(1) if m.lastindex else ""
                summary = f"{tool_name}: {detail}".strip(": ") if tool_name else line[:80]
                asyncio.create_task(on_stream_event(
                    StreamEvent(kind=kind, summary=summary, tool_name=tool_name, detail=detail[:200])
                ))
                return
        # Unmatched stderr — emit as progress if it looks meaningful
        if len(line) > 5 and not line.startswith("Warning"):
            asyncio.create_task(on_stream_event(
                StreamEvent(kind=KIND_PROGRESS, summary=line[:80])
            ))

    # ------------------------------------------------------------------
    # Stream-JSON event parsing (used when verbose mode triggers -o stream-json)
    # ------------------------------------------------------------------

    def _emit_stream_event(self, se: StreamEvent, on_stream_event: StreamCallback) -> None:
        """Fire-and-forget emit of a StreamEvent via the async callback."""
        if on_stream_event is None:
            return
        asyncio.create_task(on_stream_event(se))

    def _parse_stream_json_line(
        self, raw: str, on_stream_event: StreamCallback, text_fragments: list[str],
    ) -> bool:
        """
        Parse a single stream-json line from Gemini CLI.
        Appends assistant text to text_fragments.
        Returns True when a result event is seen (signals completion).

        Gemini stream-json emits JSONL objects:
          {"type":"init","session_id":"...","model":"..."}
          {"type":"message","role":"assistant","content":"...","delta":true}
          {"type":"tool_use","tool_name":"Read","parameters":{...}}
          {"type":"tool_result","tool_id":"...","status":"success","output":"..."}
          {"type":"error","severity":"warning","message":"..."}
          {"type":"result","status":"success","stats":{...}}
        """
        raw = raw.strip()
        if not raw:
            return False
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return False

        etype = event.get("type", "")

        # --- Result event: signals end of response ---
        if etype == "result":
            return True

        # --- Assistant message (streaming deltas) ---
        if etype == "message" and event.get("role") == "assistant":
            content = event.get("content") or ""
            if content:
                text_fragments.append(content)
                self._emit_stream_event(
                    StreamEvent(kind=KIND_TEXT_DELTA, summary=content[:200]),
                    on_stream_event,
                )
            return False

        # --- Tool use ---
        if etype == "tool_use":
            tool_name = event.get("tool_name", event.get("name", "unknown"))
            params = event.get("parameters", {})

            kind = KIND_TOOL_START
            summary = tool_name
            file_path = ""

            if tool_name in ("Read", "ReadFile", "read_file"):
                kind = KIND_FILE_READ
                file_path = params.get("file_path", params.get("path", ""))
                summary = f"Read: {file_path}"
            elif tool_name in ("Edit", "WriteFile", "write_file", "Write"):
                kind = KIND_FILE_EDIT
                file_path = params.get("file_path", params.get("path", ""))
                summary = f"{tool_name}: {file_path}"
            elif tool_name in ("Shell", "shell", "Bash", "bash", "RunCommand"):
                kind = KIND_SHELL_EXEC
                cmd = (params.get("command", params.get("cmd", "")) or "")[:80]
                summary = f"Cmd: {cmd}"
            elif tool_name in ("Search", "Grep", "grep", "GoogleSearch"):
                pattern = (params.get("pattern", params.get("query", "")) or "")[:40]
                summary = f"Search: {pattern}"
            elif tool_name in ("Glob", "glob", "ListFiles"):
                pattern = (params.get("pattern", "") or "")[:40]
                summary = f"Glob: {pattern}"

            self._emit_stream_event(
                StreamEvent(kind=kind, summary=summary[:100], tool_name=tool_name, file_path=file_path),
                on_stream_event,
            )
            return False

        # --- Tool result ---
        if etype == "tool_result":
            output = event.get("output", event.get("content", ""))
            status = event.get("status", "")
            if event.get("error"):
                preview = str(event["error"].get("message", event["error"]))[:80]
                self._emit_stream_event(
                    StreamEvent(kind=KIND_TOOL_END, summary=f"-> error: {preview}"),
                    on_stream_event,
                )
            elif len(output) > 120:
                self._emit_stream_event(
                    StreamEvent(kind=KIND_TOOL_END, summary=f"-> ({len(output)} chars)"),
                    on_stream_event,
                )
            else:
                self._emit_stream_event(
                    StreamEvent(kind=KIND_TOOL_END, summary=f"-> {output[:80]}"),
                    on_stream_event,
                )
            return False

        # --- Error event ---
        if etype == "error":
            msg = (event.get("message") or "")[:100]
            self._emit_stream_event(
                StreamEvent(kind=KIND_ERROR, summary=msg),
                on_stream_event,
            )
            return False

        return False

    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        if not prompt or not prompt.strip():
            return BackendResponse(
                text="",
                duration_ms=0,
                error="Empty prompt. Request was not sent to Gemini CLI.",
                is_success=False,
            )

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
            "--model",
            self.config.model,
            "-o",
            output_format,
            "--approval-mode",
            "yolo",
            "--include-directories",
            self.effective_add_dir,
        ]
        try:
            started = time.perf_counter()
            effective_workdir = self.effective_workdir
            self.logger.info(
                f"Launching Gemini request {request_id} "
                f"(stateless=True, retry={is_retry}, stdin={stdin_data is not None}, "
                f"streaming={use_streaming}, "
                f"prompt_len={len(prompt)}, cwd={effective_workdir}, system_md={self.system_md_path})"
            )
            _extra_kwargs = {}
            if os.name != "nt":
                # Put the subprocess in its own process group so force_kill_process_tree
                # can kill all child processes (Node workers, tool subprocesses, etc.)
                # via os.killpg, preventing orphaned pipe holders after /stop.
                _extra_kwargs["start_new_session"] = True
            self.current_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(effective_workdir),
                limit=1024 * 1024,  # 1 MB readline buffer (default 64 KB too small for large output lines)
                **_extra_kwargs,
            )
            self.logger.info(
                f"Gemini subprocess started for {request_id} "
                f"(pid={self.current_proc.pid}, cmd={cmd})"
            )
            self._touch_activity()

            return await self._read_streaming(
                request_id, started, stdin_data, cmd, is_retry, silent, on_stream_event,
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
    # Streaming path: parse stdout as JSONL (stream-json mode)
    # ------------------------------------------------------------------

    async def _read_streaming(
        self,
        request_id: str,
        started: float,
        stdin_data: bytes | None,
        cmd: list[str],
        is_retry: bool,
        silent: bool,
        on_stream_event: StreamCallback,
    ) -> BackendResponse:
        # Capture local ref to avoid race with shutdown() nulling self.current_proc
        proc = self.current_proc

        # Send stdin
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
        elif proc.stdin is not None:
            proc.stdin.close()

        text_fragments: list[str] = []
        stdout_line_count = 0
        stderr_lines: list[str] = []
        timeout_kind: str | None = None

        async def _read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._touch_activity()
                decoded = line.decode(errors="replace")
                stderr_lines.append(decoded)

        stderr_task = asyncio.create_task(_read_stderr())

        # Emit a thinking event at start
        self._emit_stream_event(
            StreamEvent(kind=KIND_THINKING, summary="Thinking..."),
            on_stream_event,
        )

        async def _read_stdout():
            nonlocal stdout_line_count
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._touch_activity()
                stdout_line_count += 1
                decoded = line.decode(errors="replace")

                # Parse the JSONL event; assistant text is accumulated in text_fragments
                self._parse_stream_json_line(decoded, on_stream_event, text_fragments)

        stdout_task = asyncio.create_task(_read_stdout())

        # Track active read tasks so shutdown() can cancel them if the process is killed
        # but orphaned child processes keep the pipes open.
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
                f"Gemini request {request_id} {timeout_kind}-timed out "
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
                    f"Gemini CLI was idle for {self.IDLE_TIMEOUT_SEC}s with no output."
                    if timeout_kind == "idle"
                    else f"Gemini CLI exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s."
                ),
                is_success=False,
            )

        # Use return_exceptions=True so that cancellations from shutdown() don't propagate
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        self._active_read_tasks = []
        await proc.wait()
        returncode = proc.returncode
        self.current_proc = None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        self.logger.info(
            f"Gemini request {request_id} exited "
            f"(returncode={returncode}, duration_ms={duration_ms}, streaming=True, "
            f"stdout_events={stdout_line_count}, stderr_lines={len(stderr_lines)})"
        )

        if returncode != 0:
            err_msg = "".join(stderr_lines).strip()
            if "empty inlineData parameter" in err_msg:
                if not is_retry:
                    self.logger.warning(f"Retrying {request_id} once after inlineData failure.")
                    retry_prompt = stdin_data.decode(errors="replace") if stdin_data else " "
                    return await self.generate_response(
                        retry_prompt, request_id, is_retry=True, silent=silent,
                        on_stream_event=on_stream_event,
                    )
                err_msg = (
                    "Gemini returned empty inlineData after a file/image turn. "
                    "Please retry the request."
                )
            return BackendResponse(text="", duration_ms=duration_ms, error=err_msg, is_success=False)

        # Assemble response from accumulated assistant message fragments
        response = "".join(text_fragments).strip()

        # if response and not silent:
            # self.emit_console_text(response + "\n", self.logger)

        return BackendResponse(text=response, duration_ms=duration_ms, is_success=True)

    # ------------------------------------------------------------------
    # Blocking path: original communicate()
    # ------------------------------------------------------------------

    async def _read_blocking(
        self,
        request_id: str,
        started: float,
        stdin_data: bytes | None,
        cmd: list[str],
        is_retry: bool,
        silent: bool,
    ) -> BackendResponse:
        proc = self.current_proc  # local ref — shutdown() may null self.current_proc
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
                f"Gemini request {request_id} timed out "
                f"(pid={pid}, duration_ms={duration_ms}, stateless=True, "
                f"retry={is_retry}, stdin={stdin_data is not None}, prompt_len=N/A, "
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
                error="Gemini CLI timed out while generating a response.",
                is_success=False,
            )

        returncode = proc.returncode
        self.current_proc = None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self.logger.info(
            f"Gemini request {request_id} exited "
            f"(returncode={returncode}, duration_ms={duration_ms}, "
            f"stdout_bytes={len(stdout_data)}, stderr_bytes={len(stderr_data)})"
        )
        if returncode == 0 and stderr_data:
            stderr_preview = stderr_data.decode(errors="replace").strip()
            if len(stderr_preview) > 300:
                stderr_preview = stderr_preview[:300].rstrip() + " ...[truncated]"
            self.logger.info(
                f"Gemini request {request_id} stderr preview: {stderr_preview}"
            )

        if returncode != 0:
            err_msg = stderr_data.decode(errors="replace").strip()
            if "empty inlineData parameter" in err_msg:
                if not is_retry:
                    self.logger.warning(f"Retrying {request_id} once after inlineData failure.")
                    retry_prompt = stdin_data.decode(errors="replace") if stdin_data else " "
                    return await self.generate_response(
                        retry_prompt,
                        request_id,
                        is_retry=True,
                        silent=silent,
                    )
                err_msg = (
                    "Gemini returned empty inlineData after a file/image turn. "
                    "Please retry the request."
                )
            return BackendResponse(text="", duration_ms=duration_ms, error=err_msg, is_success=False)

        response = stdout_data.decode(errors="replace").strip()
        # if response and not silent:
            # self.emit_console_text(response + "\n", self.logger)

        # Remove stray thinking marker lines occasionally emitted in CLI output.
        parts = re.split(r"à§ƒà¦¤\S*\n?", response)
        clean = [p.strip() for p in parts if p.strip() and "CRITICAL INSTRUCTION" not in p]
        if clean:
            response = clean[-1]

        return BackendResponse(text=response, duration_ms=duration_ms, is_success=True)

    async def shutdown(self):
        if self.current_proc:
            await self.force_kill_process_tree(
                self.current_proc,
                logger=self.logger,
                reason="backend_shutdown",
            )
            self.current_proc = None
        # Cancel any in-flight stdout/stderr read tasks. After the process is killed,
        # orphaned child processes may still hold the pipes open, blocking readline()
        # indefinitely. Cancelling the tasks unblocks generate_response immediately.
        for task in self._active_read_tasks:
            if not task.done():
                task.cancel()
        self._active_read_tasks = []
