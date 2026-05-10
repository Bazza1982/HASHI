from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_remote


def _runtime(tmp_path: Path):
    return SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path),
        config=SimpleNamespace(agent_name="lin_yueru"),
        _remote_config_snapshot=lambda: runtime_remote.remote_config_snapshot(
            SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path), config=SimpleNamespace(agent_name="lin_yueru"))
        ),
        _remote_urls=lambda path: runtime_remote.remote_urls(
            SimpleNamespace(
                _remote_config_snapshot=lambda: {
                    "port": 8766,
                    "use_tls": True,
                    "backend": "lan",
                }
            ),
            path,
        ),
        _read_remote_start_log_excerpt=lambda path: runtime_remote.read_remote_start_log_excerpt(path),
        _build_remote_start_failure_message=lambda **kwargs: runtime_remote.build_remote_start_failure_message(
            SimpleNamespace(_read_remote_start_log_excerpt=lambda path: runtime_remote.read_remote_start_log_excerpt(path)),
            **kwargs,
        ),
    )


def test_remote_config_snapshot_reads_local_config(tmp_path: Path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "server:\n  port: 9999\n  use_tls: false\ndiscovery:\n  backend: tailscale\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.json").write_text(json.dumps({"global": {"instance_id": "hashi2"}}), encoding="utf-8")
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi2": {"remote_port": 18888}}}),
        encoding="utf-8",
    )
    cfg = runtime_remote.remote_config_snapshot(SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path)))
    assert cfg["port"] == 18888
    assert cfg["use_tls"] is False
    assert cfg["backend"] == "tailscale"


def test_remote_urls_respect_tls_order(tmp_path: Path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "server:\n  port: 8766\n  use_tls: true\ndiscovery:\n  backend: lan\n",
        encoding="utf-8",
    )
    runtime = SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path))
    assert runtime_remote.remote_urls(runtime, "/health") == [
        "https://127.0.0.1:8766/health",
        "http://127.0.0.1:8766/health",
    ]


def test_remote_backend_block_reason_blocks_automated_api_usage():
    runtime = SimpleNamespace(config=SimpleNamespace(active_backend="openrouter-api"))
    reason = runtime_remote.remote_backend_block_reason(runtime, "scheduler")
    assert reason is not None
    assert "user-initiated requests only" in reason


def test_remote_backend_block_reason_allows_manual_sources():
    runtime = SimpleNamespace(config=SimpleNamespace(active_backend="openrouter-api"))
    assert runtime_remote.remote_backend_block_reason(runtime, "telegram") is None


def test_remote_peer_presence_marks_live_online():
    runtime = SimpleNamespace(_format_remote_age=lambda timestamp: "1s ago")
    peer = {"properties": {"live_status": "online", "handshake_state": "handshake_accepted"}}
    rank, label, state = runtime_remote.remote_peer_presence(runtime, peer)
    assert rank == 0
    assert "online" in label
    assert state == "handshake_accepted"


def test_render_remote_peer_endpoints_prefers_same_host_network_hint(tmp_path: Path):
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi9": {"same_host_loopback": "127.0.0.1", "lan_ip": "192.168.0.9", "remote_port": 8766}}}),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path))
    peer = {
        "instance_id": "hashi9",
        "resolved_route_host": "127.0.0.1",
        "resolved_route_port": 8766,
        "same_host": True,
        "properties": {},
    }
    lines = runtime_remote.render_remote_peer_endpoints(runtime, peer)
    assert "same host" in lines[0]
    assert "192.168.0.9:8766" in lines[0]


@pytest.mark.asyncio
async def test_handle_remote_backend_block_sends_warning():
    sent = []
    warnings = []
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="openrouter-api"),
        error_logger=SimpleNamespace(warning=lambda message: warnings.append(message)),
        send_long_message=lambda chat_id, text, request_id, purpose: _record_remote_send(
            sent, chat_id, text, request_id, purpose
        ),
    )
    item = SimpleNamespace(
        source="scheduler",
        deliver_to_telegram=True,
        chat_id=123,
        request_id="req-1",
    )

    blocked = await runtime_remote.handle_remote_backend_block(runtime, item)

    assert blocked is True
    assert warnings
    assert sent[0]["purpose"] == "remote-backend-policy"


def test_remote_start_log_helpers(tmp_path: Path):
    runtime = _runtime(tmp_path)
    path = runtime_remote.remote_start_log_path(runtime)
    path.write_text("hello\nworld", encoding="utf-8")
    assert path.name == "lin_yueru_remote_startup.log"
    assert runtime_remote.read_remote_start_log_excerpt(path, max_chars=5) == "world"


def test_build_remote_start_failure_message_includes_excerpt(tmp_path: Path):
    runtime = _runtime(tmp_path)
    log_path = tmp_path / "tmp" / "remote.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("last line", encoding="utf-8")
    text = runtime_remote.build_remote_start_failure_message(
        runtime,
        cfg={"port": 8766, "use_tls": True, "backend": "lan"},
        cmd=["python", "remote/main.py"],
        reason="boom",
        log_path=log_path,
        exit_code=1,
    )
    assert "Hashi Remote failed to start" in text
    assert "last line" in text


@pytest.mark.asyncio
async def test_await_remote_start_health_reports_early_exit(tmp_path: Path):
    runtime = _runtime(tmp_path)
    process = SimpleNamespace(returncode=3)
    ok, detail = await runtime_remote.await_remote_start_health(
        runtime,
        process=process,
        cfg={"port": 8766, "use_tls": True, "backend": "lan"},
        cmd=["python", "-m", "remote"],
        log_path=tmp_path / "tmp" / "remote.log",
        timeout_s=0.1,
    )
    assert ok is False
    assert "process exited before /health became ready" in detail


@pytest.mark.asyncio
async def test_fetch_remote_json_returns_none_when_all_attempts_fail(monkeypatch):
    monkeypatch.setattr(runtime_remote, "remote_urls", lambda runtime, path: ["http://127.0.0.1:1/health"])
    runtime = SimpleNamespace()
    data, url = await runtime_remote.fetch_remote_json(runtime, "/health")
    assert data is None
    assert url is None


@pytest.mark.asyncio
async def test_cmd_remote_status_reports_not_running(tmp_path: Path, monkeypatch):
    replies = []
    monkeypatch.setattr(runtime_remote, "fetch_remote_json", _fake_fetch_none_runtime)
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _remote_process=None,
        _reply_text=lambda update, text, parse_mode=None: _fake_reply(replies, text),
        global_config=SimpleNamespace(project_root=tmp_path),
    )
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "server:\n  port: 8766\n  use_tls: true\ndiscovery:\n  backend: lan\n",
        encoding="utf-8",
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=["status"])

    await runtime_remote.cmd_remote(runtime, update, context)

    assert replies == ["⚪ Hashi Remote is not running. Use /remote on to start."]


async def _fake_fetch_none(path: str):
    return None, None


async def _fake_fetch_none_runtime(runtime, path: str):
    return None, None


async def _fake_reply(replies: list[str], text: str):
    replies.append(text)


async def _record_remote_send(sent: list[dict[str, str]], chat_id: int, text: str, request_id: str, purpose: str):
    sent.append(
        {
            "chat_id": chat_id,
            "text": text,
            "request_id": request_id,
            "purpose": purpose,
        }
    )
    return (0.0, 1)
