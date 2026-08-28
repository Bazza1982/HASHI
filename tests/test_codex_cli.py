from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from adapters.base import BackendResponse
from adapters.codex_cli import CodexCLIAdapter
from adapters.stream_events import KIND_COMMENTARY, KIND_THINKING
from orchestrator.multimodal_contract import canonical_request_content
from tests.mocks.mock_adapters import SimpleGlobalConfig, SimpleTestConfig


class _FakeStdout:
    def __init__(self, proc: "_HangingProc", lines: list[str]):
        self._proc = proc
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]

    async def read(self, _size: int) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        await self._proc.wait()
        return b""


class _FakeStderr:
    def __init__(self, proc: "_HangingProc"):
        self._proc = proc

    async def read(self, _size: int) -> bytes:
        await self._proc.wait()
        return b""


class _FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _HangingProc:
    def __init__(self, lines: list[str], pid: int = 12345):
        self.pid = pid
        self.returncode = None
        self.stdin = _FakeStdin()
        self._exit_event = asyncio.Event()
        self.stdout = _FakeStdout(self, lines)
        self.stderr = _FakeStderr(self)

    async def wait(self) -> int:
        await self._exit_event.wait()
        return int(self.returncode or 0)

    def finish(self, code: int) -> None:
        self.returncode = code
        self._exit_event.set()


def _build_adapter(tmp_path: Path) -> CodexCLIAdapter:
    cfg = SimpleTestConfig(name="hashiko", workspace_dir=str(tmp_path))
    cfg.model = "gpt-5.4"
    global_cfg = SimpleGlobalConfig()
    return CodexCLIAdapter(cfg, global_cfg)


@pytest.mark.asyncio
async def test_codex_normal_generate_attaches_validated_native_image_path(
    tmp_path, monkeypatch
):
    adapter = _build_adapter(tmp_path)
    image = tmp_path / "one.png"
    payload = b"\x89PNG\r\n\x1a\nimage"
    image.write_bytes(payload)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-1",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {},
            },
        ]
    )
    proc = _HangingProc(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "seen"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    captured_command = []

    async def create_subprocess(*args, **_kwargs):
        captured_command.extend(args)
        proc.finish(0)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    response = await adapter.generate_response(
        "Inspect it.",
        "request-image",
        request_content=content,
        reasoning_effort="high",
    )

    assert response.is_success is True
    assert response.text == "seen"
    assert "--image" in captured_command
    assert captured_command[captured_command.index("--image") + 1] == str(
        image.resolve()
    )
    prompt_argument = captured_command[captured_command.index("--") + 1]
    assert str(image) not in prompt_argument
    assert 'model_reasoning_effort="high"' in captured_command
    assert response.stream_metadata["multimodal_routing"][0]["attachment_id"] == (
        "attachment-1"
    )
    assert response.stream_metadata["multimodal_routing"][0]["transport"] == (
        "local_path"
    )


@pytest.mark.asyncio
async def test_codex_document_keeps_established_local_file_fallback(
    tmp_path,
    monkeypatch,
):
    adapter = _build_adapter(tmp_path)
    document = tmp_path / "notes.txt"
    payload = b"verified notes"
    document.write_bytes(payload)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Read the notes."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-document",
                "modality": "document",
                "kind": "document",
                "mime_type": "text/plain",
                "filename": document.name,
                "caption": "",
                "local_ref": str(document),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {},
            },
        ]
    )
    proc = _HangingProc(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "read"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    captured_command = []

    async def create_subprocess(*args, **_kwargs):
        captured_command.extend(args)
        proc.finish(0)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    response = await adapter.generate_response(
        "Read the received document.",
        "request-document",
        request_content=content,
    )

    assert response.is_success is True
    assert "--image" not in captured_command
    assert captured_command[captured_command.index("--") + 1] == "-"
    stdin_prompt = proc.stdin.data.decode("utf-8")
    assert proc.stdin.closed is True
    assert "attachment-document" in stdin_prompt
    assert str(document) in stdin_prompt
    assert "media bytes were not sent natively" in stdin_prompt
    assert response.stream_metadata["multimodal_routing"][0]["route"] == (
        "local_fallback"
    )


class _CompletedProc:
    def __init__(self, *, pid: int = 12345, stdout: bytes = b"[]"):
        self.pid = pid
        self.returncode = 0
        self._stdout = stdout
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_codex_intermediate_agent_message_is_full_commentary_not_reasoning(tmp_path):
    adapter = _build_adapter(tmp_path)
    events = []

    async def collect(event):
        events.append(event)

    commentary = "Inspecting the failing test\n\n" + ("full detail " * 60)
    adapter._flush_pending_agent_message({"text": commentary}, collect)
    await asyncio.sleep(0)

    assert adapter.capabilities.supports_thinking_stream is False
    assert adapter.capabilities.supports_commentary_stream is True
    assert [event.kind for event in events] == [KIND_COMMENTARY]
    assert events[0].summary == commentary.strip()
    assert all(event.kind != KIND_THINKING for event in events)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group isolation only")
async def test_codex_mcp_inventory_starts_in_an_isolated_session(
    tmp_path, monkeypatch
):
    adapter = _build_adapter(tmp_path)
    proc = _CompletedProc(stdout=b'[{"name":"github"}]')
    captured_kwargs = {}

    async def create_subprocess(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    discovered = await adapter._discover_mcp_servers()

    assert discovered == ("github",)
    assert captured_kwargs["start_new_session"] is True
    assert captured_kwargs["cwd"] == str(adapter.effective_workdir)
    assert adapter._external_tool_processes == set()


@pytest.mark.asyncio
async def test_codex_mcp_inventory_retries_one_timeout_without_stale_fallback(
    tmp_path, monkeypatch
):
    adapter = _build_adapter(tmp_path)

    class _TimedOutProc(_CompletedProc):
        def __init__(self):
            super().__init__()
            self.returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

    first = _TimedOutProc()
    second = _CompletedProc(stdout=b'[{"name":"openaiDeveloperDocs"}]')
    pending = [first, second]
    created = []
    killed = []

    async def create_subprocess(*_args, **_kwargs):
        proc = pending.pop(0)
        created.append(proc)
        return proc

    async def force_kill(proc, **_kwargs):
        proc.kill()
        killed.append(proc)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(adapter, "force_kill_process_tree", force_kill)
    monkeypatch.setattr(adapter, "MCP_INVENTORY_TIMEOUT_SEC", 0.001)

    discovered = await adapter._discover_mcp_servers()

    assert discovered == ("openaiDeveloperDocs",)
    assert created == [first, second]
    assert killed == [first]
    assert adapter._external_tool_processes == set()


@pytest.mark.asyncio
async def test_codex_mcp_inventory_fails_closed_after_bounded_timeout_retries(
    tmp_path, monkeypatch
):
    adapter = _build_adapter(tmp_path)

    class _TimedOutProc(_CompletedProc):
        def __init__(self):
            super().__init__()
            self.returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

    pending = [_TimedOutProc(), _TimedOutProc()]
    killed = []

    async def create_subprocess(*_args, **_kwargs):
        return pending.pop(0)

    async def force_kill(proc, **_kwargs):
        proc.kill()
        killed.append(proc)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(adapter, "force_kill_process_tree", force_kill)
    monkeypatch.setattr(adapter, "MCP_INVENTORY_TIMEOUT_SEC", 0.001)

    with pytest.raises(RuntimeError, match=r"timed out after 2 attempts"):
        await adapter._discover_mcp_servers()

    assert len(killed) == 2
    assert adapter._external_tool_processes == set()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group isolation only")
async def test_force_kill_refuses_hashi_own_process_group(
    tmp_path, monkeypatch, caplog
):
    adapter = _build_adapter(tmp_path)
    proc = _CompletedProc(pid=4242)
    proc.returncode = None
    killpg_calls = []

    monkeypatch.setattr("adapters.base.os.getpgid", lambda _pid: 9000)
    monkeypatch.setattr("adapters.base.os.getpgrp", lambda: 9000)
    monkeypatch.setattr(
        "adapters.base.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )

    killed = await adapter.force_kill_process_tree(
        proc,
        logger=adapter.logger,
        reason="gateway-hot-reload",
    )

    assert killed is True
    assert proc.killed is True
    assert killpg_calls == []
    assert "Refused unsafe killpg" in caplog.text


@pytest.mark.asyncio
async def test_codex_only_emits_agent_messages_that_are_proven_intermediate(tmp_path):
    adapter = _build_adapter(tmp_path)
    events = []

    async def collect(event):
        events.append(event)

    pending = adapter._parse_codex_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first update"}}),
        collect,
    )
    pending = adapter._parse_codex_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}}),
        collect,
        pending_agent_message=pending,
    )
    pending = adapter._parse_codex_event(
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 2}}),
        collect,
        pending_agent_message=pending,
    )
    await asyncio.sleep(0)

    assert pending is None
    assert [(event.kind, event.summary) for event in events] == [
        (KIND_COMMENTARY, "first update")
    ]


def test_codex_accepts_completed_turn_even_if_process_needs_forced_exit(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    adapter.set_session_mode(True)
    adapter.POST_TURN_COMPLETION_GRACE_SEC = 0.01

    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 3}}),
    ]
    proc = _HangingProc(lines)
    killed_reasons: list[str] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    async def _fake_force_kill_process_tree(proc_obj, logger=None, reason: str = ""):
        killed_reasons.append(reason)
        proc_obj.finish(-9)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(adapter, "force_kill_process_tree", _fake_force_kill_process_tree)

    response = asyncio.run(adapter.generate_response("hello", "req-0001"))

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.usage is not None
    assert response.usage.input_tokens == 10
    assert response.usage.thinking_tokens == 3
    assert adapter._session_id == "thread_123"
    assert killed_reasons == ["turn-completed-grace-expired:req-0001"]


def test_codex_idle_timeout_is_enforced_when_process_stalls(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    adapter.config.extra["idle_timeout_sec"] = 1
    adapter.config.extra["hard_timeout_sec"] = 30
    proc = _HangingProc([])
    killed_reasons: list[str] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    async def _fake_force_kill_process_tree(proc_obj, logger=None, reason: str = ""):
        killed_reasons.append(reason)
        proc_obj.finish(-9)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(adapter, "force_kill_process_tree", _fake_force_kill_process_tree)

    response = asyncio.run(adapter.generate_response("hello", "req-0002"))

    assert response.is_success is False
    assert "idle for 1s" in (response.error or "")
    assert killed_reasons == ["idle-timeout:req-0002"]


def test_codex_idle_timeout_preserves_last_agent_message(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    adapter.config.extra["idle_timeout_sec"] = 1
    adapter.config.extra["hard_timeout_sec"] = 30
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread_timeout"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "I made useful progress before stalling."}}),
    ]
    proc = _HangingProc(lines)
    killed_reasons: list[str] = []

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    async def _fake_force_kill_process_tree(proc_obj, logger=None, reason: str = ""):
        killed_reasons.append(reason)
        proc_obj.finish(-9)
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(adapter, "force_kill_process_tree", _fake_force_kill_process_tree)

    response = asyncio.run(adapter.generate_response("hello", "req-0003"))

    assert response.is_success is False
    assert "idle for 1s" in (response.error or "")
    assert "I made useful progress before stalling." in (response.error or "")
    assert killed_reasons == ["idle-timeout:req-0003"]
    assert "I made useful progress before stalling." in (tmp_path / "codex_exec_events.jsonl").read_text()


def test_codex_nonzero_exit_preserves_last_agent_message(tmp_path, monkeypatch: pytest.MonkeyPatch):
    adapter = _build_adapter(tmp_path)
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thread_nonzero"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Latest progress before stop."}}),
    ]
    proc = _HangingProc(lines)

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        async def _finish():
            await asyncio.sleep(0.01)
            proc.finish(1)

        asyncio.create_task(_finish())
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    response = asyncio.run(adapter.generate_response("hello", "req-0004"))

    assert response.is_success is False
    assert "non-zero status" in (response.error or "")
    assert "Latest progress before stop." in (response.error or "")
    assert "Latest progress before stop." in (tmp_path / "codex_exec_events.jsonl").read_text()


def test_codex_add_dir_uses_access_root_when_workzone_off(tmp_path):
    adapter = _build_adapter(tmp_path / "workspace")
    project_root = tmp_path / "project"
    project_root.mkdir()
    adapter.config.resolve_access_root = lambda: project_root

    cmd = adapter._build_cmd("hello", tmp_path / "out.txt")

    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(project_root)


def test_codex_add_dir_uses_workzone_when_workzone_on(tmp_path):
    workspace = tmp_path / "workspace"
    workzone = tmp_path / "repo"
    workspace.mkdir()
    workzone.mkdir()
    adapter = _build_adapter(workspace)
    adapter.config.resolve_access_root = lambda: tmp_path / "project"
    adapter.config.extra["workzone_dir"] = str(workzone)

    cmd = adapter._build_cmd("hello", tmp_path / "out.txt")

    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(workzone.resolve())


def test_codex_long_prompt_is_preserved_and_switched_to_stdin(tmp_path):
    adapter = _build_adapter(tmp_path)
    prompt = "z" * (adapter.LONG_PROMPT_STDIN_THRESHOLD + 10_000)

    assert adapter._should_use_stdin_transport(prompt) is True
    assert adapter._sanitize_for_codex(prompt) == prompt


def test_codex_resume_is_used_only_in_explicit_session_mode(tmp_path):
    adapter = _build_adapter(tmp_path)
    adapter._session_id = "thread-existing"

    flex_cmd = adapter._build_cmd("hello", tmp_path / "flex.txt")
    assert "resume" not in flex_cmd

    adapter.set_session_mode(True)
    adapter._session_id = "thread-existing"
    fixed_cmd = adapter._build_cmd("hello", tmp_path / "fixed.txt")
    assert fixed_cmd[1:4] == ["exec", "resume", "thread-existing"]

    adapter.set_session_mode(False)
    assert adapter._session_id is None


def test_codex_command_reasoning_effort_is_request_scoped(tmp_path):
    adapter = _build_adapter(tmp_path)

    cmd = adapter._build_cmd(
        "hello",
        tmp_path / "out.txt",
        reasoning_effort="max",
    )

    assert 'model_reasoning_effort="max"' in cmd
    assert 'model_reasoning_effort="medium"' not in cmd
    assert adapter.effort == "medium"


def test_codex_command_rejects_unknown_request_reasoning_effort(tmp_path):
    adapter = _build_adapter(tmp_path)

    with pytest.raises(ValueError, match="Codex reasoning_effort"):
        adapter._build_cmd(
            "hello",
            tmp_path / "out.txt",
            reasoning_effort="ultra",
        )


@pytest.mark.asyncio
async def test_codex_external_tools_refresh_mcp_inventory_each_request(
    tmp_path, monkeypatch
):
    adapter = _build_adapter(tmp_path)
    inventories = [("first",), ("first", "second")]
    discovered = []
    bridge_inventories = []
    bridge_efforts = []

    async def discover():
        value = inventories[len(discovered)]
        discovered.append(value)
        return value

    class _Bridge:
        def __init__(self, **kwargs):
            bridge_inventories.append(kwargs["disabled_mcp_servers"])
            bridge_efforts.append(kwargs["effort"])

        async def run(self, *_args, **_kwargs):
            return BackendResponse(text="done", duration_ms=1)

    monkeypatch.setattr(adapter, "_discover_mcp_servers", discover)
    monkeypatch.setattr("adapters.codex_cli.CodexAppServerToolBridge", _Bridge)

    for request_id, effort in (("req-one", "high"), ("req-two", "max")):
        response = await adapter.generate_external_tool_response(
            [{"role": "user", "content": "hello"}],
            [],
            request_id,
            model="gpt-5.6-luna",
            request_options={"reasoning_effort": effort},
        )
        assert response.is_success is True

    assert discovered == inventories
    assert bridge_inventories == inventories
    assert bridge_efforts == ["high", "max"]
    assert adapter.effort == "medium"
