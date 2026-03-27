from __future__ import annotations
import os
import json
import time
import asyncio
import logging
from pathlib import Path

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    StreamCallback, StreamEvent,
    KIND_THINKING, KIND_TOOL_END,
    KIND_FILE_EDIT, KIND_SHELL_EXEC, KIND_PROGRESS,
)


class CodexCLIAdapter(BaseBackend):
    MAX_PROMPT_CHARS = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 3600

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
        self.logger = logging.getLogger(f"Backend.Codex.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self.effort = ((self.config.extra or {}).get("effort") or "medium").lower()
        self.cmd_base = self.global_config.codex_cmd
        if os.name == "nt" and Path(self.cmd_base).suffix.lower() not in {".cmd", ".exe", ".bat", ".ps1"}:
            self.cmd_base = f"{self.cmd_base}.cmd"
        self.access_root = str(self.config.resolve_access_root())
        self.events_log_path = self.config.workspace_dir / "codex_exec_events.jsonl"

    def _should_use_stdin_transport(self, prompt: str) -> bool:
        if "\n" in prompt or "\r" in prompt:
            return True
        if os.name != "nt":
            return False
        cmd_suffix = Path(self.cmd_base).suffix.lower()
        if cmd_suffix not in {".cmd", ".bat"}:
            return False
        # Windows .cmd launch goes through cmd.exe. Always use stdin to avoid
        # the 8191-char cmd.exe limit and quoting inflation from special characters.
        return True

    async def initialize(self) -> bool:
        self.logger.info("Initializing Codex CLI backend...")
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
                self.logger.error(f"Codex CLI version check failed: {err}")
                return False
            version = stdout.decode(errors="replace").strip()
            self.logger.info(f"Codex CLI version: {version}")
            return True
        except Exception as e:
            self.logger.error(f"Codex CLI not accessible: {e}")
            return False

    async def handle_new_session(self) -> bool:
        self.logger.info("Codex backend is stateless. /new acknowledged.")
        return True

    def should_bootstrap_on_startup(self) -> bool:
        return False

    def _clip_text(self, text: str, limit: int) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 32].rstrip() + "\n\n[truncated for Codex prompt size]"

    # Patterns that Codex CLI's internal chunker may misinterpret as chunk
    # separators, causing "Separator is not found, and chunk exceed the limit".
    # We replace them with visually similar but parser-safe alternatives.
    _SEPARATOR_SUBS = [
        # 3+ dashes on a line (markdown HR / YAML front-matter)
        (r"^-{3,}$",   lambda m: "- " * (len(m.group()) // 2)),
        # 3+ equals on a line (markdown heading underline)
        (r"^={3,}$",   lambda m: "= " * (len(m.group()) // 2)),
        # 3+ asterisks on a line (markdown HR)
        (r"^\*{3,}$",  lambda m: "* " * (len(m.group()) // 2)),
        # 3+ underscores on a line (markdown HR)
        (r"^_{3,}$",   lambda m: "_ " * (len(m.group()) // 2)),
        # 3+ tildes on a line (code fences — keep content but break pattern)
        (r"^~{3,}$",   lambda m: "~ " * (len(m.group()) // 2)),
    ]

    def _sanitize_for_codex(self, prompt: str) -> str:
        """Replace separator-like patterns that confuse Codex's internal chunker."""
        import re
        lines = prompt.split("\n")
        out = []
        for line in lines:
            stripped = line.strip()
            replaced = False
            for pattern, repl in self._SEPARATOR_SUBS:
                if re.match(pattern, stripped):
                    # Preserve leading whitespace
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(indent + re.sub(pattern, repl, stripped))
                    replaced = True
                    break
            if not replaced:
                out.append(line)
        return "\n".join(out)

    def _truncate_prompt(self, prompt: str) -> str:
        if len(prompt) <= self.MAX_PROMPT_CHARS:
            return prompt

        separators = [
            "\n\n--- NEW REQUEST ---\n",
            "\n\nRespond to the following user request while following that context:\n",
        ]
        for separator in separators:
            if separator in prompt:
                context_part, request_part = prompt.split(separator, 1)
                request_part = request_part.strip()
                request_budget = min(max(len(request_part), 4000), 8000)
                request_budget = min(request_budget, self.MAX_PROMPT_CHARS // 2)
                kept_request = request_part[-request_budget:]
                context_budget = self.MAX_PROMPT_CHARS - len(separator) - len(kept_request) - 64
                kept_context = self._clip_text(context_part, max(context_budget, 1000))
                self.logger.warning(
                    "Codex prompt exceeded safe size; truncated context before CLI execution."
                )
                return (
                    f"{kept_context}\n\n[context trimmed for Codex size]"
                    f"{separator}{kept_request}"
                )

        self.logger.warning("Codex prompt exceeded safe size; keeping tail only before CLI execution.")
        return prompt[-self.MAX_PROMPT_CHARS:]

    def _emit_stream_event(self, event: StreamEvent | None, on_stream_event: StreamCallback) -> None:
        if event is None or on_stream_event is None:
            return
        asyncio.create_task(on_stream_event(event))

    def _summarize_command(self, item: dict) -> str:
        raw_cmd = item.get("command")
        if isinstance(raw_cmd, list):
            cmd = " ".join(str(part) for part in raw_cmd if part)
        else:
            cmd = str(raw_cmd or "").strip()
        cmd = " ".join(cmd.split())
        if not cmd:
            return "Running command"
        return f"Running: {cmd[:100]}"

    def _summarize_file_change(self, item: dict) -> tuple[str, str]:
        changes = item.get("changes")
        if isinstance(changes, list) and changes:
            paths = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = str(change.get("path") or "").strip()
                if path:
                    paths.append(path)
            if paths:
                if len(paths) == 1:
                    return (f"Edited: {paths[0]}", paths[0])
                preview = ", ".join(paths[:2])
                if len(paths) > 2:
                    preview += ", ..."
                return (f"Edited {len(paths)} files: {preview}", paths[0])
        path = str(item.get("file_path") or "").strip() or "unknown"
        return (f"Edited: {path}", path)

    def _flush_pending_agent_message(
        self,
        pending_agent_message: dict | None,
        on_stream_event: StreamCallback,
    ) -> None:
        if on_stream_event is None or not pending_agent_message:
            return
        text = " ".join(str(pending_agent_message.get("text") or "").split())
        if not text:
            return
        self._emit_stream_event(
            StreamEvent(kind=KIND_THINKING, summary=text[:160]),
            on_stream_event,
        )

    def _parse_codex_event(
        self,
        raw_line: str,
        on_stream_event: StreamCallback,
        pending_agent_message: dict | None = None,
    ) -> dict | None:
        """Parse a single Codex JSONL line and emit stream events when possible."""
        if on_stream_event is None:
            return pending_agent_message
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return pending_agent_message

        etype = event.get("type", "")
        item = event.get("item") or {}
        item_type = item.get("type", "")

        if etype == "turn.completed":
            return None

        if pending_agent_message and not (etype == "item.completed" and item_type == "agent_message"):
            # Codex uses `agent_message` for intermediate progress updates as well as
            # the final answer. Hold the latest one until another event arrives; if
            # something follows, it was an interim status update and can be exposed
            # as a thinking trace without duplicating the final response.
            self._flush_pending_agent_message(pending_agent_message, on_stream_event)
            pending_agent_message = None

        se: StreamEvent | None = None

        if etype == "turn.started":
            se = StreamEvent(kind=KIND_PROGRESS, summary="Codex started reasoning")
        elif etype == "item.started" and item_type == "command_execution":
            se = StreamEvent(
                kind=KIND_SHELL_EXEC,
                summary=self._summarize_command(item),
                tool_name="Bash",
            )
        elif etype == "item.completed" and item_type == "command_execution":
            exit_code = item.get("exit_code", "?")
            se = StreamEvent(kind=KIND_TOOL_END, summary=f"Command exited ({exit_code})", tool_name="Bash")
        elif etype == "item.completed" and item_type == "file_change":
            summary, path = self._summarize_file_change(item)
            se = StreamEvent(kind=KIND_FILE_EDIT, summary=summary, file_path=path)
        elif etype == "item.started" and item_type == "todo_list":
            se = StreamEvent(kind=KIND_PROGRESS, summary="Updated task list")
        elif etype == "item.completed" and item_type == "agent_message":
            pending_agent_message = {"text": item.get("text") or ""}

        self._emit_stream_event(se, on_stream_event)
        return pending_agent_message

    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        output_path = self.config.workspace_dir / f".codex_last_{request_id}.txt"
        if output_path.exists():
            output_path.unlink()

        built_prompt = self._sanitize_for_codex(self._truncate_prompt(prompt))
        stdin_data = None
        prompt_arg = built_prompt
        if self._should_use_stdin_transport(built_prompt):
            prompt_arg = "-"
            stdin_data = built_prompt.encode("utf-8")
            self.logger.info(
                f"Prompt for {request_id} requires stdin transport; sending full prompt via stdin."
            )

        cmd = [
            self.cmd_base,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--add-dir",
            self.access_root,
            "--ephemeral",
            "--json",
            "--output-last-message",
            str(output_path),
        ]

        if self.config.model and self.config.model != "default":
            cmd += ["--model", self.config.model]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']

        cmd += ["--", prompt_arg]

        try:
            self.logger.info(
                f"Launching Codex request {request_id} "
                f"(stateless=True, retry={is_retry}, stdin={stdin_data is not None}, "
                f"prompt_len={len(built_prompt)}, cwd={self.config.workspace_dir})"
            )
            _extra_kwargs = {}
            if os.name != "nt":
                _extra_kwargs["start_new_session"] = True
            self.current_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.workspace_dir),
                limit=16 * 1024 * 1024,  # 16 MB readline buffer — Codex embeds full command output in single JSON lines
                **_extra_kwargs,
            )
            # Capture local ref to avoid race with shutdown() nulling self.current_proc
            proc = self.current_proc
            self.logger.info(
                f"Codex subprocess started for {request_id} "
                f"(pid={proc.pid}, cmd={cmd})"
            )
            self._touch_activity()  # mark process launch as initial activity

            stdout_lines = []
            stderr_chunks: list[bytes] = []
            timeout_kind: str | None = None
            pending_agent_message: dict | None = None

            async def _read_stdout():
                nonlocal pending_agent_message
                nonlocal timeout_kind
                while True:
                    try:
                        line = await proc.stdout.readline()
                    except asyncio.LimitOverrunError:
                        # Single JSON event exceeded buffer — drain the oversized line and continue
                        self.logger.warning(
                            f"Codex stdout line exceeded buffer limit for {request_id}; skipping event"
                        )
                        try:
                            await proc.stdout.readuntil(b"\n")
                        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                            # Still too big or stream ended — read remaining buffer
                            await proc.stdout.read(16 * 1024 * 1024)
                        self._touch_activity()
                        continue
                    if not line:
                        break
                    self._touch_activity()
                    decoded = line.decode(errors="replace")
                    stdout_lines.append(decoded)
                    pending_agent_message = self._parse_codex_event(
                        decoded,
                        on_stream_event,
                        pending_agent_message=pending_agent_message,
                    )

            stdout_task = asyncio.create_task(_read_stdout())

            async def _read_stderr():
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    self._touch_activity()
                    stderr_chunks.append(chunk)

            stderr_task = asyncio.create_task(_read_stderr())
            self._active_read_tasks = [stdout_task, stderr_task]
            if stdin_data is not None and proc.stdin is not None:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()

            hard_deadline = started + self.HARD_TIMEOUT_SEC
            while proc.returncode is None and timeout_kind is None:
                remaining_hard = hard_deadline - time.perf_counter()
                if remaining_hard <= 0:
                    timeout_kind = "hard"
                    break
                wait_slice = min(5.0, remaining_hard)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=wait_slice)
                except asyncio.TimeoutError:
                    continue

            if timeout_kind is not None:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                detail = (
                    f"idle for {self.IDLE_TIMEOUT_SEC}s with no output"
                    if timeout_kind == "idle"
                    else f"exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s"
                )
                self.logger.error(
                    f"Codex request {request_id} {timeout_kind}-timed out "
                    f"(pid={proc.pid}, duration_ms={duration_ms}, detail={detail})"
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
                        f"Codex CLI was idle for {self.IDLE_TIMEOUT_SEC}s with no output."
                        if timeout_kind == "idle"
                        else f"Codex CLI exceeded hard timeout of {self.HARD_TIMEOUT_SEC}s."
                    ),
                    is_success=False,
                )

            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            self._active_read_tasks = []
            stderr_data = b"".join(stderr_chunks)
            await proc.wait()
            returncode = proc.returncode
            self.current_proc = None
            duration_ms = round((time.perf_counter() - started) * 1000, 2)

            if stdout_lines:
                with open(self.events_log_path, "a", encoding="utf-8") as f:
                    for raw_line in stdout_lines:
                        f.write(raw_line)

            self.logger.info(
                f"Codex request {request_id} exited "
                f"(returncode={returncode}, duration_ms={duration_ms}, "
                f"stdout_lines={len(stdout_lines)}, stderr_bytes={len(stderr_data)})"
            )

            if returncode != 0:
                err_msg = stderr_data.decode(errors="replace").strip()
                if not err_msg:
                    err_msg = "".join(stdout_lines).strip() or "Codex CLI exited with a non-zero status."
                return BackendResponse(text="", duration_ms=duration_ms, error=err_msg, is_success=False)

            response = ""
            if output_path.exists():
                response = output_path.read_text(encoding="utf-8").strip()
                output_path.unlink(missing_ok=True)

            if not response:
                for raw_line in stdout_lines:
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    item = event.get("item")
                    if event.get("type") == "item.completed" and isinstance(item, dict):
                        if item.get("type") == "agent_message" and item.get("text"):
                            response = item["text"].strip()

            if not response:
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    error="Codex CLI completed without a final assistant message.",
                    is_success=False,
                )

            return BackendResponse(text=response, duration_ms=duration_ms, is_success=True)

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
        finally:
            output_path.unlink(missing_ok=True)

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
