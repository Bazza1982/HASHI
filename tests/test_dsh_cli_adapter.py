"""Tests for the DeepSeek Harness (dsh-cli / DSH) ACP stdio adapter and HASHI wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from adapters.dsh_cli import DshCLIAdapter, _match_model_option, _normalize_dsh_effort
from adapters.registry import get_backend_class, registered_backend_engines
from adapters.stream_events import (
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_FILE_READ,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
)
from orchestrator.api_gateway_preflight import check_gateway_engine
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.config import SESSION_MODE_BACKENDS
from orchestrator.flexible_backend_registry import (
    CLI_ENGINES,
    canonical_backend_engine,
    get_available_efforts,
    get_available_models,
    get_backend_label,
    normalize_effort,
    public_backend_engine,
)
from orchestrator.pathing import resolve_dsh_invocation


_MOCK_ACP_SERVER_SCRIPT = textwrap.dedent(
    """
    import json, os, sys, time, uuid

    if "--help" in sys.argv:
        print("Usage: dsh --profile acp [options]")
        sys.exit(0)

    state = {
        "sessions": {},
        "active_prompt": None,
        "rpc_out_id": 1000,
    }

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    def record_usage(sid, turn):
        # Mirror DSH's own per-session projection so the adapter's delta read
        # can be exercised end to end. Only active when the test sets DSH_HOME.
        home = os.environ.get("DSH_HOME")
        if not home:
            return
        folder = os.path.join(home, "storages", "session_projcache", "sessions")
        os.makedirs(folder, exist_ok=True)
        totals = {
            "uncachedInputTokens": 1000 * turn,
            "cacheReadTokens": 500 * turn,
            "cacheWriteTokens": 0,
            "outputTokens": 200 * turn,
        }
        path = os.path.join(folder, sid + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": 1,
                    "record": {
                        "rows": {
                            "tokenUsage": {
                                "ver": 2,
                                "seq": turn,
                                "val": {"totals": totals},
                            }
                        }
                    },
                },
                fh,
            )

    def config_options(sess):
        return [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": sess["model"],
                "options": [
                    {
                        "group": "deepseek-official",
                        "name": "DeepSeek API",
                        "options": [
                            {"value": json.dumps(["deepseek-official", "deepseek-v4-flash"]), "name": "deepseek-v4-flash"},
                            {"value": json.dumps(["deepseek-official", "deepseek-flash"]), "name": "DeepSeek-V4.1-Flash"},
                            {"value": json.dumps(["deepseek-official", "deepseek-v4-pro"]), "name": "DeepSeek-V4-Pro"},
                            {"value": json.dumps(["custom-provider", "my-custom-v9"]), "name": "My Custom v9"},
                        ],
                    }
                ],
            },
            {
                "id": "reasoning_effort",
                "name": "Reasoning effort",
                "category": "thought_level",
                "type": "select",
                "currentValue": sess["effort"],
                "options": [
                    {"value": "off", "name": "Off"},
                    {"value": "low", "name": "Low"},
                    {"value": "high", "name": "High"},
                    {"value": "max", "name": "Max"},
                ],
            },
        ]

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "mock-dsh-acp", "version": "0.0.1"},
                    "agentCapabilities": {
                        "mcpCapabilities": {"http": True},
                        "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                        "sessionCapabilities": {"close": {}, "list": {}, "resume": {}},
                    },
                    "authMethods": [],
                },
            })
            continue

        if method == "session/new":
            sid = f"mock-sid-{uuid.uuid4().hex[:8]}"
            sess = {
                "model": json.dumps(["deepseek-official", "deepseek-v4-flash"]),
                "effort": "high",
                "turns": [],
                "resumed": False,
                "cancelled": False,
                "mcp_args": [s.get("args", []) for s in (params.get("mcpServers") or [])],
            }
            state["sessions"][sid] = sess
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"sessionId": sid, "configOptions": config_options(sess)}})
            continue

        if method == "session/resume":
            sid = params.get("sessionId")
            if str(sid or "").startswith("unresumable-"):
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": f"session is not resumable: {sid}"},
                })
                continue
            sess = {
                "model": json.dumps(["deepseek-official", "deepseek-v4-pro"]),
                "effort": "high",
                "turns": [],
                "resumed": True,
                "cancelled": False,
                "mcp_args": [s.get("args", []) for s in (params.get("mcpServers") or [])],
            }
            state["sessions"][sid] = sess
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"configOptions": config_options(sess)}})
            continue

        if method == "session/close":
            state["sessions"].pop(params.get("sessionId"), None)
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
            continue

        if method == "session/set_config_option":
            sid = params["sessionId"]
            sess = state["sessions"][sid]
            if params["configId"] == "model":
                sess["model"] = params["value"]
            elif params["configId"] == "reasoning_effort":
                sess["effort"] = params["value"]
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"configOptions": config_options(sess)}})
            continue

        if method == "session/cancel":
            ap = state.get("active_prompt")
            if ap and ap["sid"] == params.get("sessionId"):
                send({"jsonrpc": "2.0", "id": ap["rpc_id"], "result": {"stopReason": "cancelled"}})
                state["active_prompt"] = None
            continue

        if method == "session/prompt":
            sid = params["sessionId"]
            sess = state["sessions"][sid]
            text = " ".join(b.get("text", "") for b in params.get("prompt", []) if b.get("type") == "text")
            sess["turns"].append(text)
            record_usage(sid, len(sess["turns"]))

            if "HANG_FOR_STOP" in text:
                state["active_prompt"] = {"sid": sid, "rpc_id": rpc_id}
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "messageId": "m-hang",
                            "content": {"type": "text", "text": "thinking before cancel"},
                        },
                    },
                })
                continue

            # Test permission roundtrip + thinking + commentary + tool + final answer + usage
            if "WITH_TOOLS" in text:
                state["rpc_out_id"] += 1
                perm_id = state["rpc_out_id"]
                send({
                    "jsonrpc": "2.0",
                    "id": perm_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": "call-1"},
                        "options": [
                            {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                })
                # Read client permission reply synchronously
                perm_reply = json.loads(sys.stdin.readline())
                outcome = ((perm_reply.get("result") or {}).get("outcome") or {}).get("optionId")
                assert outcome == "allow-once", f"unexpected permission outcome: {perm_reply}"

                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "messageId": "m1",
                            "content": {"type": "text", "text": "Need to read config first."},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": "m1",
                            "content": {"type": "text", "text": "Checking README now."},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "call-1",
                            "title": "read_file",
                            "kind": "other",
                            "status": "in_progress",
                            "rawInput": {"path": "README.md"},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "call-1",
                            "status": "completed",
                            "content": [{"type": "content", "content": {"type": "text", "text": "README content"}}],
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "messageId": "m2",
                            "content": {"type": "text", "text": f"Final answer turn={len(sess['turns'])} model={sess['model']} effort={sess['effort']} resumed={sess['resumed']}"},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "usage_update", "used": 1234, "size": 1048576},
                    },
                })
                send({"jsonrpc": "2.0", "id": rpc_id, "result": {"stopReason": "end_turn"}})
                continue

            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": f"m-{len(sess['turns'])}",
                        "content": {
                            "type": "text",
                            "text": f"OK sid={sid} turns={len(sess['turns'])} model={sess['model']} effort={sess['effort']} resumed={sess['resumed']} mcp={json.dumps(sess.get('mcp_args', []))}",
                        },
                    },
                },
            })
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {"stopReason": "end_turn"}})
    """
)


def _make_adapter(tmp_path: Path, mock_script: Path, *, session_mode: bool = True, model: str = "deepseek-v4-pro", effort: str = "high") -> DshCLIAdapter:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agent_cfg = SimpleNamespace(
        name="dsh-test-agent",
        engine="dsh-cli",
        model=model,
        workspace_dir=workspace,
        extra={"session_mode": session_mode, "effort": effort},
        access_scope="drive",
    )
    global_cfg = SimpleNamespace(
        dsh_cmd=f'"{sys.executable}" "{mock_script}"',
        idle_timeout_sec=30,
    )
    return DshCLIAdapter(agent_cfg, global_cfg)


def test_dsh_registry_and_public_display_contract():
    assert "dsh-cli" in registered_backend_engines()
    assert "dsh-cli" in CLI_ENGINES
    assert "dsh-cli" in SESSION_MODE_BACKENDS
    assert get_backend_class("dsh-cli") is DshCLIAdapter
    assert get_backend_class("DSH") is DshCLIAdapter
    assert get_backend_class("dsh") is DshCLIAdapter

    assert public_backend_engine("dsh-cli") == "DSH"
    assert canonical_backend_engine("DSH") == "dsh-cli"
    assert canonical_backend_engine("dsh") == "dsh-cli"
    assert get_backend_label("dsh-cli") == "DSH"

    models = get_available_models("dsh-cli")
    assert "deepseek-v4-pro" in models
    assert "deepseek-flash" in models
    assert get_available_efforts("dsh-cli") == ["off", "low", "high", "max"]
    assert normalize_effort("dsh-cli", "zero") == "off"
    assert normalize_effort("dsh-cli", "none") == "off"
    assert normalize_effort("dsh-cli", "max") == "max"


def test_dsh_model_and_effort_matching_helpers():
    assert _normalize_dsh_effort("zero") == "off"
    assert _normalize_dsh_effort("none") == "off"
    assert _normalize_dsh_effort("low") == "low"
    assert _normalize_dsh_effort("unknown") == "unknown"

    opts = [
        {
            "id": "model",
            "currentValue": '["deepseek-official","deepseek-v4-flash"]',
            "options": [
                {
                    "group": "deepseek-official",
                    "options": [
                        {"value": '["deepseek-official","deepseek-v4-pro"]', "name": "DeepSeek-V4-Pro"},
                        {"value": '["deepseek-official","deepseek-flash"]', "name": "DeepSeek-V4.1-Flash"},
                    ],
                }
            ],
        }
    ]
    matched, current = _match_model_option(opts, "deepseek-v4-pro")
    assert matched == '["deepseek-official","deepseek-v4-pro"]'
    assert current == '["deepseek-official","deepseek-v4-flash"]'


@pytest.mark.asyncio
async def test_dsh_acp_pcm_fixed_mode_tools_streaming_model_effort_and_stop(tmp_path: Path):
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(tmp_path, mock_script, session_mode=True, model="deepseek-v4-pro", effort="max")

    assert adapter.capabilities.supports_sessions is True
    assert adapter.capabilities.supports_thinking_stream is True
    assert adapter.capabilities.supports_commentary_stream is True
    assert adapter.capabilities.supports_tool_stream is True
    assert await adapter.initialize() is True

    try:
        # Turn 1: full tools + auto-permission + /think + /commentary + /verbose + model & effort sync
        events = []

        async def _collect(evt):
            events.append(evt)

        res1 = await adapter.generate_response("WITH_TOOLS inspect", "req-1", on_stream_event=_collect)
        assert res1.is_success is True
        assert "turn=1" in res1.text
        assert '["deepseek-official", "deepseek-v4-pro"]' in res1.text
        assert "effort=max" in res1.text
        assert res1.tool_call_count == 1
        assert (res1.stream_metadata or {}).get("context_usage") == {"used": 1234, "size": 1048576}
        thinking_meta = (res1.stream_metadata or {}).get("thinking") or {}
        assert thinking_meta.get("thinking_chars") == len("Need to read config first.")
        assert thinking_meta.get("thinking_event_count") == 1
        assert thinking_meta.get("thinking_redacted_count") == 0
        assert thinking_meta.get("thinking_sources") == ["dsh-cli"]

        kinds = [e.kind for e in events]
        assert KIND_THINKING in kinds
        assert KIND_COMMENTARY in kinds
        assert KIND_FILE_READ in kinds
        assert KIND_TOOL_END in kinds
        assert KIND_TEXT_DELTA in kinds

        thinking_evt = next(e for e in events if e.kind == KIND_THINKING)
        assert thinking_evt.delivery_class == DELIVERY_REASONING
        assert "Need to read config first." in thinking_evt.summary

        commentary_evt = next(e for e in events if e.kind == KIND_COMMENTARY)
        assert commentary_evt.delivery_class == DELIVERY_USER_COMMENTARY
        assert commentary_evt.summary == "Checking README now."

        tool_evt = next(e for e in events if e.kind == KIND_FILE_READ)
        assert tool_evt.delivery_class == DELIVERY_TECHNICAL
        assert tool_evt.file_path == "README.md"

        first_sid = adapter._session_id
        assert first_sid and first_sid.startswith("mock-sid-")

        # Turn 2: PCM fixed mode reuses SAME session & warm worker, dynamically switches model + effort ("zero" -> "off")
        adapter.config.model = "deepseek-flash"
        adapter.effort = "zero"
        res2 = await adapter.generate_response("incremental turn 2", "req-2")
        assert res2.is_success is True
        assert f"sid={first_sid}" in res2.text
        assert "turns=2" in res2.text
        assert '["deepseek-official", "deepseek-flash"]' in res2.text
        assert "effort=off" in res2.text

        # Turn 3: /stop graceful cancellation via interrupt_nowait + task cancel keeps session alive!
        cancel_task = asyncio.create_task(adapter.generate_response("HANG_FOR_STOP", "req-3"))
        await asyncio.sleep(0.25)
        assert adapter.interrupt_nowait("USER_STOP") is True
        cancel_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel_task

        # Turn 4 after /stop: SAME session continues cleanly without restart!
        res4 = await adapter.generate_response("after stop", "req-4")
        assert res4.is_success is True
        assert f"sid={first_sid}" in res4.text
        assert "turns=4" in res4.text

        # Worker unexpected exit recovery: session/resume preserves session_id!
        await adapter._stop_worker(reason="simulated_crash")
        assert adapter._session_id == first_sid
        res5 = await adapter.generate_response("after crash", "req-5")
        assert res5.is_success is True
        assert f"sid={first_sid}" in res5.text
        assert "resumed=True" in res5.text

        # /new resets session via session/close and opens a brand new session id
        assert await adapter.handle_new_session() is True
        assert adapter._session_id is None
        res6 = await adapter.generate_response("brand new session", "req-6")
        assert res6.is_success is True
        assert f"sid={first_sid}" not in res6.text
        assert "resumed=False" in res6.text
        assert "turns=1" in res6.text
    finally:
        await adapter.shutdown()


def test_dsh_session_projcache_usage_helpers(tmp_path: Path, monkeypatch):
    from adapters import dsh_cli as dsh

    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    sessions = tmp_path / "storages" / "session_projcache" / "sessions"
    assert dsh._read_dsh_session_totals("missing-session") is None

    sessions.mkdir(parents=True)
    (sessions / "broken.json").write_text("{not json", encoding="utf-8")
    assert dsh._read_dsh_session_totals("broken") is None

    totals = {
        "uncachedInputTokens": 1_200,
        "cacheReadTokens": 3_400,
        "cacheWriteTokens": 0,
        "outputTokens": 560,
    }
    (sessions / "sid-a.json").write_text(
        json.dumps(
            {"version": 1, "record": {"rows": {"tokenUsage": {"val": {"totals": totals}}}}}
        ),
        encoding="utf-8",
    )
    assert dsh._read_dsh_session_totals("sid-a") == totals

    # A brand-new session has no prior snapshot; the whole total is this turn.
    assert dsh._dsh_turn_usage(None, totals, fresh_session=True) == totals
    # An existing session without a baseline holds cumulative history, so a
    # missing snapshot must never be priced as this turn's provider usage.
    assert dsh._dsh_turn_usage(None, totals) is None
    # Re-reading an unchanged snapshot is not a turn.
    assert dsh._dsh_turn_usage(totals, totals, revision_advanced=False) is None
    # A revision bump is required before an existing session's delta is trusted.
    grown = {**totals, "outputTokens": totals["outputTokens"] + 10}
    assert dsh._dsh_turn_usage(totals, grown) is None
    assert dsh._dsh_turn_usage(totals, grown, revision_advanced=True) == {
        "uncachedInputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "outputTokens": 10,
    }
    # Compaction or session reset must never fabricate a negative turn.
    shrunk = dict(totals)
    shrunk["cacheReadTokens"] = 0
    assert dsh._dsh_turn_usage(totals, shrunk, revision_advanced=True) is None
    # Unreadable store keeps the estimate path.
    assert dsh._dsh_turn_usage(totals, None) is None


@pytest.mark.asyncio
async def test_dsh_usage_wait_is_bounded_when_the_store_never_updates(
    tmp_path: Path, monkeypatch
):
    from adapters import dsh_cli as dsh

    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    started = time.monotonic()
    assert await dsh._await_dsh_usage_snapshot("no-such-session", None) is None
    assert time.monotonic() - started < 2.0

    sessions = tmp_path / "storages" / "session_projcache" / "sessions"
    sessions.mkdir(parents=True)
    totals = {
        "uncachedInputTokens": 10,
        "cacheReadTokens": 20,
        "cacheWriteTokens": 0,
        "outputTokens": 30,
    }
    (sessions / "sid-stale.json").write_text(
        json.dumps(
            {
                "version": 1,
                "record": {
                    "rows": {
                        "tokenUsage": {
                            "ver": 2,
                            "seq": 7,
                            "val": {"totals": totals},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    baseline = dsh._read_dsh_session_snapshot("sid-stale")
    assert baseline == (7, totals)
    started = time.monotonic()
    stale = await dsh._await_dsh_usage_snapshot("sid-stale", baseline)
    assert stale == baseline
    assert time.monotonic() - started < 2.0
    # No new revision means no new turn: a late flush of the previous turn must
    # not be reported as this turn's provider usage.
    assert dsh._dsh_turn_usage(baseline[1], stale[1], revision_advanced=False) is None


@pytest.mark.asyncio
async def test_dsh_acp_turn_usage_comes_from_session_projcache(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(
        tmp_path, mock_script, session_mode=True, model="deepseek-flash", effort="off"
    )
    assert await adapter.initialize() is True

    try:
        res1 = await adapter.generate_response("usage turn one", "req-u1")
        assert res1.is_success is True
        assert res1.usage is not None
        assert res1.usage.input_tokens == 1_500
        assert res1.usage.output_tokens == 200
        assert res1.usage.prompt_cache_hit_tokens == 500
        assert res1.usage.prompt_cache_miss_tokens == 1_000
        assert res1.usage.thinking_tokens == 0
        assert (res1.stream_metadata or {}).get("token_usage_source") == (
            "dsh_session_projcache"
        )

        # Turn 2 reports only the increment over the cumulative session totals.
        res2 = await adapter.generate_response("usage turn two", "req-u2")
        assert res2.usage is not None
        assert res2.usage.input_tokens == 1_500
        assert res2.usage.output_tokens == 200
        assert res2.usage.prompt_cache_hit_tokens == 500
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_dsh_resumed_session_without_baseline_keeps_estimate(
    tmp_path: Path, monkeypatch
):
    """CRC-225-01: an existing session must not price its whole history as one turn."""

    from adapters import dsh_cli as dsh

    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(
        tmp_path, mock_script, session_mode=True, model="deepseek-flash", effort="off"
    )
    assert await adapter.initialize() is True

    try:
        res1 = await adapter.generate_response("usage turn one", "req-crc-u1")
        assert res1.is_success is True
        assert res1.usage is not None

        real_read = dsh._read_dsh_session_snapshot
        calls = {"count": 0}

        def flaky_read(session_id):
            calls["count"] += 1
            if calls["count"] == 1:
                # Existing session, but the pre-turn snapshot is unreadable.
                return None
            return real_read(session_id)

        monkeypatch.setattr(dsh, "_read_dsh_session_snapshot", flaky_read)

        started = time.monotonic()
        res2 = await adapter.generate_response("usage turn two", "req-crc-u2")
        # No baseline means no wait and no provider usage claim.
        assert time.monotonic() - started < 5.0
        assert res2.is_success is True
        assert res2.usage is None
        assert (res2.stream_metadata or {}).get("token_usage_source") == "estimated"
    finally:
        await adapter.shutdown()


def test_dsh_preflight_and_local_invocation_resolution():
    argv, cwd, label = resolve_dsh_invocation("dsh")
    assert "--profile" in argv
    assert "acp" in argv
    if Path(label).is_file():
        global_cfg = SimpleNamespace(
            claude_cmd="claude",
            codex_cmd="codex",
            agy_cmd="agy",
            grok_cmd="grok",
            dsh_cmd="dsh",
        )
        gw = check_gateway_engine(global_cfg, {}, "dsh-cli")
        assert gw["available"] is True
        agent_cfg = SimpleNamespace(active_backend="dsh-cli", allowed_backends=[{"engine": "dsh-cli"}])
        pf = BackendPreflight().check_backend_availability(global_cfg, [agent_cfg], {})
        assert pf["dsh-cli"][0] is True


def test_dsh_config_loader_pcm_fixed_mode_and_workbench_catalogue(tmp_path: Path):
    from orchestrator.agent_creation_policy import build_effective_backend_catalogue
    from orchestrator.config import ConfigManager

    ws = tmp_path / "workspaces" / "dsh_agent"
    ws.mkdir(parents=True)
    (ws / "PCM.md").write_text("# Identity\nDSH test agent.\n", encoding="utf-8")
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "agents.json"
    cfg_path.write_text(
        json.dumps(
            {
                "global": {"workbench_port": 18899},
                "agents": [
                    {
                        "name": "dsh_agent",
                        "type": "flex",
                        "default_mode": "fixed",
                        "active_backend": "DSH",
                        "workspace_dir": "workspaces/dsh_agent",
                        "system_md": "workspaces/dsh_agent/PCM.md",
                        "allowed_backends": [
                            {"engine": "DSH", "model": "deepseek-v4-pro", "effort": "high"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    global_cfg, agents, _secrets = ConfigManager(cfg_path, tmp_path / "secrets.json").load()
    assert len(agents) == 1
    ag = agents[0]
    assert ag.default_mode == "fixed"
    assert ag.active_backend == "dsh-cli"
    assert public_backend_engine(ag.active_backend) == "DSH"

    cat = build_effective_backend_catalogue(
        ag.allowed_backends,
        {},
        availability={"dsh-cli": (True, global_cfg.dsh_cmd)},
    )
    assert "DSH" in cat
    assert cat["DSH"]["engine"] == "DSH"
    assert cat["DSH"]["label"] == "DSH"
    assert cat["DSH"]["available"] is True
    assert cat["DSH"]["efforts"] == ["off", "low", "high", "max"]
    assert "deepseek-v4-pro" in cat["DSH"]["models"]
    assert "deepseek-flash" in cat["DSH"]["models"]


@pytest.mark.asyncio
async def test_dsh_p1_01_run_scoped_hashi_mcp_lifecycle_preserves_warm_worker_and_session(
    tmp_path: Path, monkeypatch
):
    from adapters import dsh_cli
    from adapters.hashi_mcp import HashiMcpInvocation

    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(tmp_path, mock_script, session_mode=True, model="deepseek-v4-pro", effort="high")
    adapter.tool_registry = SimpleNamespace(allowed_tool_names=lambda: ["memory_search"])

    issued: list[HashiMcpInvocation] = []

    def fake_prepare_hashi_mcp(target_adapter, *, backend="dsh-cli", audit_context=None):
        idx = len(issued) + 1
        ctx_path = tmp_path / f"run-ctx-{idx}.json"
        ctx_path.write_text(json.dumps({"run": f"run-{idx}"}), encoding="utf-8")
        inv = HashiMcpInvocation(
            {
                "name": "hashi_tools",
                "command": sys.executable,
                "args": ["-B", "-m", "tools.gateway.mcp_stdio", "--context", str(ctx_path)],
                "cwd": str(tmp_path),
                "context_path": str(ctx_path),
            },
            owner=target_adapter,
        )
        target_adapter._hashi_mcp_enabled = True
        issued.append(inv)
        return inv

    monkeypatch.setattr(dsh_cli, "prepare_hashi_mcp", fake_prepare_hashi_mcp)

    try:
        # Run 1
        res1 = await adapter.generate_response("run 1 prompt", "req-mcp-1")
        assert res1.is_success is True
        assert len(issued) == 1
        assert issued[0]._closed is True, "Run 1 MCP invocation must be closed when Run 1 finishes"
        assert not Path(issued[0]["context_path"]).exists()
        assert "run-ctx-1.json" in res1.text
        sid1 = adapter._session_id
        pid1 = adapter._proc.pid
        assert adapter._session_in_current_worker is False

        # Run 2: same warm ACP worker PID + same persisted sessionId via session/resume + fresh Run 2 MCP context!
        res2 = await adapter.generate_response("run 2 prompt", "req-mcp-2")
        assert res2.is_success is True
        assert len(issued) == 2
        assert issued[1]._closed is True, "Run 2 MCP invocation must be closed when Run 2 finishes"
        assert not Path(issued[1]["context_path"]).exists()
        assert adapter._proc.pid == pid1, "Warm ACP worker must stay alive across Run-scoped MCP turns"
        assert adapter._session_id == sid1, "Persisted DSH session ID must stay continuous across Runs"
        assert "resumed=True" in res2.text
        assert "run-ctx-2.json" in res2.text
        assert "run-ctx-1.json" not in res2.text
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_dsh_p1_02_failed_session_resume_does_not_send_incremental_prompt_to_fresh_session(
    tmp_path: Path,
):
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(tmp_path, mock_script, session_mode=True, model="deepseek-v4-pro", effort="high")

    try:
        # Establish normal session first
        res1 = await adapter.generate_response("seed PCM", "req-p102-1")
        assert res1.is_success is True

        # Simulate stale/unresumable persisted session after worker restart
        await adapter._stop_worker(reason="simulated_restart")
        adapter._session_id = "unresumable-dead-sid"

        # Incremental prompt MUST fail explicitly instead of silently calling session/new
        res2 = await adapter.generate_response("incremental followup", "req-p102-2")
        assert res2.is_success is False
        assert res2.error_code == "dsh_session_resume_failed"
        assert res2.error_retryable is True
        assert adapter._session_id is None, "Failed resume must clear _session_id so next request rebuilds full PCM"

        # Subsequent request (now non-incremental with _session_id=None) opens a fresh session cleanly
        res3 = await adapter.generate_response("full pcm re-init", "req-p102-3")
        assert res3.is_success is True
        assert adapter._session_id is not None
        assert "resumed=False" in res3.text
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_dsh_p1_03_truthful_model_and_effort_config_and_custom_model_support(tmp_path: Path):
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(tmp_path, mock_script, session_mode=True, model="deepseek-v4-pro", effort="high")

    try:
        # Unmatched model on Turn 1 MUST fail truthfully AND leave _session_id=None
        # so the next request rebuilds full PCM context instead of sending incremental.
        fresh_bad = _make_adapter(tmp_path, mock_script, session_mode=True, model="nonexistent-first-turn", effort="high")
        try:
            res_first_bad = await fresh_bad.generate_response("first turn bad model", "req-p103-0")
            assert res_first_bad.is_success is False
            assert res_first_bad.error_code == "dsh_model_unavailable"
            assert fresh_bad._session_id is None
        finally:
            await fresh_bad.shutdown()

        res_ok = await adapter.generate_response("hello", "req-p103-1")
        assert res_ok.is_success is True
        established_sid = adapter._session_id
        assert established_sid is not None
        assert (res_ok.stream_metadata or {}).get("effective_model") == "deepseek-official/deepseek-v4-pro"
        assert (res_ok.stream_metadata or {}).get("effective_effort") == "high"

        # Unmatched model MUST fail truthfully without silently running on previous model,
        # while preserving the already-established session_id for recovery on next turn.
        adapter.config.model = "nonexistent-model-999"
        res_bad_model = await adapter.generate_response("should not run", "req-p103-2")
        assert res_bad_model.is_success is False
        assert res_bad_model.error_code == "dsh_model_unavailable"
        assert res_bad_model.error_retryable is False
        assert "nonexistent-model-999" in (res_bad_model.error or "")
        assert (res_bad_model.stream_metadata or {}).get("effective_model") == "deepseek-official/deepseek-v4-pro"
        assert adapter._session_id == established_sid

        # Custom model exposed by DSH live catalog works without any hardcoded allowlist
        adapter.config.model = "my-custom-v9"
        res_custom = await adapter.generate_response("custom model turn", "req-p103-3")
        assert res_custom.is_success is True
        assert (res_custom.stream_metadata or {}).get("effective_model") == "custom-provider/my-custom-v9"

        # Unsupported effort MUST fail truthfully without silently coercing to high
        adapter.effort = "xhigh"
        res_bad_effort = await adapter.generate_response("should not run effort", "req-p103-4")
        assert res_bad_effort.is_success is False
        assert res_bad_effort.error_code == "dsh_effort_unavailable"
        assert res_bad_effort.error_retryable is False
        assert "xhigh" in (res_bad_effort.error or "")
        assert (res_bad_effort.stream_metadata or {}).get("effective_effort") == "high"
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_dsh_cli_initialize_never_spawns_a_help_probe(tmp_path: Path, monkeypatch):
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    real_exec = asyncio.create_subprocess_exec
    spawns: list[tuple] = []

    async def counting_exec(*args, **kwargs):
        spawns.append(tuple(args))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    adapter = _make_adapter(tmp_path, mock_script)
    assert await adapter.initialize() is True
    # An idle interval past the former 600s probe TTL must stay probe-free.
    monkeypatch.setattr("adapters.dsh_cli.time.monotonic", lambda: 10_000_000.0)
    assert await adapter.initialize() is True
    assert spawns == []
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_dsh_cli_initialize_reports_missing_entry_without_spawning(tmp_path: Path, monkeypatch):
    missing = tmp_path / "missing_dsh_entry.js"
    agent_cfg = SimpleNamespace(
        name="dsh-test-agent",
        engine="dsh-cli",
        model="deepseek-v4-pro",
        workspace_dir=tmp_path / "workspace",
        extra={"session_mode": True, "effort": "high"},
        access_scope="drive",
    )
    global_cfg = SimpleNamespace(
        dsh_cmd=f'"{sys.executable}" "{missing}"',
        idle_timeout_sec=30,
    )
    adapter = DshCLIAdapter(agent_cfg, global_cfg)

    async def fail_exec(*args, **kwargs):
        raise AssertionError("initialize must not spawn the CLI")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)
    assert await adapter.initialize() is False
    assert "launch script not found" in adapter.last_initialize_error
    assert str(missing) in adapter.last_initialize_error


def test_dsh_cli_reports_images_not_transmitted_for_legacy_and_canonical_content(tmp_path: Path, caplog):
    mock_script = tmp_path / "mock_dsh_acp.py"
    mock_script.write_text(_MOCK_ACP_SERVER_SCRIPT, encoding="utf-8")
    adapter = _make_adapter(tmp_path, mock_script)
    assert adapter._image_prompts_supported is False
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    canonical = {
        "version": 1,
        "parts": [
            {
                "type": "media",
                "item_index": 1,
                "attachment_id": "att-1",
                "modality": "image",
                "mime_type": "image/png",
                "local_ref": str(image),
                "size_bytes": 8,
                "sha256": "a" * 64,
            },
        ],
    }
    caplog.set_level("WARNING")

    # Legacy compatibility shape still reports the skipped attachment.
    blocks = adapter._build_prompt_blocks("hello", {"attachments": [{"path": str(image)}]})
    assert blocks == [{"type": "text", "text": "hello"}]
    assert "did not transmit 1 image attachment(s) (canonical=0, legacy=1" in caplog.text

    # Canonical production content (contract v1 media part) is reported too.
    caplog.clear()
    blocks = adapter._build_prompt_blocks("hello", canonical)
    assert blocks == [{"type": "text", "text": "hello"}]
    assert "did not transmit 1 image attachment(s) (canonical=1, legacy=0" in caplog.text

    # Legacy images stay transmitted when prompts were advertised; canonical
    # images still have no DSH transport and remain reported.
    caplog.clear()
    adapter._image_prompts_supported = True
    legacy_blocks = adapter._build_prompt_blocks("hello", {"attachments": [{"path": str(image)}]})
    assert any(block.get("type") == "image" for block in legacy_blocks)
    assert "did not transmit" not in caplog.text
    adapter._build_prompt_blocks("hello", canonical)
    assert "did not transmit 1 image attachment(s) (canonical=1, legacy=0" in caplog.text
# HASHI-Fingerprint: hfp_1ae9b92de86a47c898f9d4c4833d4020
