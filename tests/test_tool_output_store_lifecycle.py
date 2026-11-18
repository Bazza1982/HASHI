from __future__ import annotations

import gc
import hashlib
import json
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import runtime_session
from orchestrator.session_store import SessionStore
from tools.tool_output_store import ToolOutputStore, run_binding


def _binding(run_id: str, request_id: str | None = None) -> tuple[str, ...]:
    return run_binding({
        "instance_id": "HASHI3",
        "agent_name": "lily",
        "owner_id": "user:7",
        "hashi_session_id": "session-1",
        "hashi_run_id": run_id,
        "request_id": request_id or run_id,
    })


def _all_pages(store, binding, reference, *, limit=9_913):
    parts = []
    offset = 0
    while True:
        page = json.loads(store.page(binding, reference, offset, limit))
        parts.append(page["text"])
        if page["complete"]:
            return "".join(parts), page
        assert page["next_offset"] > offset
        offset = page["next_offset"]


def test_large_result_is_retrievable_after_registry_replacement_without_retaining_text(tmp_path):
    class LargeText(str):
        pass

    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("run-large")
    state = {binding: "running"}
    store = ToolOutputStore(agent_home=home, state_resolver=lambda b: state.get(b))
    expected = ("海🦋\t\n" * 500_000) + "last bytes  \n"
    value = LargeText(expected)
    weak = weakref.ref(value)
    reference = store.retain(binding, "file_read", value)
    del value
    gc.collect()
    assert weak() is None, "the store must not retain the complete source string in memory"

    replacement = ToolOutputStore(agent_home=home, state_resolver=lambda b: state.get(b))
    actual, final = _all_pages(replacement, binding, reference)
    assert actual == expected
    assert final["sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert final["retention"] == "run_lifetime"
    with pytest.raises(ValueError):
        replacement.page(_binding("other-run"), reference, 0)


def test_terminal_reconciliation_cleans_only_finished_runs_and_preserves_unknown(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    first, second, unknown = _binding("one"), _binding("two"), _binding("unknown")
    state = {first: "running", second: "running"}
    store = ToolOutputStore(agent_home=home, state_resolver=lambda b: state.get(b))
    refs = {binding: store.retain(binding, "file_read", binding[-1] * 20_000)
            for binding in (first, second, unknown)}
    state[first] = "completed"
    replacement = ToolOutputStore(agent_home=home, state_resolver=lambda b: state.get(b))
    replacement.reconcile_terminal()

    assert not replacement.has_bound(first)
    assert replacement.has_bound(second)
    assert replacement.has_bound(unknown)
    with pytest.raises(ValueError):
        replacement.lookup(first, refs[first])
    assert replacement.lookup(second, refs[second]).source_tool == "file_read"

    state[second] = "stopped"
    assert replacement.cleanup_if_terminal(second)
    assert not replacement.has_bound(second)
    assert replacement.retain(second, "file_read", "late completion") is None
    assert replacement.has_bound(unknown)


def test_finished_run_cleanup_waits_for_a_concurrent_writer(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("cancelled")
    states = {binding: "running"}
    entered, proceed = Event(), Event()
    calls = 0

    def resolve(value):
        nonlocal calls
        result = states.get(value)
        calls += 1
        if calls == 1:
            entered.set()
            assert proceed.wait(5)
        return result

    store = ToolOutputStore(agent_home=home, state_resolver=resolve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(store.retain, binding, "file_read", "x" * 100_000)
        assert entered.wait(5)
        states[binding] = "stopped"
        cleanup = pool.submit(store.cleanup_if_terminal, binding)
        proceed.set()
        pending.result(timeout=15)
        assert cleanup.result(timeout=15)
    assert not store.has_bound(binding)
    assert store.retain(binding, "file_read", "late") is None


def test_screenshot_outcomes_survive_store_replacement(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("screens")
    first = ToolOutputStore(agent_home=home)
    reference = first.retain(binding, "desktop_screenshot", "two frames",
                             kind="legacy_screenshot", image_outcomes={0: True})
    second = ToolOutputStore(agent_home=home)
    assert _all_pages(second, binding, reference)[0] == "two frames"
    assert second.record_image_outcomes(binding, reference, {1: True}, total=2)


def test_corrupt_disk_content_is_reported_instead_of_silently_returned(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("corrupt")
    store = ToolOutputStore(agent_home=home)
    reference = store.retain(binding, "file_read", "retained contents" * 1000)
    data = next(home.rglob("*.data"))
    raw = data.read_bytes()
    data.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(ValueError, match="corrupt"):
        store.page(binding, reference, 0)


def test_canonical_terminal_listener_cleans_run_data_and_replacement_preserves_active(tmp_path, monkeypatch):
    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    accepted = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="request-1", text="work", source="session-api", idempotency_key="key-1",
    )
    token = sessions.mark_request_running(accepted.request_id, worker_id="worker-1")
    binding = run_binding({
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": accepted.run_id,
        "request_id": accepted.request_id,
    })
    from tools.tool_output_store import run_state_from_store
    def resolve(value):
        return run_state_from_store(sessions, value)
    first = ToolOutputStore(agent_home=home, state_resolver=resolve)
    reference = first.retain(binding, "file_read", "old contents" * 3000)
    replacement = ToolOutputStore(agent_home=home, state_resolver=resolve)
    assert replacement.lookup(binding, reference).source_tool == "file_read"

    runtime = SimpleNamespace(
        name="lily", workspace_dir=home,
        global_config=SimpleNamespace(instance_id="HASHI3", authorized_id=7),
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(current_backend=None),
        session_store=sessions, session_dir=tmp_path,
        _request_meta_by_id={accepted.request_id: {
            "owner_id": "user:7", "agent_id": "lily", "hashi_fencing_token": token,
        }},
        logger=Mock(),
    )
    monkeypatch.setattr(runtime_session, "capture_backend_binding", lambda *_a, **_k: None)
    resolution = runtime_session.finish_request_from_listener(
        runtime, accepted.request_id, {"success": True, "text": "done"},
    )
    assert resolution.run["state"] == "completed"
    assert not replacement.has_bound(binding)


def test_cancelled_run_is_reclaimed_by_new_worker_from_real_session_state(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    accepted = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="request-stop", text="work", source="session-api", idempotency_key="stop",
    )
    sessions.mark_request_running(accepted.request_id, worker_id="worker-1")
    binding = run_binding({
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": accepted.run_id,
        "request_id": accepted.request_id,
    })
    from tools.tool_output_store import run_state_from_store
    def resolve(value):
        return run_state_from_store(sessions, value)
    first = ToolOutputStore(agent_home=home, state_resolver=resolve)
    first.retain(binding, "file_read", "output")
    sessions.cancel_run(accepted.run_id, owner_id="user:7", reason="user_stop")
    replacement = ToolOutputStore(agent_home=home, state_resolver=resolve)
    replacement.reconcile_terminal()
    assert not replacement.has_bound(binding)
    assert replacement.retain(binding, "file_read", "late") is None


def test_completed_output_is_not_held_as_a_python_string():
    class LargeText(str):
        pass

    store = ToolOutputStore()
    value = LargeText("large synthetic output" * 100_000)
    observed = weakref.ref(value)
    reference = store.retain(_binding("heap"), "file_read", value)
    assert reference
    del value
    gc.collect()
    assert observed() is None


@pytest.mark.asyncio
async def test_real_registry_continuation_survives_worker_replacement_and_is_cleaned_on_cancel(tmp_path, monkeypatch):
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    accepted = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="registry-run", text="work", source="session-api", idempotency_key="registry",
    )
    sessions.mark_request_running(accepted.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": accepted.run_id,
        "request_id": accepted.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    first = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=home, secrets={},
                         audit_context=context, search_scope=scope)
    original = "long output 海🦋\n" * 8_000

    async def dispatch(_name, _arguments, *, tool_call_id=""):
        return original

    monkeypatch.setattr(first, "_dispatch", dispatch)
    result = await first.execute("file_read", {"path": str(home / "synthetic")})
    assert not result.is_error
    reference = result.details["output_continuation"]
    assert result.details["output_retention"] == "run_lifetime"

    replacement = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=home, secrets={},
                               audit_context=context, search_scope=scope)
    parts, offset = [], 0
    while True:
        page = await replacement.execute("tool_output_read", {"reference": reference, "offset": offset})
        assert not page.is_error, page.output
        value = json.loads(page.output)
        parts.append(value["text"])
        if value["complete"]:
            break
        offset = value["next_offset"]
    assert "".join(parts) == original
    denied = await replacement.execute_with_audit_context(
        "tool_output_read", {"reference": reference, "offset": 0},
        audit_context={"owner_id": "user:another"},
    )
    assert denied.is_error

    sessions.cancel_run(accepted.run_id, owner_id="user:7", reason="user_stop")
    next_worker = ToolRegistry(["file_read"], access_root=tmp_path, workspace_dir=home, secrets={},
                               audit_context=context, search_scope=scope)
    assert not next_worker._tool_output_store.has_bound(run_binding(context))


def test_gateway_descriptor_reads_exact_canonical_run_state_without_writing(tmp_path):
    from tools.tool_output_store import run_state_from_context

    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    accepted = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="descriptor-run", text="work", source="session-api", idempotency_key="descriptor",
    )
    binding = run_binding({
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": accepted.run_id,
        "request_id": accepted.request_id,
    })
    context = {"session_store_descriptor": {
        "db_path": str(sessions.db_path), "instance_id": "HASHI3",
    }}
    assert run_state_from_context(context, binding) == "queued"
    wrong = list(binding)
    wrong[2] = "user:another"
    assert run_state_from_context(context, tuple(wrong)) is None
    sessions.cancel_run(accepted.run_id, owner_id="user:7", reason="user_stop")
    assert run_state_from_context(context, binding) == "stopped"

def test_indexed_screenshot_pages_match_legacy_after_worker_replacement(tmp_path, monkeypatch):
    import base64
    import io

    from PIL import Image
    from tools.legacy_screenshot import legacy_screenshot_page

    stream = io.BytesIO()
    Image.new("RGB", (3, 3), "blue").save(stream, format="PNG")
    image = base64.b64encode(stream.getvalue()).decode("ascii")
    text = "first " + "".join(
        f"frame-{i} data:image/png;base64,{image if i != 3 else 'AAAA'} tail-{i} "
        for i in range(12)
    ) + "last"
    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("many-screens")
    first = ToolOutputStore(agent_home=home)
    reference = first.retain(binding, "desktop_screenshot", text, kind="legacy_screenshot")
    replacement = ToolOutputStore(agent_home=home)
    for offset in range(13):
        for limit in (1, 4, None):
            expected = legacy_screenshot_page("desktop_screenshot", text, offset=offset, limit=limit)
            actual = replacement.screenshot_page(binding, reference, offset, limit)
            assert {k: v for k, v in actual.items() if k != "content"} == {
                k: v for k, v in expected.items() if k != "content"
            }
            assert len(actual["content"]) == len(expected["content"])
            for left, right in zip(actual["content"], expected["content"], strict=True):
                if left.get("text", "").startswith("Screenshot unavailable: screenshot decode failed"):
                    assert right.get("text", "").startswith("Screenshot unavailable: screenshot decode failed")
                else:
                    assert left == right

    frames = next(home.rglob("*.frames"))
    raw = frames.read_bytes()
    frames.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(ValueError, match="corrupt"):
        replacement.screenshot_page(binding, reference, 0, 1)


def test_interrupted_run_creation_recovers_only_an_empty_owned_directory(tmp_path):
    import hashlib
    import json

    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("interrupted")
    store = ToolOutputStore(agent_home=home)
    root = store._root_for_use(create=True)
    key = hashlib.sha256(json.dumps(binding, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    run_dir = root / key
    run_dir.mkdir()
    (run_dir / "..run.json.lock").touch()
    reference = store.retain(binding, "file_read", "resumed")
    assert _all_pages(store, binding, reference)[0] == "resumed"

    other = _binding("unknown")
    key2 = hashlib.sha256(json.dumps(other, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    incomplete = root / key2
    incomplete.mkdir()
    (incomplete / "unknown-data").write_text("do not delete", encoding="utf-8")
    with pytest.raises(OSError, match="unknown contents"):
        store.retain(other, "file_read", "new")
    assert (incomplete / "unknown-data").read_text(encoding="utf-8") == "do not delete"


def test_unavailable_store_does_not_break_catalog_checks(tmp_path, monkeypatch):
    store = ToolOutputStore(agent_home=tmp_path)
    monkeypatch.setattr(store, "_root_for_use", lambda **_: (_ for _ in ()).throw(OSError("unavailable")))
    assert not store.has_bound(_binding("active"))
    assert not store.source_allowed(_binding("active"), "ref", {"file_read"})

@pytest.mark.asyncio
async def test_real_run_cancellation_cleans_only_its_own_output(tmp_path):
    import asyncio
    import logging

    from orchestrator import runtime_cancel
    from tools.tool_output_store import run_state_from_store

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    accepted = []
    for number in range(2):
        run = sessions.accept_run(
            session_id=session["session_id"], owner_id="user:7", agent_id="lily",
            request_id=f"cancel-{number}", text="work", source="session-api",
            idempotency_key=f"cancel-{number}",
        )
        sessions.mark_request_running(run.request_id, worker_id=f"worker-{number}")
        accepted.append(run)
    bindings = [run_binding({
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id,
    }) for run in accepted]
    store = ToolOutputStore(agent_home=home, state_resolver=lambda b: run_state_from_store(sessions, b))
    refs = [store.retain(binding, "file_read", "contents" * 1000) for binding in bindings]
    runtime = SimpleNamespace(
        name="lily", session_store=sessions, workspace_dir=home,
        global_config=SimpleNamespace(instance_id="HASHI3"), queue=asyncio.Queue(),
        current_request_meta=None, logger=logging.getLogger("test"),
    )

    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="user:7", session_id=session["session_id"],
        run_id=accepted[0].run_id, request_id=accepted[0].request_id,
    )
    assert result["terminal"] and result["status"] == "stopped"
    assert not store.has_bound(bindings[0])
    assert store.has_bound(bindings[1])
    assert _all_pages(store, bindings[1], refs[1])[0] == "contents" * 1000
    with pytest.raises(ValueError):
        store.lookup(bindings[0], refs[0])

def test_store_lock_wait_is_retryable_and_configurable(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import tools.tool_output_store as module

    calls = []

    @contextmanager
    def transient_lock(_path, *, timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise TimeoutError("temporary contention")
        yield

    monkeypatch.setattr(module, "file_write_lock", transient_lock)
    store = ToolOutputStore(agent_home=tmp_path)
    with store._file_lock(tmp_path):
        assert len(calls) == 3
    assert all(value == 60 for value in calls)

    monkeypatch.setenv("HASHI_TOOL_OUTPUT_LOCK_TIMEOUT_SECONDS", "0")
    configured = ToolOutputStore(agent_home=tmp_path)
    calls.clear()
    with pytest.raises(TimeoutError):
        with configured._file_lock(tmp_path):
            pytest.fail("a timed-out lock cannot be entered")
    assert calls == [0]

def test_streamed_text_spanning_disk_blocks_and_empty_chunks(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    binding = _binding("chunked")
    parts = ["", "中文", "🦋" * 70_000, "X" * 15, "", "tail\n"]
    expected = "".join(parts)
    store = ToolOutputStore(agent_home=home)
    reference = store.retain_text_chunks(binding, "shell", iter(parts))
    replacement = ToolOutputStore(agent_home=home)
    output, final = _all_pages(replacement, binding, reference)
    assert output == expected
    assert final["sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()

@pytest.mark.asyncio
async def test_shell_large_stdout_stderr_are_fully_paged_after_replacement(tmp_path, monkeypatch):
    import sys
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    run = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="shell-large", text="work", source="session-api", idempotency_key="shell-large",
    )
    sessions.mark_request_running(run.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    # Exercise the real pipe, capture and registry path without invoking a shell.
    program = ("import sys; sys.stdout.buffer.write(('A' * 130000 + '海END').encode('utf-8')); "
               "sys.stderr.buffer.write(('E' * 90000 + 'ERR-END').encode('utf-8')); sys.exit(7)")
    invocation = SimpleNamespace(argv=(sys.executable, "-c", program), shell="synthetic",
                                 executable=sys.executable, launcher="direct",
                                 launcher_executable=sys.executable, encoding="utf-8")
    monkeypatch.setattr("tools.builtins.resolve_shell_invocation", lambda *_: invocation)
    first = ToolRegistry(["shell"], access_root=tmp_path, workspace_dir=home, secrets={},
                         audit_context=context, search_scope=scope)
    result = await first.execute("shell", {"command": "fixture"}, tool_call_id="capture")
    reference = result.details["output_continuation"]
    assert result.details["exit_code"] == 7
    replacement = ToolRegistry(["shell"], access_root=tmp_path, workspace_dir=home, secrets={},
                               audit_context=context, search_scope=scope)
    pieces, offset = [], 0
    while True:
        page = await replacement.execute("tool_output_read", {"reference": reference, "offset": offset})
        assert not page.is_error, page.output
        row = json.loads(page.output)
        pieces.append(row["text"])
        if row["complete"]:
            break
        offset = row["next_offset"]
    expected = "[exit code 7]\n" + "A" * 130000 + "海END\n[stderr]\n" + "E" * 90000 + "ERR-END"
    assert "".join(pieces) == expected

@pytest.mark.asyncio
async def test_shell_timeout_preserves_all_captured_output(tmp_path, monkeypatch):
    import sys
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    run = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="shell-timeout", text="work", source="session-api", idempotency_key="shell-timeout",
    )
    sessions.mark_request_running(run.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    program = "import sys,time; sys.stdout.buffer.write(b'X' * 90000); sys.stdout.flush(); time.sleep(60)"
    invocation = SimpleNamespace(argv=(sys.executable, "-c", program), shell="synthetic",
                                 executable=sys.executable, launcher="direct",
                                 launcher_executable=sys.executable, encoding="utf-8")
    monkeypatch.setattr("tools.builtins.resolve_shell_invocation", lambda *_: invocation)
    registry = ToolRegistry(["shell"], access_root=tmp_path, workspace_dir=home, secrets={},
                            audit_context=context, search_scope=scope)
    result = await registry.execute("shell", {"command": "fixture", "timeout": 1}, tool_call_id="timeout")
    assert result.is_error and "timed out" in result.output
    reference = result.details["output_continuation"]
    content, _ = _all_pages(registry._tool_output_store, run_binding(context), reference)
    assert content == "Error: command timed out after 1s\n[partial output]\n" + "X" * 90000
    assert result.details["foreground_cleanup"]["process_reaped"]

@pytest.mark.asyncio
async def test_cancelled_shell_closes_spools_without_touching_other_run(tmp_path, monkeypatch):
    import asyncio
    import sys
    from tools.registry import ToolRegistry
    from tools.tool_output_store import run_state_from_store

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    runs = []
    for suffix in ("target", "other"):
        run = sessions.accept_run(
            session_id=session["session_id"], owner_id="user:7", agent_id="lily",
            request_id=f"shell-{suffix}", text="work", source="session-api", idempotency_key=f"shell-{suffix}",
        )
        sessions.mark_request_running(run.request_id, worker_id=f"worker-{suffix}")
        runs.append(run)
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": runs[0].run_id,
        "request_id": runs[0].request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    registry = ToolRegistry(["shell"], access_root=tmp_path, workspace_dir=home, secrets={},
                            audit_context=context, search_scope=scope)
    other_context = {**context, "hashi_run_id": runs[1].run_id, "request_id": runs[1].request_id}
    other_binding = run_binding(other_context)
    reference = registry._tool_output_store.retain(other_binding, "shell", "other run")
    ready = tmp_path / "started"
    program = ("import sys,time,pathlib; sys.stdout.buffer.write(b'X'*90000); sys.stdout.flush(); "
               f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(60)")
    invocation = SimpleNamespace(argv=(sys.executable, "-c", program), shell="synthetic",
                                 executable=sys.executable, launcher="direct",
                                 launcher_executable=sys.executable, encoding="utf-8")
    monkeypatch.setattr("tools.builtins.resolve_shell_invocation", lambda *_: invocation)
    task = asyncio.create_task(registry.execute("shell", {"command": "fixture"}, tool_call_id="cancel"))
    try:
        async def wait_ready():
            while not ready.exists():
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait_ready(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    sessions.cancel_run(runs[0].run_id, owner_id="user:7", reason="user_stop")
    store = ToolOutputStore(agent_home=home, state_resolver=lambda b: run_state_from_store(sessions, b))
    store.reconcile_terminal()
    assert _all_pages(store, other_binding, reference)[0] == "other run"
    root = home / ".hashi-tool-output-v1"
    assert all(path.name.startswith(".") or path.is_dir() for path in root.iterdir())

@pytest.mark.asyncio
async def test_verification_large_output_is_completely_retrievable(tmp_path):
    import sys
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    run = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="verify-large", text="work", source="session-api", idempotency_key="verify-large",
    )
    sessions.mark_request_running(run.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    registry = ToolRegistry(["verification_run"], access_root=tmp_path, workspace_dir=home, secrets={},
                            audit_context=context, search_scope=scope)
    program = ("import sys; sys.stdout.buffer.write(b'V'*110000); "
               "sys.stderr.buffer.write(b'E'*90000); sys.exit(7)")
    result = await registry.execute("verification_run", {
        "operation": "run", "argv": [sys.executable, "-c", program],
    })
    assert result.is_error and result.details["exit_code"] == 7
    reference = result.details["output_continuation"]
    captured, _ = _all_pages(registry._tool_output_store, run_binding(context), reference)
    assert captured == "Error: verification direct_argv failed with exit code 7\n" + "V" * 110000 + "\n" + "E" * 90000

@pytest.mark.asyncio
async def test_workspace_inspection_search_pages_all_matches(tmp_path):
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    source = home / "many.txt"
    source.write_text("MATCH row-" + "x" * 65 + "\n", encoding="utf-8")
    with source.open("a", encoding="utf-8") as output:
        for index in range(5000):
            output.write(f"MATCH {index:05d} " + "y" * 65 + "\n")
        output.write("MATCH last-sentinel\n")
    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    run = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="inspect-search", text="work", source="session-api", idempotency_key="inspect-search",
    )
    sessions.mark_request_running(run.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    scope = {"agent_id": "lily", "agent_home": str(home)}
    registry = ToolRegistry(["workspace_inspect"], access_root=tmp_path, workspace_dir=home, secrets={},
                            audit_context=context, search_scope=scope)
    result = await registry.execute("workspace_inspect", {"operation": "search", "query": "MATCH", "path": "many.txt"})
    assert not result.is_error, result.output
    reference = result.details["output_continuation"]
    complete, _ = _all_pages(registry._tool_output_store, run_binding(context), reference)
    assert "MATCH row-" in complete and "MATCH last-sentinel" in complete
    assert complete.count("MATCH ") == 5002


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "diff"])
async def test_workspace_inspection_pages_full_git_output(tmp_path, operation):
    import subprocess
    from tools.registry import ToolRegistry

    home = tmp_path / "repository"
    home.mkdir()
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=home, check=True, capture_output=True,
            text=True, encoding="utf-8",
        )

    git("init", "--quiet")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    source = home / "tracked.txt"
    source.write_text("original\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "--quiet", "-m", "fixture")
    if operation == "diff":
        source.write_text("".join(f"row {i:05d} " + "x" * 75 + "\n" for i in range(3000))
                          + "unique diff final sentinel\n", encoding="utf-8")
        expected = "unique diff final sentinel"
    else:
        for index in range(1100):
            (home / f"item-{index:04d}-{'x' * 80}.txt").touch()
        (home / "zz-status-final-sentinel.txt").touch()
        expected = "zz-status-final-sentinel.txt"

    sessions = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    session = sessions.ensure_default_session(owner_id="user:7", agent_id="lily")
    run = sessions.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="inspect-git", text="work", source="session-api", idempotency_key="inspect-git",
    )
    sessions.mark_request_running(run.request_id, worker_id="worker-1")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": session["session_id"], "hashi_run_id": run.run_id,
        "request_id": run.request_id, "_runtime": SimpleNamespace(session_store=sessions),
    }
    registry = ToolRegistry(
        ["workspace_inspect"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context=context, search_scope={"agent_id": "lily", "agent_home": str(home)},
    )
    result = await registry.execute("workspace_inspect", {"operation": operation})
    assert not result.is_error, result.output
    reference = result.details["output_continuation"]
    actual, page = _all_pages(registry._tool_output_store, run_binding(context), reference)
    assert expected in actual
    assert page["complete"]
    if operation == "status":
        assert "item-0000-" in actual and "item-1099-" in actual
    else:
        assert "+row 00000 " in actual and "+row 02999 " in actual


@pytest.mark.asyncio
async def test_workspace_inspection_reports_retention_failure_without_inventing_match_count(tmp_path, monkeypatch):
    from tools.registry import ToolRegistry

    home = tmp_path / "agent"
    home.mkdir()
    (home / "many.txt").write_text("MATCH " + "x" * 40000 + "\n", encoding="utf-8")
    context = {
        "instance_id": "HASHI3", "agent_name": "lily", "owner_id": "user:7",
        "hashi_session_id": "session-1", "hashi_run_id": "run-1", "request_id": "req-1",
    }
    registry = ToolRegistry(
        ["workspace_inspect"], access_root=tmp_path, workspace_dir=home, secrets={},
        audit_context=context, search_scope={"agent_id": "lily", "agent_home": str(home)},
    )
    def no_storage():
        raise OSError("simulated unavailable capture storage")
    monkeypatch.setattr(registry._tool_output_store, "temporary_capture_file", no_storage)
    result = await registry.execute(
        "workspace_inspect", {"operation": "search", "query": "MATCH", "path": "many.txt"},
    )
    assert "retention is unavailable" in result.output
    assert result.details["complete_content"] is False
    assert result.details["output_continuation"] is None
    assert result.details["matches"] is None
