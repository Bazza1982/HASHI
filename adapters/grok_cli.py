from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    KIND_ERROR,
    KIND_FILE_EDIT,
    KIND_PROGRESS,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)


class GrokCLIAdapter(BaseBackend):
    MAX_PROMPT_ARG_CHARS = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 1800

    def _define_capabilities(self) -> BackendCapabilities:
        capabilities = BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )
        capabilities.supports_answer_stream = True
        return capabilities

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Grok.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self.cmd_base = getattr(self.global_config, "grok_cmd", "grok")
        if os.name == "nt" and Path(self.cmd_base).suffix.lower() not in {".cmd", ".exe", ".bat", ".ps1"}:
            self.cmd_base = f"{self.cmd_base}.cmd"
        self.access_root = str(self.config.resolve_access_root())
        self._session_id: str | None = None

    async def initialize(self) -> bool:
        self.logger.info("Initializing Grok CLI backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
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
                self.logger.error(f"Grok CLI version check failed: {err}")
                return False
            version = stdout.decode(errors="replace").strip()
            self.logger.info(f"Grok CLI version: {version}")
            return True
        except Exception as exc:
            self.logger.error(f"Grok CLI not accessible: {exc}")
            return False

    async def handle_new_session(self) -> bool:
        old_id = self._session_id
        self._session_id = None
        self.logger.info(f"Grok session reset (previous session_id={old_id}).")
        return True

    async def shutdown(self):
        proc = self.current_proc
        if proc and proc.returncode is None:
            self.logger.warning("Terminating active Grok subprocess during shutdown")
            await self.force_kill_process_tree(proc, self.logger, reason="grok shutdown")
        self.current_proc = None
        for task in self._active_read_tasks:
            if not task.done():
                task.cancel()
        if self._active_read_tasks:
            await asyncio.gather(*self._active_read_tasks, return_exceptions=True)
        self._active_read_tasks = []

    def _build_cmd(self, prompt: str) -> list[str]:
        cmd = [
            self.cmd_base,
            "--no-auto-update",
            "--no-alt-screen",
            "--cwd",
            self.access_root,
            "-m",
            self.config.model,
            "--output-format",
            "streaming-json",
        ]
        if self._session_id:
            cmd.extend(["--resume", self._session_id])
        cmd.extend(["-p", prompt])
        return cmd

    def _extract_content_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(self._extract_content_text(item.get("text") or item.get("content")))
            return "".join(parts)
        if isinstance(value, dict):
            for key in ("text", "content", "data", "value", "message"):
                text = self._extract_content_text(value.get(key))
                if text:
                    return text
        return ""

    def _extract_delta_text(self, event: dict[str, Any]) -> str:
        etype = str(event.get("type") or event.get("event") or "").lower()
        update = str(event.get("sessionUpdate") or event.get("session_update") or "").lower()
        if update == "agent_message_chunk":
            return self._extract_content_text(event.get("content"))
        if etype == "text":
            return self._extract_content_text(event.get("data") or event.get("content") or event.get("text"))
        if any(marker in etype for marker in ("delta", "chunk", "text_delta", "assistant_delta")):
            for key in ("content", "data", "delta", "text", "chunk", "message"):
                text = self._extract_content_text(event.get(key))
                if text:
                    return text
        delta = event.get("delta")
        if isinstance(delta, dict):
            return self._extract_content_text(delta.get("content") or delta.get("text"))
        return ""

    def _extract_final_text(self, event: dict[str, Any]) -> str:
        etype = str(event.get("type") or event.get("event") or "").lower()
        if any(marker in etype for marker in ("final", "result", "complete", "completed")):
            for key in ("text", "content", "message", "result", "output"):
                text = self._extract_content_text(event.get(key))
                if text:
                    return text
        result = event.get("result")
        if isinstance(result, dict):
            return self._extract_content_text(result.get("text") or result.get("output") or result.get("content"))
        return ""

    def _extract_session_id(self, event: dict[str, Any]) -> str | None:
        for key in ("session_id", "sessionId", "session", "conversation_id", "conversationId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    async def _emit(self, on_stream_event: StreamCallback, event: StreamEvent | None) -> None:
        if on_stream_event is not None and event is not None:
            await on_stream_event(event)

    async def _handle_stream_json_line(
        self,
        raw_line: str,
        on_stream_event: StreamCallback,
        deltas: list[str],
    ) -> str:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return ""
        if not isinstance(event, dict):
            return ""

        session_id = self._extract_session_id(event)
        if session_id and self._session_id is None:
            self._session_id = session_id

        delta = self._extract_delta_text(event)
        if delta:
            deltas.append(delta)
            await self._emit(on_stream_event, StreamEvent(kind=KIND_TEXT_DELTA, summary=delta))
            return ""

        etype = str(event.get("type") or event.get("event") or "").lower()
        summary = self._extract_content_text(event.get("summary") or event.get("message") or event.get("text") or event.get("data"))
        if "error" in etype and summary:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_ERROR, summary=summary[:300]))
        elif etype == "thought" or "thinking" in etype or "reasoning" in etype:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_THINKING, summary=summary[:300] or "Grok thinking"))
        elif "tool" in etype and "end" in etype:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_TOOL_END, summary=summary[:300] or "Grok tool finished"))
        elif "tool" in etype:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_TOOL_START, summary=summary[:300] or "Grok tool started"))
        elif "file" in etype:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_FILE_EDIT, summary=summary[:300] or "Grok changed files"))
        elif "shell" in etype or "command" in etype:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_SHELL_EXEC, summary=summary[:300] or "Grok ran a command"))
        elif summary:
            await self._emit(on_stream_event, StreamEvent(kind=KIND_PROGRESS, summary=summary[:300]))

        return self._extract_final_text(event)

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        started = time.time()
        cmd = self._build_cmd(prompt)
        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []
        deltas: list[str] = []
        final_text = ""
        plain_lines: list[str] = []

        extra_kwargs = {}
        if os.name != "nt":
            extra_kwargs["start_new_session"] = True

        try:
            self.current_proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.config.workspace_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **extra_kwargs,
            )
            proc = self.current_proc
            self.logger.info(f"Grok subprocess started for {request_id} (pid={proc.pid})")
            self._touch_activity()

            async def _read_stdout():
                nonlocal final_text
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    self._touch_activity()
                    decoded = line.decode(errors="replace")
                    stdout_lines.append(decoded)
                    parsed_final = await self._handle_stream_json_line(decoded, on_stream_event, deltas)
                    if parsed_final:
                        final_text = parsed_final
                    elif not decoded.lstrip().startswith("{"):
                        plain_lines.append(decoded)

            async def _read_stderr():
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    self._touch_activity()
                    stderr_chunks.append(chunk)

            stdout_task = asyncio.create_task(_read_stdout())
            stderr_task = asyncio.create_task(_read_stderr())
            self._active_read_tasks = [stdout_task, stderr_task]
            await asyncio.wait_for(proc.wait(), timeout=self.HARD_TIMEOUT_SEC)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
            response_text = final_text or "".join(deltas).strip() or "".join(plain_lines).strip()
            if proc.returncode != 0:
                error = stderr or f"Grok CLI exited with code {proc.returncode}"
                return BackendResponse(
                    text=response_text,
                    error=error,
                    is_success=False,
                    duration_ms=(time.time() - started) * 1000,
                )
            if not response_text:
                response_text = self._extract_last_text_from_stdout(stdout_lines)
            if not response_text:
                empty_pattern = self._classify_empty_stdout(stdout_lines)
                if (
                    not is_retry
                    and proc.returncode == 0
                    and empty_pattern == "thought_end_no_text"
                ):
                    self.logger.warning(
                        "Retrying Grok request %s once after thought/end with no answer text.",
                        request_id,
                    )
                    self._session_id = None
                    retry_response = await self.generate_response(
                        self._build_empty_answer_retry_prompt(prompt),
                        request_id,
                        is_retry=True,
                        silent=silent,
                        on_stream_event=on_stream_event,
                    )
                    retry_metadata = dict(retry_response.stream_metadata or {})
                    retry_metadata["grok_empty_answer_retry_attempted"] = True
                    retry_metadata["grok_empty_answer_retry_succeeded"] = retry_response.is_success
                    retry_metadata["grok_empty_answer_pattern"] = empty_pattern
                    retry_response.stream_metadata = retry_metadata
                    if retry_response.is_success and retry_response.text:
                        return retry_response
                    diagnostic = self._summarize_empty_response(stdout_lines, stderr, empty_pattern)
                    retry_error = retry_response.error or "retry returned no answer text"
                    self.logger.warning(
                        "Grok request %s empty-answer retry failed: %s",
                        request_id,
                        retry_error,
                    )
                    return BackendResponse(
                        text="",
                        error=(
                            f"Grok CLI returned no answer text after one retry. "
                            f"{diagnostic}; retry_error={retry_error}"
                        ),
                        is_success=False,
                        duration_ms=(time.time() - started) * 1000,
                        stream_metadata=retry_metadata,
                    )
                diagnostic = self._summarize_empty_response(stdout_lines, stderr, empty_pattern)
                self.logger.warning("Grok request %s returned no answer text: %s", request_id, diagnostic)
                return BackendResponse(
                    text="",
                    error=f"Grok CLI returned no answer text. {diagnostic}",
                    is_success=False,
                    duration_ms=(time.time() - started) * 1000,
                    stream_metadata={
                        "grok_text_delta_count": len(deltas),
                        "grok_empty_answer_pattern": empty_pattern,
                    },
                )
            return BackendResponse(
                text=response_text,
                duration_ms=(time.time() - started) * 1000,
                stream_metadata={"grok_text_delta_count": len(deltas)},
            )
        except asyncio.TimeoutError:
            await self.force_kill_process_tree(self.current_proc, self.logger, reason="grok hard timeout")
            return BackendResponse(
                text="",
                error=f"Grok CLI exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s.",
                is_success=False,
                duration_ms=(time.time() - started) * 1000,
            )
        except Exception as exc:
            self.logger.exception(f"Grok request {request_id} failed")
            return BackendResponse(
                text="",
                error=str(exc),
                is_success=False,
                duration_ms=(time.time() - started) * 1000,
            )
        finally:
            self.current_proc = None
            self._active_read_tasks = []

    def _extract_last_text_from_stdout(self, stdout_lines: list[str]) -> str:
        for line in reversed(stdout_lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                text = self._extract_final_text(event) or self._extract_delta_text(event)
                if text:
                    return text
        return ""

    @staticmethod
    def _build_empty_answer_retry_prompt(original_prompt: str) -> str:
        return (
            "Your previous Grok CLI response ended without any answer text. "
            "Reply with only the final answer to the following request:\n\n"
            f"{original_prompt}"
        )

    def _classify_empty_stdout(self, stdout_lines: list[str]) -> str:
        saw_thought = False
        saw_end = False
        saw_text = False
        saw_side_effect_event = False
        for line in stdout_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type") or event.get("event") or "").lower()
            if self._extract_delta_text(event) or self._extract_final_text(event):
                saw_text = True
            if etype == "thought" or "thinking" in etype or "reasoning" in etype:
                saw_thought = True
            if etype == "end" or "complete" in etype or "completed" in etype:
                saw_end = True
            if any(
                marker in etype
                for marker in (
                    "tool",
                    "shell",
                    "exec",
                    "command",
                    "file",
                    "edit",
                    "write",
                    "patch",
                )
            ):
                saw_side_effect_event = True
        if saw_text:
            return "no_text_extracted"
        if saw_side_effect_event:
            return "side_effect_events_no_text"
        if saw_thought and saw_end:
            return "thought_end_no_text"
        if saw_end:
            return "end_no_text"
        return "no_answer_events"

    def _summarize_empty_response(
        self,
        stdout_lines: list[str],
        stderr: str,
        empty_pattern: str | None = None,
    ) -> str:
        event_types: list[str] = []
        stop_reason = None
        session_id = None
        for line in stdout_lines[-8:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event_types.append("plain")
                continue
            if not isinstance(event, dict):
                event_types.append(type(event).__name__)
                continue
            etype = event.get("type") or event.get("event") or event.get("sessionUpdate") or "unknown"
            event_types.append(str(etype))
            if str(event.get("type") or "").lower() == "end":
                stop_reason = event.get("stopReason") or event.get("stop_reason") or stop_reason
            session_id = self._extract_session_id(event) or session_id
        parts = [
            f"stdout_lines={len(stdout_lines)}",
            f"recent_events={event_types or ['none']}",
            f"empty_answer_pattern={empty_pattern or self._classify_empty_stdout(stdout_lines)}",
        ]
        if stop_reason:
            parts.append(f"stop_reason={stop_reason}")
        if session_id:
            parts.append(f"session_id={session_id}")
        if stderr:
            parts.append(f"stderr={self._preview_text(stderr, 300)}")
        return "; ".join(parts)
