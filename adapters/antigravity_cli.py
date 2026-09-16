"""Backend adapter for the Google Antigravity CLI (``agy``).

Verified contract (agy 1.2.3, 2026-09-16; evidence under
exp/antigravity-cli-hashi1/):

* Headless print mode: ``agy -p <prompt> --output-format stream-json``
  emits one JSON object per line on stdout and exits 0 on success.
* stream-json NDJSON event schema (captured 2026-09-16, f2-stream-json.txt):

  ``{"event":"init","conversation_id":"...","init":{...}}``

  ``{"event":"step_update","step_update":{"conversation_id":"...",
  "step_index":N,"state":"ACTIVE|DONE","step_type":"...","text_delta":"..."}}``

  ``{"event":"result","result":{"conversation_id":"...","status":"SUCCESS|ERROR",
  "response":"...","error":"...","duration_seconds":...,"num_turns":...,
  "usage":{...}}}``

* ``--output-format json`` whole-response mode (feasibility report T2/T3):

  ``{"conversation_id":"...","status":"SUCCESS|ERROR","response":"...",
  "error":"...","duration_seconds":...,"num_turns":...,"usage":{...}}``

* Conversation continuity: a later process resumes with ``--conversation <id>``.
* IMPORTANT: agy can exit 0 while reporting ``status:"ERROR"`` inside the JSON
  payload (captured 2026-09-16, f3-stdin.txt), so the parser treats the
  payload status as authoritative, not just the exit code.
* Prompt transport: prompts travel as the ``-p`` argument only.  agy 1.2.4
  silently drops prompts above ~24 KiB of UTF-8 BYTES (empty SUCCESS payload
  with zero usage, rc=0), so the adapter fits prompts to ``MAX_PROMPT_BYTES``
  (head+tail keep with an explicit truncation marker) and treats hollow
  results as errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse, TokenUsage
from adapters.stream_io import iter_stream_lines
from adapters.stream_events import (
    KIND_ERROR,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from orchestrator.pathing import resolve_agy_executable
from orchestrator.process_execution import (
    process_group_kwargs,
    resolve_argv_invocation,
)


class AntigravityCLIAdapter(BaseBackend):
    """Headless Antigravity CLI backend with cross-process conversation resume."""

    # Verified agy 1.2.4 ``-p`` acceptance ceiling (probes, 2026-09-16):
    # 24060 UTF-8 bytes -> real response; 24560 bytes -> hollow success
    # (rc=0, status SUCCESS, empty response, zero usage) in json mode and the
    # same silent drop at 28060 bytes in stream-json mode.  24000 bytes keeps
    # a safety margin below every observed failure.  The legacy char-based
    # name is retained for compatibility; fitting now uses MAX_PROMPT_BYTES.
    MAX_PROMPT_ARG_CHARS = 24000
    MAX_PROMPT_BYTES = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 60 * 60

    HOLLOW_RESULT_ERROR = (
        "Antigravity CLI returned an empty result with zero token usage "
        "(prompt likely exceeded agy's -p byte limit or was silently dropped)."
    )

    _STALE_CONVERSATION_MARKERS = (
        "conversation not found",
        "no such conversation",
        "invalid conversation",
        "unknown conversation",
        "conversation is invalid",
        "conversation expired",
    )

    @staticmethod
    def _is_hollow_result(payload) -> bool:
        """True when agy reports SUCCESS but never called the model."""
        response = str(payload.get("response") or "").strip()
        usage = payload.get("usage") or {}
        total = 0
        for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens"):
            try:
                total += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return not response and total == 0

    def _stale_conversation_error(self, error: str) -> bool:
        lowered = str(error or "").casefold()
        return any(marker in lowered for marker in self._STALE_CONVERSATION_MARKERS)

    _SESSION_STATE_FILE = ".hashi-antigravity-session.json"

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=False,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Antigravity.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self.cmd_base = resolve_agy_executable(
            getattr(self.global_config, "agy_cmd", "agy")
        )
        self._launch_mode = str(
            getattr(self.global_config, "agy_launch_mode", "direct") or "direct"
        ).strip().casefold()
        if self._launch_mode not in {"direct", "user-session"}:
            self.logger.warning(
                "Unknown agy_launch_mode %r; falling back to 'direct'.",
                self._launch_mode,
            )
            self._launch_mode = "direct"
        self._launcher_script = (
            Path(__file__).resolve().parent / "agy_user_session_launcher.py"
        )
        self.logger.info("agy launch mode: %s", self._launch_mode)
        self._conversation_id: str | None = None
        extra = dict(getattr(self.config, "extra", {}) or {})
        self._session_mode: bool = bool(extra.get("session_mode", True))
        self._output_format: str = str(extra.get("output_format") or "stream-json").strip()
        if self._output_format not in {"stream-json", "json"}:
            self._output_format = "stream-json"
        self._result_payload: dict = {}
        self.access_root = str(self.config.resolve_access_root())

    # ------------------------------------------------------------------
    # conversation_id lifecycle
    # ------------------------------------------------------------------

    def _session_state_path(self) -> Path:
        return self.config.workspace_dir / self._SESSION_STATE_FILE

    def _load_session_state(self) -> None:
        path = self._session_state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            conversation_id = str(data.get("conversation_id") or "").strip()
            if conversation_id:
                self._conversation_id = conversation_id
                self.logger.info(f"Loaded conversation_id={conversation_id}")
        except (OSError, ValueError):
            self._conversation_id = None

    def _persist_session_state(self) -> None:
        path = self._session_state_path()
        try:
            if self._session_mode and self._conversation_id:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"conversation_id": self._conversation_id}),
                    encoding="utf-8",
                )
            elif path.exists():
                path.unlink()
        except OSError as exc:  # persistence failure must never fail the reply
            self.logger.warning(f"Could not persist conversation_id: {exc}")

    async def initialize(self) -> bool:
        self.logger.info("Initializing Antigravity CLI backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._load_session_state()
        try:
            invocation = resolve_argv_invocation(
                self._launcher_argv([self.cmd_base, "--version"])
            )
            proc = await asyncio.create_subprocess_exec(
                *invocation.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_group_kwargs(),
            )
            stdout, stderr = await proc.communicate()
            try:
                with open(r"C:\ProgramData\HASHI\HASHI3\tmp\rika-plana-20260916\probes\worker-init-trace.log", "a", encoding="utf-8") as _fh:
                    _fh.write(
                        "version check: rc=%s out=%r err=%r\n"
                        % (proc.returncode,
                            stdout.decode(errors="replace")[:200],
                            stderr.decode(errors="replace")[:800])
                    )
            except Exception:
                pass
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                self.logger.error(f"Antigravity CLI version check failed: {err}")
                return False
            version = stdout.decode(errors="replace").strip()
            self.logger.info(f"Antigravity CLI version: {version}")
        except Exception as exc:
            self.logger.error(f"Antigravity CLI not accessible: {exc}")
            return False
        return True

    async def handle_new_session(self) -> bool:
        old = self._conversation_id
        self._conversation_id = None
        self._persist_session_state()
        self.logger.info(f"Antigravity session reset (previous conversation_id={old}).")
        return True

    def set_session_mode(self, enabled: bool):
        """Enable/disable conversation persistence (called by the runtime)."""
        self._session_mode = bool(enabled)
        if not self._session_mode:
            self._conversation_id = None
        self.logger.info(
            f"Antigravity session mode {'enabled' if self._session_mode else 'disabled'}."
        )

    # ------------------------------------------------------------------
    # stream-json / json event parsing
    # ------------------------------------------------------------------

    def _emit_stream_event(self, se: StreamEvent, on_stream_event: StreamCallback) -> None:
        if on_stream_event is None:
            return
        asyncio.create_task(on_stream_event(se))

    def _parse_stream_json_line(
        self, raw: str, on_stream_event: StreamCallback, text_fragments: list[str],
    ) -> tuple[bool, str]:
        """Parse one agy stream-json NDJSON line.

        Returns ``(done, error)``. The payload ``status`` is authoritative
        even when the process exit code is 0.
        """
        raw = raw.strip()
        if not raw:
            return False, ""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return False, ""

        etype = event.get("event", "")
        if etype == "init":
            cid = event.get("conversation_id")
            if cid:
                self._conversation_id = str(cid)
            self._emit_stream_event(
                StreamEvent(kind=KIND_PROGRESS, summary="Antigravity task started"),
                on_stream_event,
            )
            return False, ""

        if etype == "step_update":
            step_update = event.get("step_update") or {}
            cid = step_update.get("conversation_id")
            if cid:
                self._conversation_id = str(cid)
            step_type = str(step_update.get("step_type") or "")
            text = step_update.get("text_delta") or ""
            if text:
                text_fragments.append(str(text))
                if step_type == "agent_response":
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_TEXT_DELTA, summary=str(text)),
                        on_stream_event,
                    )
                else:
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_PROGRESS, summary=str(text)[:120]),
                        on_stream_event,
                    )
            if step_type and step_type not in {"user_input", "agent_response"}:
                if step_update.get("state") == "DONE":
                    self._emit_stream_event(
                        StreamEvent(kind=KIND_TOOL_END, summary=f"{step_type} done"),
                        on_stream_event,
                    )
                else:
                    self._emit_stream_event(
                        StreamEvent(
                            kind=KIND_TOOL_START,
                            summary=step_type,
                            tool_name=step_type,
                        ),
                        on_stream_event,
                    )
            return False, ""

        if etype == "result":
            result = event.get("result") or {}
            self._result_payload = result
            cid = result.get("conversation_id")
            if cid:
                self._conversation_id = str(cid)
            status = str(result.get("status") or "").upper()
            if status == "ERROR":
                err = str(result.get("error") or "Antigravity CLI reported an error.")
                self._emit_stream_event(
                    StreamEvent(kind=KIND_ERROR, summary=err[:100]),
                    on_stream_event,
                )
                return True, err
            if self._is_hollow_result(result):
                return True, self.HOLLOW_RESULT_ERROR
            return True, ""

        return False, ""

    def _parse_json_line(self, raw: str) -> tuple[bool, str]:
        """Parse one whole-response ``--output-format json`` payload.

        Non-JSON noise lines are tolerated; the payload ``status`` is
        authoritative even when the process exit code is 0.
        """
        raw = raw.strip()
        if not raw:
            return False, ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False, ""
        if not isinstance(payload, dict) or "status" not in payload:
            return False, ""
        self._result_payload = payload
        cid = payload.get("conversation_id")
        if cid:
            self._conversation_id = str(cid)
        status = str(payload.get("status") or "").upper()
        if status == "ERROR":
            err = str(payload.get("error") or "Antigravity CLI reported an error.")
            return True, err
        if self._is_hollow_result(payload):
            return True, self.HOLLOW_RESULT_ERROR
        return True, ""

    @staticmethod
    def _usage_from_payload(payload) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None

        def _num(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return TokenUsage(
            input_tokens=_num("input_tokens"),
            output_tokens=_num("output_tokens"),
            thinking_tokens=_num("thinking_tokens"),
            prompt_cache_hit_tokens=_num("cache_read_tokens"),
            prompt_cache_miss_tokens=_num("cache_write_tokens"),
        )

    # ------------------------------------------------------------------
    # request handling
    # ------------------------------------------------------------------

    def _build_cmd(self, prompt: str) -> list[str]:
        cmd = [
            self.cmd_base,
            "-p", prompt,
            "--model", self.config.model,
            "--output-format", self._output_format,
            "--print-timeout", f"{int(self.DEFAULT_IDLE_TIMEOUT_SEC)}s",
            "--dangerously-skip-permissions",
        ]
        if self._session_mode and self._conversation_id:
            cmd.extend(["--conversation", self._conversation_id])
        for directory in self.effective_add_dirs:
            cmd.extend(["--add-dir", str(directory)])
        return cmd

    def _launcher_argv(self, cmd: list[str]) -> tuple[str, ...]:
        '''Prefix the user-session launcher when launch mode requires it.

        In ``user-session`` mode the service (LocalSystem) never executes
        agy directly; it runs the launcher, which borrows the active console
        session user token (Plan A, DPAPI boundary untouched).  ``direct``
        mode keeps the original direct-execution behaviour as a configurable
        fallback.
        '''
        if self._launch_mode != "user-session" or os.name != "nt":
            return tuple(cmd)
        try:
            workdir = str(self.effective_workdir)
        except Exception:
            workdir = ""
        if workdir:
            return (
                sys.executable,
                str(self._launcher_script),
                "--cwd",
                workdir,
                "--",
                *tuple(cmd),
            )
        return (sys.executable, str(self._launcher_script), "--", *tuple(cmd))

    @staticmethod
    def _cut_bytes(text: str, max_bytes: int, from_end: bool = False) -> str:
        """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes."""
        raw = text.encode("utf-8", errors="replace")
        if len(raw) <= max_bytes:
            return text
        cut = raw[-max_bytes:] if from_end else raw[:max_bytes]
        return cut.decode("utf-8", errors="ignore")

    def _fit_prompt_for_argv(self, prompt: str) -> str:
        """Fit the prompt to the measured agy ``-p`` transport ceiling.

        agy 1.2.4 accepts up to ~24 KiB measured in UTF-8 BYTES and silently
        drops oversized prompts (empty success, zero usage) instead of
        erroring; the Windows CreateProcess command line additionally caps
        argv at 32767 chars.  Oversized prompts keep the HEAD (system
        instructions / role framing) and the TAIL (recent context and the
        latest user request) and replace the excised middle with an explicit
        marker, so content is never dropped silently.
        """
        if len(prompt.encode("utf-8", errors="replace")) <= self.MAX_PROMPT_BYTES:
            return prompt
        marker = (
            "\n[Truncation marker: earlier context was truncated by the "
            "antigravity-cli adapter (the middle of this prompt, i.e. older "
            "conversation history / intermediate context) to fit agy's "
            "verified -p byte limit. Head instructions and the latest "
            "request were kept.]\n"
        )
        remaining = max(1, self.MAX_PROMPT_BYTES - len(marker.encode("utf-8")))
        head_budget = remaining * 40 // 100
        tail_budget = remaining - head_budget
        head = self._cut_bytes(prompt, head_budget)
        tail = self._cut_bytes(prompt, tail_budget, from_end=True)
        fitted = head + marker + tail
        self.logger.warning(
            "Prompt fitted for agy transport: %d chars / %d bytes -> "
            "%d chars / %d bytes",
            len(prompt),
            len(prompt.encode("utf-8", errors="replace")),
            len(fitted),
            len(fitted.encode("utf-8", errors="replace")),
        )
        return fitted

    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        if not prompt or not prompt.strip():
            return BackendResponse(
                text="",
                duration_ms=0,
                error="Empty prompt. Request was not sent to Antigravity CLI.",
                is_success=False,
            )
        prompt = self._fit_prompt_for_argv(prompt)

        cmd = self._build_cmd(prompt)
        self._result_payload = {}
        started = time.perf_counter()
        self.logger.info(
            f"Launching Antigravity request {request_id} "
            f"(conversation_id={self._conversation_id}, retry={is_retry}, "
            f"output_format={self._output_format}, prompt_len={len(prompt)})"
        )
        try:
            invocation = resolve_argv_invocation(self._launcher_argv(cmd))
            self.current_proc = await asyncio.create_subprocess_exec(
                *invocation.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.effective_workdir),
                **process_group_kwargs(),
            )
            self.logger.info(
                f"Antigravity subprocess started for {request_id} "
                f"(pid={self.current_proc.pid})"
            )
            self._touch_activity()
        except Exception as exc:
            self.current_proc = None
            return BackendResponse(
                text="",
                duration_ms=0,
                error=f"Failed to start Antigravity CLI ({self.cmd_base}): {exc}",
                is_success=False,
            )

        try:
            response = await self._read_streaming(
                request_id, started, cmd, on_stream_event,
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
        except Exception as exc:
            return BackendResponse(text="", duration_ms=0, error=str(exc), is_success=False)
        if (
            response.error
            and self._conversation_id
            and not is_retry
            and self._stale_conversation_error(response.error)
        ):
            stale = self._conversation_id
            self._conversation_id = None
            self._persist_session_state()
            self.logger.warning(
                "Dropping stale agy conversation %s and retrying once: %s",
                stale,
                response.error[:160],
            )
            return await self.generate_response(
                prompt,
                request_id,
                is_retry=True,
                silent=silent,
                on_stream_event=on_stream_event,
            )
        return response

    async def _read_streaming(
        self,
        request_id: str,
        started: float,
        cmd: list[str],
        on_stream_event: StreamCallback,
    ) -> BackendResponse:
        proc = self.current_proc  # local ref — shutdown() may null self.current_proc
        text_fragments: list[str] = []
        stdout_line_count = 0
        stdout_tail: list[str] = []
        stderr_lines: list[str] = []
        done_with_error: str | None = None
        timeout_kind: str | None = None

        async def _read_stderr():
            async for line in iter_stream_lines(proc.stderr):
                self._touch_activity()
                stderr_lines.append(line.decode(errors="replace"))

        stderr_task = asyncio.create_task(_read_stderr())

        def _handle_line(decoded: str):
            nonlocal done_with_error
            if self._output_format == "json":
                done, err = self._parse_json_line(decoded)
            else:
                done, err = self._parse_stream_json_line(
                    decoded, on_stream_event, text_fragments,
                )
            if done:
                done_with_error = err

        async def _read_stdout():
            nonlocal stdout_line_count
            async for line in iter_stream_lines(proc.stdout):
                self._touch_activity()
                stdout_line_count += 1
                decoded = line.decode(errors="replace")
                stdout_tail.append(decoded.strip())
                if len(stdout_tail) > 20:
                    stdout_tail.pop(0)
                _handle_line(decoded)

        stdout_task = asyncio.create_task(_read_stdout())
        self._active_read_tasks = [stdout_task, stderr_task]

        while proc.returncode is None:
            idle_for = self._last_activity_age()
            if idle_for >= self.DEFAULT_IDLE_TIMEOUT_SEC:
                timeout_kind = "idle"
                break
            wait_slice = min(5.0, max(0.1, self.DEFAULT_IDLE_TIMEOUT_SEC - idle_for))
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_slice)
            except asyncio.TimeoutError:
                continue

        if timeout_kind is not None:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            pid = getattr(proc, "pid", "unknown")
            diagnostic = self._timeout_diagnostic(
                timeout_kind,
                started_monotonic=started,
            )
            self.logger.error(
                f"Antigravity request {request_id} {timeout_kind}-timed out "
                f"(pid={pid}, duration_ms={duration_ms}, {diagnostic})"
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
                    f"Antigravity CLI was idle for {self.DEFAULT_IDLE_TIMEOUT_SEC}s "
                    f"with no output."
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
            f"Antigravity request {request_id} exited "
            f"(returncode={returncode}, duration_ms={duration_ms}, "
            f"stdout_lines={stdout_line_count}, stderr_lines={len(stderr_lines)}, "
            f"conversation_id={self._conversation_id})"
        )

        if done_with_error:
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=done_with_error,
                is_success=False,
            )

        payload = self._result_payload
        response_text = str(payload.get("response") or "").strip()
        if not response_text:
            response_text = "".join(text_fragments).strip()

        if not payload:
            err_msg = "".join(stderr_lines).strip()
            if returncode != 0:
                details = err_msg
                if stdout_tail:
                    details = (
                        (details + "\nstdout tail:\n" if details else "stdout tail:\n")
                        + "\n".join(stdout_tail[-5:])
                    ).strip()
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    error=details or "Antigravity CLI failed.",
                    is_success=False,
                )
            if "run ended with no output" in "".join(stderr_lines):
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    error=(
                        "Antigravity CLI ended with no output and no recorded "
                        "error (prompt may have been silently dropped)."
                    ),
                    is_success=False,
                )
            if not response_text:
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    error="Antigravity CLI produced no result payload.",
                    is_success=False,
                )

        metadata = {
            "conversation_id": payload.get("conversation_id"),
            "num_turns": payload.get("num_turns"),
            "duration_seconds": payload.get("duration_seconds"),
            "status": payload.get("status"),
        }
        if self._session_mode and self._conversation_id:
            self._persist_session_state()

        return BackendResponse(
            text=response_text,
            duration_ms=duration_ms,
            is_success=True,
            usage=self._usage_from_payload(payload),
            stream_metadata=metadata,
        )

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
