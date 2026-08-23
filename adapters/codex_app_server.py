from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from adapters.base import BackendResponse, TokenUsage
from adapters.stream_events import KIND_TEXT_DELTA, StreamCallback, StreamEvent

_APP_SERVER_READ_LIMIT = 8 * 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_DEFERRED_TOOL_RESULT = (
    "HASHI_EXTERNAL_TOOL_DEFERRED: the OpenAI-compatible API caller owns this "
    "tool and will provide its real result in the next request. Do not retry "
    "the call or answer from this placeholder."
)
_BASE_INSTRUCTIONS = """You are serving one OpenAI-compatible Chat Completions turn.
Answer from the supplied conversation. The host may provide client-executed dynamic
function tools. Their internal names are aliases; each description identifies the
caller-visible function name that user messages may reference. Never execute those
functions yourself and never substitute local shell, filesystem, web, app, MCP,
skill, or multi-agent tools for them. If a dynamic function is needed, invoke it
with valid JSON arguments and wait for its result."""
_CONTINUE_AFTER_TOOL_RESULTS = (
    "Continue the conversation using the supplied function-call results."
)
_LOCAL_TOOL_ITEM_TYPES = frozenset(
    {
        "collabToolCall",
        "commandExecution",
        "fileChange",
        "imageView",
        "mcpToolCall",
        "webSearch",
    }
)
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CODEX_DYNAMIC_TOOL_PREFIX = "hashi_ext_"
_CODEX_DYNAMIC_TOOL_MAX_LENGTH = 64
_CODEX_DYNAMIC_TOOL_DIGEST_LENGTH = 16
_CODEX_DYNAMIC_TOOL_STEM_RE = re.compile(r"[^A-Za-z0-9_-]")


class CodexAppServerError(RuntimeError):
    """Raised when Codex app-server cannot satisfy the external-tool contract."""


@dataclass(frozen=True)
class CodexConversation:
    developer_instructions: str
    history_items: list[dict[str, Any]]
    turn_input: list[dict[str, Any]]


ProcessCallback = Callable[[Any], None]
KillCallback = Callable[[Any, str], Awaitable[None]]


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"kind": "text", "text": content}]
    if not isinstance(content, list):
        return [{"kind": "text", "text": str(content)}]

    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if text is not None:
                parts.append({"kind": "text", "text": str(text)})
            continue
        if part_type in {"image_url", "input_image"}:
            image = part.get("image_url")
            detail = part.get("detail")
            if isinstance(image, dict):
                detail = image.get("detail", detail)
                image = image.get("url")
            if image:
                normalized = {"kind": "image", "url": str(image)}
                if detail in {"auto", "low", "high", "original"}:
                    normalized["detail"] = detail
                parts.append(normalized)
    return parts


def _content_text(content: Any) -> str:
    return "\n".join(
        str(part["text"])
        for part in _content_parts(content)
        if part.get("kind") == "text" and str(part.get("text") or "")
    )


def _history_message_content(content: Any, *, assistant: bool) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for part in _content_parts(content):
        if part["kind"] == "text":
            converted.append(
                {
                    "type": "output_text" if assistant else "input_text",
                    "text": part["text"],
                }
            )
        elif not assistant:
            item: dict[str, Any] = {
                "type": "input_image",
                "image_url": part["url"],
            }
            if part.get("detail"):
                item["detail"] = part["detail"]
            converted.append(item)
    return converted


def _turn_input(content: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for part in _content_parts(content):
        if part["kind"] == "text":
            converted.append({"type": "text", "text": part["text"]})
        else:
            item: dict[str, Any] = {"type": "image", "url": part["url"]}
            if part.get("detail"):
                item["detail"] = part["detail"]
            converted.append(item)
    return converted


def _tool_output(content: Any) -> str | list[dict[str, Any]]:
    parts = _content_parts(content)
    if not parts:
        return ""
    if all(part["kind"] == "text" for part in parts):
        return "\n".join(str(part["text"]) for part in parts)

    converted: list[dict[str, Any]] = []
    for part in parts:
        if part["kind"] == "text":
            converted.append({"type": "input_text", "text": part["text"]})
        else:
            item: dict[str, Any] = {
                "type": "input_image",
                "image_url": part["url"],
            }
            if part.get("detail"):
                item["detail"] = part["detail"]
            converted.append(item)
    return converted


def codex_dynamic_tool_name(name: str) -> str:
    """Return a deterministic caller-tool alias outside Codex's built-in names."""
    original = str(name)
    stem = _CODEX_DYNAMIC_TOOL_STEM_RE.sub("_", original) or "tool"
    direct_name = f"{_CODEX_DYNAMIC_TOOL_PREFIX}{stem}"
    if stem == original and len(direct_name) <= _CODEX_DYNAMIC_TOOL_MAX_LENGTH:
        return direct_name
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[
        :_CODEX_DYNAMIC_TOOL_DIGEST_LENGTH
    ]
    stem_length = (
        _CODEX_DYNAMIC_TOOL_MAX_LENGTH
        - len(_CODEX_DYNAMIC_TOOL_PREFIX)
        - len(digest)
        - 1
    )
    return f"{_CODEX_DYNAMIC_TOOL_PREFIX}{stem[:stem_length]}_{digest}"


def _codex_tool_name_maps(
    tools: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    original_to_codex: dict[str, str] = {}
    codex_to_original: dict[str, str] = {}
    for tool in tools:
        function = tool.get("function") or {}
        original_name = str(function.get("name") or "")
        if not original_name:
            raise CodexAppServerError("caller-owned tool is missing a function name")
        if original_name in original_to_codex:
            raise CodexAppServerError(
                f"duplicate caller-owned tool name {original_name!r}"
            )
        codex_name = codex_dynamic_tool_name(original_name)
        colliding_name = codex_to_original.get(codex_name)
        if colliding_name is not None:
            raise CodexAppServerError(
                "caller-owned tool aliases collided for "
                f"{colliding_name!r} and {original_name!r}"
            )
        original_to_codex[original_name] = codex_name
        codex_to_original[codex_name] = original_name
    return original_to_codex, codex_to_original


def openai_tools_to_codex_dynamic_tools(
    tools: list[dict[str, Any]],
    *,
    tool_name_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if tool_name_map is None:
        tool_name_map, _ = _codex_tool_name_maps(tools)
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") or {}
        original_name = str(function.get("name") or "")
        description = str(function.get("description") or "")
        caller_name_note = f"Caller-visible function name: {original_name}."
        if description:
            description = f"{caller_name_note} {description}"
        else:
            description = caller_name_note
        converted.append(
            {
                "type": "function",
                "name": tool_name_map[original_name],
                "description": description,
                "inputSchema": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return converted


def _tool_choice_name(tool_choice: Any) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return None
    return str(function.get("name") or "").strip() or None


def _tool_policy_instructions(
    tools: list[dict[str, Any]],
    *,
    tool_choice: Any,
    parallel_tool_calls: bool | None,
    tool_name_map: Mapping[str, str],
) -> str:
    lines = [
        "HOST TOOL BOUNDARY: Only the dynamic functions supplied by the host are "
        "callable for this API turn. Never call or emulate any other tool.",
        "Dynamic function names are internal aliases. Match caller-visible names "
        "in messages to the 'Caller-visible function name' in each description.",
        "When a dynamic function returns HASHI_EXTERNAL_TOOL_DEFERRED, do not "
        "retry it or produce a final answer; the host will end this turn and the "
        "API caller will resume with the real tool result.",
    ]
    if tools:
        lines.append(
            "CALLER TOOL NAME MAP (caller-visible name -> dynamic function alias):"
        )
        for tool in tools:
            function = tool.get("function") or {}
            original_name = str(function.get("name") or "")
            lines.append(
                f"- {original_name!r} -> {tool_name_map[original_name]!r}"
            )
    if not tools or tool_choice == "none":
        lines.append("No dynamic function may be called on this turn.")
    elif tool_choice == "required":
        lines.append("You must call at least one supplied dynamic function before answering.")
    elif (name := _tool_choice_name(tool_choice)) is not None:
        codex_name = tool_name_map.get(name, codex_dynamic_tool_name(name))
        lines.append(
            f"You must call the dynamic function named {codex_name!r} before answering."
        )
    if parallel_tool_calls is False:
        lines.append("Make at most one dynamic function call in this turn.")
    elif parallel_tool_calls is True:
        lines.append("Independent dynamic function calls may be requested together.")
    return "\n".join(lines)


def openai_messages_to_codex_conversation(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    tool_choice: Any = None,
    parallel_tool_calls: bool | None = None,
    tool_name_map: Mapping[str, str] | None = None,
) -> CodexConversation:
    if tool_name_map is None:
        tool_name_map, _ = _codex_tool_name_maps(tools)
    developer_parts: list[str] = []
    history_items: list[dict[str, Any]] = []

    final_user_index: int | None = None
    if messages and str(messages[-1].get("role") or "").lower() == "user":
        final_user_index = len(messages) - 1

    for index, message in enumerate(messages):
        role = str(message.get("role") or "").lower()
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                developer_parts.append(f"CALLER {role.upper()} MESSAGE:\n{text}")
            continue
        if index == final_user_index:
            continue
        if role == "user":
            converted = _history_message_content(content, assistant=False)
            if converted:
                history_items.append(
                    {"type": "message", "role": "user", "content": converted}
                )
            continue
        if role == "assistant":
            converted = _history_message_content(content, assistant=True)
            if converted:
                history_items.append(
                    {"type": "message", "role": "assistant", "content": converted}
                )
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                original_name = str(function.get("name") or "")
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(
                        arguments, ensure_ascii=False, separators=(",", ":")
                    )
                history_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(tool_call.get("id") or ""),
                        "name": tool_name_map.get(
                            original_name,
                            codex_dynamic_tool_name(original_name),
                        ),
                        "arguments": arguments,
                    }
                )
            continue
        if role == "tool":
            history_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": _tool_output(content),
                }
            )
            continue
        raise ValueError(f"unsupported message role for Codex tool bridge: {role!r}")

    if final_user_index is not None:
        turn_input = _turn_input(messages[final_user_index].get("content"))
    else:
        turn_input = [{"type": "text", "text": _CONTINUE_AFTER_TOOL_RESULTS}]
    if not turn_input:
        turn_input = [{"type": "text", "text": _CONTINUE_AFTER_TOOL_RESULTS}]

    developer_parts.append(
        _tool_policy_instructions(
            tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            tool_name_map=tool_name_map,
        )
    )
    return CodexConversation(
        developer_instructions="\n\n".join(developer_parts),
        history_items=history_items,
        turn_input=turn_input,
    )


def _selected_tools(
    tools: list[dict[str, Any]],
    tool_choice: Any,
) -> list[dict[str, Any]]:
    if tool_choice == "none":
        return []
    selected_name = _tool_choice_name(tool_choice)
    if selected_name is None:
        return tools
    return [
        tool
        for tool in tools
        if str((tool.get("function") or {}).get("name") or "") == selected_name
    ]


def _toml_key_segment(value: str) -> str:
    if not _TOML_BARE_KEY_RE.fullmatch(value):
        raise CodexAppServerError(
            f"configured MCP server name {value!r} cannot be safely overridden"
        )
    return value


def _disabled_mcp_override(server_name: str) -> str:
    # Codex CLI config overrides replace an MCP table rather than deep-merging
    # it. Supply a complete, inert transport so the replacement remains valid
    # while carrying no configured endpoint, command, headers, or credentials.
    key = _toml_key_segment(server_name)
    return (
        f'mcp_servers.{key}={{url="http://127.0.0.1/",enabled=false}}'
    )


class CodexAppServerToolBridge:
    """Run one isolated Codex app-server turn and capture caller-owned tools."""

    def __init__(
        self,
        *,
        command: str,
        model: str,
        effort: str,
        idle_timeout_sec: int,
        disabled_mcp_servers: tuple[str, ...] = (),
        logger: logging.Logger | None = None,
        on_process_started: ProcessCallback | None = None,
        on_process_stopped: ProcessCallback | None = None,
        force_kill: KillCallback | None = None,
    ):
        self.command = command
        self.model = model
        self.effort = effort
        self.idle_timeout_sec = max(1, int(idle_timeout_sec))
        self.disabled_mcp_servers = disabled_mcp_servers
        self.logger = logger or logging.getLogger("Backend.Codex.AppServer")
        self.on_process_started = on_process_started
        self.on_process_stopped = on_process_stopped
        self.force_kill = force_kill

    def _command(self) -> list[str]:
        command = [
            self.command,
            "app-server",
            "--stdio",
            "--disable",
            "shell_tool",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "multi_agent",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--disable",
            "hooks",
            "-c",
            'web_search="disabled"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            'history.persistence="none"',
        ]
        for server_name in self.disabled_mcp_servers:
            command.extend(
                [
                    "-c",
                    _disabled_mcp_override(server_name),
                ]
            )
        return command

    async def _send(self, proc: Any, payload: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is unavailable")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        proc.stdin.write((data + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _read(self, proc: Any) -> dict[str, Any]:
        if proc.stdout is None:
            raise CodexAppServerError("Codex app-server stdout is unavailable")
        try:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=self.idle_timeout_sec
            )
        except asyncio.TimeoutError as exc:
            raise CodexAppServerError(
                f"Codex app-server was idle for {self.idle_timeout_sec}s"
            ) from exc
        if not line:
            raise CodexAppServerError("Codex app-server exited before completing the turn")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError("Codex app-server emitted invalid JSONL") from exc
        if not isinstance(message, dict):
            raise CodexAppServerError("Codex app-server emitted a non-object message")
        return message

    async def _request(
        self,
        proc: Any,
        request_id: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        await self._send(
            proc,
            {"method": method, "id": request_id, "params": params},
        )
        while True:
            message = await self._read(proc)
            if message.get("id") != request_id:
                if message.get("id") is not None and message.get("method"):
                    await self._send(
                        proc,
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "request unavailable during HASHI setup",
                            },
                        },
                    )
                continue
            if message.get("error"):
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else error
                raise CodexAppServerError(f"Codex {method} failed: {detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def _read_stderr(self, proc: Any, chunks: list[bytes]) -> None:
        if proc.stderr is None:
            return
        size = 0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            chunks.append(chunk)
            size += len(chunk)
            while size > _STDERR_LIMIT and chunks:
                size -= len(chunks.pop(0))

    async def _stop_process(self, proc: Any, *, reason: str) -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if self.force_kill is not None:
            await self.force_kill(proc, reason)
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    @staticmethod
    def _usage(message: dict[str, Any]) -> TokenUsage | None:
        params = message.get("params") or {}
        token_usage = params.get("tokenUsage") or {}
        total = token_usage.get("total") or token_usage.get("last") or {}
        if not isinstance(total, dict):
            return None
        input_tokens = int(total.get("inputTokens") or 0)
        output_tokens = int(total.get("outputTokens") or 0)
        thinking_tokens = int(total.get("reasoningOutputTokens") or 0)
        if not (input_tokens or output_tokens or thinking_tokens):
            return None
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
        )

    async def run(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        request_id: str,
        *,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        use_streaming: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        selected_tools = _selected_tools(tools, tool_choice)
        original_to_codex, codex_to_original = _codex_tool_name_maps(
            selected_tools
        )
        conversation = openai_messages_to_codex_conversation(
            messages,
            tools=selected_tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            tool_name_map=original_to_codex,
        )
        dynamic_tools = openai_tools_to_codex_dynamic_tools(
            selected_tools,
            tool_name_map=original_to_codex,
        )

        proc = None
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        stderr_task: asyncio.Task | None = None
        stderr_chunks: list[bytes] = []
        turn_id: str | None = None
        thread_id: str | None = None
        interrupt_sent = False
        external_calls: list[dict[str, Any]] = []
        external_call_ids: set[str] = set()
        completed_call_ids: set[str] = set()
        final_text = ""
        usage: TokenUsage | None = None
        protocol_error: str | None = None

        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="hashi-codex-api-tools-")
            # Keep the existing scoped block, but defer directory deletion until
            # after app-server exits. Windows cannot remove a live process cwd.
            with contextlib.nullcontext(temp_dir.name) as cwd:
                extra_kwargs: dict[str, Any] = {"limit": _APP_SERVER_READ_LIMIT}
                if os.name != "nt":
                    extra_kwargs["start_new_session"] = True
                proc = await asyncio.create_subprocess_exec(
                    *self._command(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    **extra_kwargs,
                )
                if self.on_process_started is not None:
                    self.on_process_started(proc)
                stderr_task = asyncio.create_task(
                    self._read_stderr(proc, stderr_chunks)
                )

                await self._request(
                    proc,
                    f"{request_id}:initialize",
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "hashi-api-gateway",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self._send(proc, {"method": "initialized", "params": {}})

                thread_result = await self._request(
                    proc,
                    f"{request_id}:thread",
                    "thread/start",
                    {
                        "model": self.model,
                        "cwd": cwd,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "ephemeral": True,
                        "baseInstructions": _BASE_INSTRUCTIONS,
                        "developerInstructions": conversation.developer_instructions,
                        "dynamicTools": dynamic_tools,
                        "config": {
                            "project_doc_max_bytes": 0,
                            "history": {"persistence": "none"},
                            "memories": {
                                "generate_memories": False,
                                "use_memories": False,
                            },
                        },
                    },
                )
                thread = thread_result.get("thread") or {}
                thread_id = str(thread.get("id") or "")
                if not thread_id:
                    raise CodexAppServerError("Codex thread/start returned no thread id")

                if conversation.history_items:
                    await self._request(
                        proc,
                        f"{request_id}:history",
                        "thread/inject_items",
                        {
                            "threadId": thread_id,
                            "items": conversation.history_items,
                        },
                    )

                turn_result = await self._request(
                    proc,
                    f"{request_id}:turn",
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": conversation.turn_input,
                        "model": self.model,
                        "effort": self.effort,
                    },
                )
                turn = turn_result.get("turn") or {}
                turn_id = str(turn.get("id") or "")
                if not turn_id:
                    raise CodexAppServerError("Codex turn/start returned no turn id")

                while True:
                    message = await self._read(proc)
                    method = str(message.get("method") or "")
                    params = message.get("params") or {}

                    if method == "item/tool/call":
                        call_id = str(params.get("callId") or "")
                        codex_tool_name = str(params.get("tool") or "")
                        original_tool_name = codex_to_original.get(codex_tool_name)
                        if not call_id or call_id in external_call_ids:
                            protocol_error = "Codex returned a missing or duplicate tool call id"
                        elif original_tool_name is None:
                            protocol_error = (
                                "Codex attempted an undeclared caller-owned dynamic tool"
                            )
                        else:
                            arguments = params.get("arguments", {})
                            try:
                                if isinstance(arguments, str):
                                    json.loads(arguments)
                                    encoded_arguments = arguments
                                else:
                                    encoded_arguments = json.dumps(
                                        arguments,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                            except (TypeError, ValueError, json.JSONDecodeError):
                                protocol_error = (
                                    "Codex returned invalid JSON arguments for "
                                    f"{original_tool_name!r}"
                                )
                            else:
                                external_calls.append(
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": original_tool_name,
                                            "arguments": encoded_arguments,
                                        },
                                    }
                                )
                                external_call_ids.add(call_id)

                        if message.get("id") is not None:
                            await self._send(
                                proc,
                                {
                                    "id": message["id"],
                                    "result": {
                                        "contentItems": [
                                            {
                                                "type": "inputText",
                                                "text": _DEFERRED_TOOL_RESULT,
                                            }
                                        ],
                                        "success": protocol_error is None,
                                    },
                                },
                            )
                        if protocol_error and not interrupt_sent:
                            await self._send(
                                proc,
                                {
                                    "method": "turn/interrupt",
                                    "id": f"{request_id}:interrupt",
                                    "params": {
                                        "threadId": thread_id,
                                        "turnId": turn_id,
                                    },
                                },
                            )
                            interrupt_sent = True
                        continue

                    if method == "item/completed":
                        item = params.get("item") or {}
                        item_type = str(item.get("type") or "")
                        if item_type == "dynamicToolCall":
                            completed_id = str(item.get("id") or "")
                            if completed_id:
                                completed_call_ids.add(completed_id)
                        elif item_type == "agentMessage":
                            phase = str(item.get("phase") or "")
                            if phase != "commentary":
                                final_text = str(item.get("text") or "")
                        continue

                    if method == "item/started":
                        item = params.get("item") or {}
                        item_type = str(item.get("type") or "")
                        local_tool_started = item_type in _LOCAL_TOOL_ITEM_TYPES or (
                            item_type.endswith("ToolCall")
                            and item_type != "dynamicToolCall"
                        )
                        should_stop_after_dynamic_calls = (
                            external_calls
                            and not interrupt_sent
                            and completed_call_ids.issuperset(external_call_ids)
                            and item_type not in {"dynamicToolCall", "userMessage"}
                        )
                        if local_tool_started:
                            protocol_error = (
                                "Codex attempted disabled local tool item "
                                f"{item_type!r} during an API tool turn"
                            )
                        if (
                            (local_tool_started or should_stop_after_dynamic_calls)
                            and not interrupt_sent
                        ):
                            await self._send(
                                proc,
                                {
                                    "method": "turn/interrupt",
                                    "id": f"{request_id}:interrupt",
                                    "params": {
                                        "threadId": thread_id,
                                        "turnId": turn_id,
                                    },
                                },
                            )
                            interrupt_sent = True
                        continue

                    if method == "thread/tokenUsage/updated":
                        usage = self._usage(message) or usage
                        continue

                    if method == "turn/completed":
                        completed_turn = params.get("turn") or {}
                        status = str(completed_turn.get("status") or "")
                        error = completed_turn.get("error") or {}
                        if status == "failed":
                            detail = error.get("message") if isinstance(error, dict) else error
                            raise CodexAppServerError(
                                f"Codex external-tool turn failed: {detail or 'unknown error'}"
                            )
                        break

                    if message.get("id") is not None and method:
                        protocol_error = (
                            "Codex requested unsupported host method "
                            f"{method!r} during an API tool turn"
                        )
                        await self._send(
                            proc,
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "only caller-owned dynamic tools are enabled",
                                },
                            },
                        )
                        if not interrupt_sent:
                            await self._send(
                                proc,
                                {
                                    "method": "turn/interrupt",
                                    "id": f"{request_id}:interrupt",
                                    "params": {
                                        "threadId": thread_id,
                                        "turnId": turn_id,
                                    },
                                },
                            )
                            interrupt_sent = True

                if protocol_error:
                    raise CodexAppServerError(protocol_error)
                if external_calls and not completed_call_ids.issuperset(
                    external_call_ids
                ):
                    raise CodexAppServerError(
                        "Codex completed before every external tool call was finalized"
                    )
                if parallel_tool_calls is False and len(external_calls) > 1:
                    raise CodexAppServerError(
                        "Codex returned multiple calls while parallel_tool_calls=false"
                    )
                if tool_choice == "required" and not external_calls:
                    raise CodexAppServerError(
                        "Codex did not call a tool while tool_choice=required"
                    )
                selected_name = _tool_choice_name(tool_choice)
                if selected_name and not any(
                    call["function"]["name"] == selected_name
                    for call in external_calls
                ):
                    raise CodexAppServerError(
                        f"Codex did not call required tool {selected_name!r}"
                    )
                if not external_calls and not final_text:
                    raise CodexAppServerError(
                        "Codex completed without assistant text or a tool call"
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            detail = str(exc)
            if stderr_chunks:
                stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()
                if stderr_text and not external_calls:
                    detail = f"{detail}: {stderr_text[-2000:]}"
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=detail,
                is_success=False,
            )
        finally:
            if proc is not None:
                await self._stop_process(proc, reason=f"codex-api-tool-bridge:{request_id}")
                if self.on_process_stopped is not None:
                    self.on_process_stopped(proc)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            if temp_dir is not None:
                temp_dir.cleanup()

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if use_streaming and final_text and not external_calls and on_stream_event:
            await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=final_text))
        return BackendResponse(
            text="" if external_calls else final_text,
            duration_ms=duration_ms,
            is_success=True,
            tool_calls=external_calls or None,
            stop_reason="tool_calls" if external_calls else "stop",
            usage=usage,
            tool_call_count=len(external_calls),
            tool_loop_count=0,
        )
