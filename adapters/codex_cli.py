from __future__ import annotations
import os
import json
import time
import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

import adapters.stream_events as stream_event_types
from adapters.base import BaseBackend, BackendCapabilities, BackendResponse, TokenUsage
from adapters.codex_app_server import CodexAppServerToolBridge, disabled_mcp_override
from adapters.stream_io import iter_stream_lines
from adapters.stream_events import (
    StreamCallback, StreamEvent,
    KIND_TOOL_END,
    KIND_FILE_EDIT, KIND_SHELL_EXEC, KIND_PROGRESS,
)
from orchestrator.multimodal_contract import (
    local_fallback_attachment_text,
    MultimodalContractError,
    normalize_request_content,
    request_content_has_media,
    route_request_content,
    routing_decisions_payload,
    validate_authorized_media_references,
)
from adapters.hashi_mcp import prepare_hashi_mcp

_CODEX_REQUEST_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)


class CodexCLIAdapter(BaseBackend):
    LONG_PROMPT_STDIN_THRESHOLD = 24000
    DEFAULT_IDLE_TIMEOUT_SEC = 60 * 60
    POST_TURN_COMPLETION_GRACE_SEC = 15

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=False,
            supports_headless_mode=True,
            supports_commentary_stream=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=False,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Codex.{self.config.name}")
        self.current_proc = None
        self._active_read_tasks: list[asyncio.Task] = []
        self._external_tool_processes: set[object] = set()
        self._external_mcp_server_names: tuple[str, ...] | None = None
        self.effort = ((self.config.extra or {}).get("effort") or "medium").lower()
        self.cmd_base = self.global_config.codex_cmd
        if os.name == "nt" and Path(self.cmd_base).suffix.lower() not in {".cmd", ".exe", ".bat", ".ps1"}:
            self.cmd_base = f"{self.cmd_base}.cmd"
        self.access_root = str(self.config.resolve_access_root())
        self.events_log_path = self.config.workspace_dir / "codex_exec_events.jsonl"
        # Persistent session state
        self._session_id: str | None = None
        self._session_mode: bool = bool((self.config.extra or {}).get("session_mode", False))
        # Real token usage captured from turn.completed events
        self._last_usage: TokenUsage | None = None
        self.tool_registry = None
        self._hashi_mcp_enabled = False
        self._hashi_mcp_descriptor = None

    def _should_use_stdin_transport(self, prompt: str) -> bool:
        if (
            "\n" in prompt
            or "\r" in prompt
            or len(prompt) > self.LONG_PROMPT_STDIN_THRESHOLD
        ):
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
            prepare_hashi_mcp(self, backend="codex-cli")
            return True
        except Exception as e:
            self.logger.error(f"Codex CLI not accessible: {e}")
            return False

    async def _discover_mcp_servers(self) -> tuple[str, ...]:
        """List configured MCP servers so the API bridge can disable all of them."""
        extra_kwargs: dict[str, object] = {}
        if os.name != "nt":
            # force_kill_process_tree() terminates subprocess groups.  The MCP
            # inventory process must never inherit HASHI's own process group.
            extra_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            self.cmd_base,
            "mcp",
            "list",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.effective_workdir),
            **extra_kwargs,
        )
        self._external_tool_processes.add(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason="codex-mcp-isolation-preflight-timeout",
            )
            raise RuntimeError("codex mcp list timed out")
        except asyncio.CancelledError:
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason="codex-mcp-isolation-preflight-cancelled",
            )
            raise
        finally:
            self._external_tool_processes.discard(proc)
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or "codex mcp list failed")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("codex mcp list returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("codex mcp list returned an unexpected payload")
        names = {
            str(item.get("name") or "").strip()
            for item in payload
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        return tuple(sorted(names))

    def supports_external_tool_passthrough(self, model: str | None = None) -> bool:
        """Codex app-server dynamic tools back caller-owned function calls."""
        return bool(str(model or self.config.model or "").strip())

    def supports_structured_conversation(self, model: str | None = None) -> bool:
        """Codex app-server accepts ordered text/image conversation parts."""
        return bool(str(model or self.config.model or "").strip())

    def _request_reasoning_effort(
        self,
        *,
        request_options: Mapping | None = None,
        reasoning_effort: str | None = None,
    ) -> str | None:
        """Resolve one request without mutating the pooled adapter default."""

        configured = reasoning_effort
        if configured is None and isinstance(request_options, Mapping):
            configured = request_options.get("reasoning_effort")
        if configured is None:
            configured = self.effort
        if configured is None:
            return None
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("Codex reasoning_effort must be a non-empty string")
        normalized = configured.strip().casefold()
        if normalized not in _CODEX_REQUEST_REASONING_EFFORTS:
            raise ValueError(
                "Codex reasoning_effort must be one of: "
                + ", ".join(sorted(_CODEX_REQUEST_REASONING_EFFORTS))
            )
        return normalized

    async def generate_external_tool_response(
        self,
        messages: list[dict],
        tools: list[dict],
        request_id: str,
        *,
        tool_choice=None,
        parallel_tool_calls: bool | None = None,
        use_streaming: bool = False,
        request_options: dict | None = None,
        on_stream_event=None,
        model: str | None = None,
    ) -> BackendResponse:
        """Capture one Codex dynamic-tool batch without executing caller tools."""
        selected_effort = self._request_reasoning_effort(
            request_options=request_options,
        )
        try:
            # Refresh on every request so an MCP server added after adapter
            # initialization can never escape the isolated tool-call boundary.
            self._external_mcp_server_names = await self._discover_mcp_servers()
        except Exception as exc:
            return BackendResponse(
                text="",
                duration_ms=0,
                error=(
                    "Codex API tool-call isolation is unavailable because "
                    f"configured MCP servers could not be inventoried: {exc}"
                ),
                is_success=False,
            )

        selected_model = str(model or self.config.model or "").strip()

        async def force_kill(proc, reason: str) -> None:
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason=reason,
            )

        bridge = CodexAppServerToolBridge(
            command=self.cmd_base,
            model=selected_model,
            effort=selected_effort or "medium",
            idle_timeout_sec=self.IDLE_TIMEOUT_SEC,
            disabled_mcp_servers=self._external_mcp_server_names,
            logger=self.logger,
            on_process_started=self._external_tool_processes.add,
            on_process_stopped=self._external_tool_processes.discard,
            force_kill=force_kill,
        )
        return await bridge.run(
            messages,
            tools,
            request_id,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            use_streaming=use_streaming,
            on_stream_event=on_stream_event,
        )

    async def generate_structured_response(
        self,
        messages: list[dict],
        request_id: str,
        *,
        use_streaming: bool = False,
        request_options: dict | None = None,
        on_stream_event=None,
        model: str | None = None,
    ) -> BackendResponse:
        """Run multipart conversation input without entering caller-tool mode."""

        return await self.generate_external_tool_response(
            messages,
            [],
            request_id,
            tool_choice="none",
            parallel_tool_calls=False,
            use_streaming=use_streaming,
            request_options=request_options,
            on_stream_event=on_stream_event,
            model=model,
        )

    async def handle_new_session(self) -> bool:
        """Clear session ID so the next request starts a fresh codex session."""
        old_id = self._session_id
        self._session_id = None
        if old_id:
            self.logger.info(f"Codex session cleared (was {old_id[:8]}…). Next request starts fresh.")
        else:
            self.logger.info("Codex handle_new_session: no active session, nothing to clear.")
        return True

    def set_session_mode(self, enabled: bool) -> None:
        self._session_mode = bool(enabled)
        self._session_id = None
        self.logger.info("Session mode set to %s", "ON" if enabled else "OFF")

    def should_bootstrap_on_startup(self) -> bool:
        return False

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

    def _emit_stream_event(self, event: StreamEvent | None, on_stream_event: StreamCallback) -> None:
        if event is None or on_stream_event is None:
            return
        asyncio.create_task(on_stream_event(event))

    def _command_text(self, item: dict) -> str:
        raw_cmd = item.get("command")
        if isinstance(raw_cmd, list):
            cmd = " ".join(str(part) for part in raw_cmd if part)
        else:
            cmd = str(raw_cmd or "").strip()
        return " ".join(cmd.split())

    def _summarize_command(self, item: dict) -> str:
        cmd = self._command_text(item)
        if not cmd:
            return "Running command"
        return f"Running: {cmd[:100]}"

    def _summarize_file_change(self, item: dict) -> tuple[str, str, tuple[str, ...]]:
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
                    return (f"Edited: {paths[0]}", paths[0], tuple(paths))
                preview = ", ".join(paths[:2])
                if len(paths) > 2:
                    preview += ", ..."
                return (
                    f"Edited {len(paths)} files: {preview}",
                    paths[0],
                    tuple(paths),
                )
        path = str(item.get("file_path") or "").strip() or "unknown"
        paths = () if path == "unknown" else (path,)
        return (f"Edited: {path}", path, paths)

    def _flush_pending_agent_message(
        self,
        pending_agent_message: dict | None,
        on_stream_event: StreamCallback,
    ) -> None:
        if on_stream_event is None or not pending_agent_message:
            return
        text = str(pending_agent_message.get("text") or "").strip()
        if not text:
            return
        self._emit_stream_event(
            # Resolve through the module at emission time so a running bridge
            # can load a newly introduced event kind in the same hot restart.
            StreamEvent(kind=stream_event_types.KIND_COMMENTARY, summary=text),
            on_stream_event,
        )

    def _parse_codex_event(
        self,
        raw_line: str,
        on_stream_event: StreamCallback,
        pending_agent_message: dict | None = None,
    ) -> dict | None:
        """Parse a single Codex JSONL line and emit stream events when possible."""
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return pending_agent_message

        etype = event.get("type", "")
        item = event.get("item") or {}
        item_type = item.get("type", "")

        if etype == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._last_usage = TokenUsage(
                    input_tokens=usage.get("input_tokens", 0) or 0,
                    output_tokens=usage.get("output_tokens", 0) or 0,
                    thinking_tokens=(
                        usage.get("reasoning_output_tokens")
                        or usage.get("reasoning_tokens")
                        or 0
                    ),
                )
            return None

        if pending_agent_message:
            # Codex uses `agent_message` for intermediate progress updates as well as
            # the final answer. Hold the latest one until another event arrives; if
            # anything except `turn.completed` follows, it was model-authored interim
            # commentary. `turn.completed` is handled above so the held final answer
            # is not duplicated in the thinking channel.
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
                metadata={"command": self._command_text(item)},
            )
        elif etype == "item.completed" and item_type == "command_execution":
            exit_code = item.get("exit_code", "?")
            se = StreamEvent(
                kind=KIND_TOOL_END,
                summary=f"Command exited ({exit_code})",
                tool_name="Bash",
                metadata={
                    "command": self._command_text(item),
                    "exit_code": exit_code,
                },
            )
        elif etype == "item.completed" and item_type == "file_change":
            summary, path, paths = self._summarize_file_change(item)
            se = StreamEvent(
                kind=KIND_FILE_EDIT,
                summary=summary,
                file_path=path,
                metadata={"file_paths": paths},
            )
        elif etype == "item.started" and item_type == "todo_list":
            se = StreamEvent(kind=KIND_PROGRESS, summary="Updated task list")
        elif etype == "item.completed" and item_type == "agent_message":
            pending_agent_message = {"text": item.get("text") or ""}

        self._emit_stream_event(se, on_stream_event)
        return pending_agent_message

    def _build_cmd(
        self,
        prompt_arg: str,
        output_path: Path,
        *,
        reasoning_effort: str | None = None,
        image_paths: tuple[Path, ...] = (),
    ) -> list[str]:
        """Build the codex exec command. Uses 'resume' sub-command if a session exists.

        Note: 'codex exec resume' supports fewer flags than 'codex exec' — notably
        it does NOT support --add-dir (the session already has the access root from
        the original exec call that created it).
        """
        base_flags = [
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--output-last-message", str(output_path),
        ]
        if self.config.model and self.config.model != "default":
            base_flags += ["--model", self.config.model]
        selected_effort = self._request_reasoning_effort(
            reasoning_effort=reasoning_effort,
        )
        if selected_effort:
            base_flags += ["-c", f'model_reasoning_effort="{selected_effort}"']
        for image_path in image_paths:
            base_flags += ["--image", str(image_path)]
        descriptor = self._hashi_mcp_descriptor if self._hashi_mcp_enabled else None
        if descriptor:
            # A Fixed CLI request must expose exactly the request-scoped HASHI
            # Gateway plus Codex's ordinary local coding surface. Disable every
            # MCP server visible through user or project config before enabling
            # our gateway, and turn off other optional external tool surfaces.
            for feature in (
                "apps",
                "plugins",
                "multi_agent",
                "browser_use",
                "computer_use",
                "image_generation",
                "hooks",
            ):
                base_flags += ["--disable", feature]
            base_flags += ["-c", 'web_search="disabled"']
            for server_name in self._external_mcp_server_names or ():
                base_flags += ["-c", disabled_mcp_override(server_name)]
            command_value = json.dumps(str(descriptor["command"]), ensure_ascii=False)
            args_value = json.dumps(list(descriptor["args"]), ensure_ascii=False)
            cwd_value = json.dumps(str(descriptor["cwd"]), ensure_ascii=False)
            server_name = str(descriptor["name"])
            mcp_value = (
                f"mcp_servers.{server_name}={{command={command_value},"
                f"args={args_value},cwd={cwd_value},enabled=true}}"
            )
            base_flags += [
                "-c",
                mcp_value,
            ]

        if self._session_mode and self._session_id:
            # Resume existing session — access root already set in session, no --add-dir needed
            cmd = [self.cmd_base, "exec", "resume", self._session_id] + base_flags
        else:
            # First turn: start a new persistent session (no --ephemeral), include --add-dir
            cmd = [self.cmd_base, "exec", "--add-dir", self.effective_add_dir] + base_flags

        cmd += ["--", prompt_arg]
        return cmd

    def _extract_response_text(self, output_path: Path, stdout_lines: list[str]) -> str:
        response = ""
        if output_path.exists():
            response = output_path.read_text(encoding="utf-8").strip()
            output_path.unlink(missing_ok=True)

        if response:
            return response

        for raw_line in stdout_lines:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict):
                if item.get("type") == "agent_message" and item.get("text"):
                    response = item["text"].strip()
        return response

    def _persist_stdout_lines(self, stdout_lines: list[str]) -> None:
        if not stdout_lines:
            return
        with open(self.events_log_path, "a", encoding="utf-8") as f:
            for raw_line in stdout_lines:
                f.write(raw_line)
                if not raw_line.endswith("\n"):
                    f.write("\n")

    def _error_with_last_message(self, prefix: str, response: str) -> str:
        response = (response or "").strip()
        if not response:
            return prefix
        return f"{prefix}\n\nLast Codex message before exit:\n{response}"

    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
        reasoning_effort: str | None = None,
        request_content: Mapping | None = None,
    ) -> BackendResponse:
        # Reset per-request usage tracking
        self._last_usage = None

        try:
            normalized_request_content = normalize_request_content(request_content)
        except MultimodalContractError as exc:
            return BackendResponse(
                text="",
                duration_ms=0,
                error=str(exc),
                is_success=False,
                error_code=exc.code,
                error_retryable=False,
                stream_metadata={"attachment_id": exc.attachment_id or None},
            )
        media_routing: tuple[dict, ...] = ()
        native_image_paths: tuple[Path, ...] = ()
        native_image_refs: tuple[str, ...] = ()
        local_fallback_descriptors: tuple[str, ...] = ()

        def with_media_metadata(response: BackendResponse) -> BackendResponse:
            if media_routing:
                metadata = dict(response.stream_metadata or {})
                metadata["multimodal_routing"] = list(media_routing)
                response.stream_metadata = metadata
            return response

        if request_content_has_media(normalized_request_content):
            capability = self.resolve_input_capability()
            try:
                resolved_paths = validate_authorized_media_references(
                    normalized_request_content,
                    authorized_roots=self.authorized_media_roots(),
                )
            except MultimodalContractError as exc:
                return BackendResponse(
                    text="",
                    duration_ms=0,
                    error=str(exc),
                    is_success=False,
                    error_code=exc.code,
                    error_retryable=False,
                    stream_metadata={"attachment_id": exc.attachment_id or None},
                )
            decisions = route_request_content(
                normalized_request_content,
                capability,
                # Codex's established local CLI path can read ordinary
                # documents named in the prompt.  This compatibility signal is
                # intentionally limited to documents and does not grant native
                # audio/video understanding.
                fallback_modalities={"document"},
                transport_preferences={"image": ("local_path",)},
            )
            media_routing = routing_decisions_payload(decisions)
            unsupported = [item for item in decisions if item.route == "unsupported"]
            if unsupported:
                first = unsupported[0]
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=0,
                        error=(
                            f"{capability.provider}/{capability.model} cannot consume "
                            f"{first.modality} attachment {first.attachment_id!r}"
                        ),
                        is_success=False,
                        error_code=(
                            "MEDIA_LIMIT_EXCEEDED"
                            if "limit_exceeded" in first.reason
                            else "PROVIDER_MODALITY_UNSUPPORTED"
                        ),
                        error_retryable=False,
                        stream_metadata={"attachment_id": first.attachment_id},
                    )
                )
            native_image_paths = tuple(
                resolved_paths[item.attachment_id]
                for item in decisions
                if item.route == "native" and item.modality == "image"
            )
            native_ids = {
                item.attachment_id
                for item in decisions
                if item.route == "native" and item.modality == "image"
            }
            native_image_refs = tuple(
                str(part.get("local_ref") or "")
                for part in normalized_request_content["parts"]
                if part.get("type") == "media"
                and str(part.get("attachment_id") or "") in native_ids
                and str(part.get("local_ref") or "")
            )
            fallback_ids = {
                item.attachment_id
                for item in decisions
                if item.route == "local_fallback"
            }
            local_fallback_descriptors = tuple(
                local_fallback_attachment_text(part)
                for part in normalized_request_content["parts"]
                if part.get("type") == "media"
                and str(part.get("attachment_id") or "") in fallback_ids
            )
            if any(
                item.route == "native" and item.modality != "image"
                for item in decisions
            ):
                first = next(
                    item
                    for item in decisions
                    if item.route == "native" and item.modality != "image"
                )
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=0,
                        error=(
                            "Codex CLI has no registered native command transport for "
                            f"{first.modality} attachment {first.attachment_id!r}"
                        ),
                        is_success=False,
                        error_code="MEDIA_TRANSPORT_UNSUPPORTED",
                        error_retryable=False,
                        stream_metadata={"attachment_id": first.attachment_id},
                    )
                )

        started = time.perf_counter()
        output_path = self.config.workspace_dir / f".codex_last_{request_id}.txt"
        if output_path.exists():
            output_path.unlink()

        # Prompt size selects a transport, never a content ceiling. Long input
        # is streamed over stdin unchanged on Linux and Windows.
        prompt_for_codex = str(prompt or "")
        for local_ref in native_image_refs:
            replacement = "[image supplied through Codex native attachment input]"
            prompt_for_codex = prompt_for_codex.replace(local_ref, replacement)
            # JSON receipts escape Windows separators.  Scrub that spelling as
            # well so the model cannot take a second local-tool route merely
            # because the same native image path appeared in context.
            escaped_ref = json.dumps(local_ref, ensure_ascii=False)[1:-1]
            if escaped_ref != local_ref:
                prompt_for_codex = prompt_for_codex.replace(
                    escaped_ref,
                    replacement,
                )
        if local_fallback_descriptors:
            prompt_for_codex = "\n\n".join(
                [prompt_for_codex, *local_fallback_descriptors]
            )
        built_prompt = self._sanitize_for_codex(prompt_for_codex)
        stdin_data = None
        prompt_arg = built_prompt
        if self._should_use_stdin_transport(built_prompt):
            prompt_arg = "-"
            stdin_data = built_prompt.encode("utf-8")
            self.logger.info(
                f"Prompt for {request_id} requires stdin transport; sending full prompt via stdin."
            )

        if self._hashi_mcp_enabled:
            try:
                # Refresh for every invocation so an MCP server added after
                # adapter initialization cannot escape the request boundary.
                self._external_mcp_server_names = await self._discover_mcp_servers()
            except Exception as exc:
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        error=(
                            "Codex Fixed Tool Gateway isolation is unavailable because "
                            f"configured MCP servers could not be inventoried: {exc}"
                        ),
                        is_success=False,
                    )
                )

        cmd = self._build_cmd(
            prompt_arg,
            output_path,
            reasoning_effort=reasoning_effort,
            image_paths=native_image_paths,
        )
        session_mode = "resume" if self._session_id else "new"
        effective_workdir = self.effective_workdir
        stdout_lines: list[str] = []

        try:
            self.logger.info(
                f"Launching Codex request {request_id} "
                f"(session={session_mode}, session_id={self._session_id or 'none'}, "
                f"retry={is_retry}, stdin={stdin_data is not None}, "
                f"prompt_len={len(built_prompt)}, cwd={effective_workdir})"
            )
            _extra_kwargs = {}
            if os.name != "nt":
                _extra_kwargs["start_new_session"] = True
            self.current_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(effective_workdir),
                **_extra_kwargs,
            )
            # Capture local ref to avoid race with shutdown() nulling self.current_proc
            proc = self.current_proc
            self.logger.info(
                f"Codex subprocess started for {request_id} "
                f"(pid={proc.pid}, cmd={cmd})"
            )
            self._touch_activity()  # mark process launch as initial activity

            stderr_chunks: list[bytes] = []
            timeout_kind: str | None = None
            pending_agent_message: dict | None = None
            captured_thread_id: str | None = None
            turn_completed_at: float | None = None
            forced_completion = False

            async def _read_stdout():
                nonlocal pending_agent_message
                nonlocal timeout_kind
                nonlocal captured_thread_id
                nonlocal turn_completed_at
                async for line in iter_stream_lines(proc.stdout):
                    self._touch_activity()
                    decoded = line.decode(errors="replace")
                    stdout_lines.append(decoded)
                    try:
                        event = json.loads(decoded)
                    except json.JSONDecodeError:
                        event = None
                    if event is not None:
                        if captured_thread_id is None and event.get("type") == "thread.started" and event.get("thread_id"):
                            captured_thread_id = event["thread_id"]
                        if event.get("type") == "turn.completed":
                            turn_completed_at = time.perf_counter()
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

            while proc.returncode is None and timeout_kind is None:
                now_perf = time.perf_counter()
                idle_for = self._last_activity_age()
                if idle_for >= self.IDLE_TIMEOUT_SEC:
                    timeout_kind = "idle"
                    break
                wait_slice = min(
                    5.0,
                    max(0.1, self.IDLE_TIMEOUT_SEC - idle_for),
                )
                if turn_completed_at is not None:
                    remaining_turn = (turn_completed_at + self.POST_TURN_COMPLETION_GRACE_SEC) - now_perf
                    if remaining_turn <= 0:
                        forced_completion = True
                        break
                    wait_slice = min(wait_slice, max(0.1, remaining_turn))
                try:
                    await asyncio.wait_for(proc.wait(), timeout=wait_slice)
                except asyncio.TimeoutError:
                    continue

            if timeout_kind is not None:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                diagnostic = self._timeout_diagnostic(
                    timeout_kind,
                    started_monotonic=started,
                )
                self.logger.error(
                    f"Codex request {request_id} {timeout_kind}-timed out "
                    f"(pid={proc.pid}, duration_ms={duration_ms}, {diagnostic})"
                )
                await self.force_kill_process_tree(
                    proc, logger=self.logger,
                    reason=f"{timeout_kind}-timeout:{request_id}",
                )
                self.current_proc = None
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                self._active_read_tasks = []
                self._persist_stdout_lines(stdout_lines)
                response = self._extract_response_text(output_path, stdout_lines)
                error_text = (
                    f"Codex CLI was idle for {self.IDLE_TIMEOUT_SEC}s with no output."
                )
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=duration_ms,
                        error=self._error_with_last_message(error_text, response),
                        is_success=False,
                    )
                )

            if forced_completion and proc.returncode is None:
                self.logger.warning(
                    f"Codex request {request_id} produced turn.completed but the subprocess "
                    f"did not exit within {self.POST_TURN_COMPLETION_GRACE_SEC}s; forcing shutdown."
                )
                await self.force_kill_process_tree(
                    proc,
                    logger=self.logger,
                    reason=f"turn-completed-grace-expired:{request_id}",
                )

            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            self._active_read_tasks = []
            stderr_data = b"".join(stderr_chunks)
            if proc.returncode is None:
                await proc.wait()
            returncode = proc.returncode
            self.current_proc = None
            duration_ms = round((time.perf_counter() - started) * 1000, 2)

            self._persist_stdout_lines(stdout_lines)

            self.logger.info(
                f"Codex request {request_id} exited "
                f"(returncode={returncode}, duration_ms={duration_ms}, "
                f"stdout_lines={len(stdout_lines)}, stderr_bytes={len(stderr_data)})"
            )

            response = self._extract_response_text(output_path, stdout_lines)

            if returncode != 0 and not forced_completion:
                err_msg = stderr_data.decode(errors="replace").strip()
                if not err_msg:
                    err_msg = "Codex CLI exited with a non-zero status."
                err_msg = self._error_with_last_message(err_msg, response)
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=duration_ms,
                        error=err_msg,
                        is_success=False,
                    )
                )

            # Persist the session ID from this turn so the next request can resume it
            if self._session_mode and captured_thread_id and captured_thread_id != self._session_id:
                self.logger.info(
                    f"Codex session established: {captured_thread_id} "
                    f"(was: {self._session_id or 'none'})"
                )
                self._session_id = captured_thread_id
            elif not self._session_mode:
                self._session_id = None

            if not response:
                return with_media_metadata(
                    BackendResponse(
                        text="",
                        duration_ms=duration_ms,
                        error="Codex CLI completed without a final assistant message.",
                        is_success=False,
                    )
                )

            return with_media_metadata(
                BackendResponse(
                    text=response,
                    duration_ms=duration_ms,
                    is_success=True,
                    usage=self._last_usage,
                )
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Generation cancelled for {request_id}")
            if self.current_proc:
                await self.force_kill_process_tree(
                    self.current_proc,
                    logger=self.logger,
                    reason=f"cancelled:{request_id}",
                )
            self._persist_stdout_lines(stdout_lines)
            raise
        except Exception as e:
            return with_media_metadata(
                BackendResponse(
                    text="",
                    duration_ms=0,
                    error=str(e),
                    is_success=False,
                )
            )
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
        for proc in list(self._external_tool_processes):
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason="backend_shutdown_external_tool_bridge",
            )
            self._external_tool_processes.discard(proc)
        for task in self._active_read_tasks:
            if not task.done():
                task.cancel()
        self._active_read_tasks = []
