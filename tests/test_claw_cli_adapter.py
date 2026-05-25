from __future__ import annotations

import os
import stat
import textwrap
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.claw_cli import (
    ClawCLIAdapter,
    ClawBinaryNotFound,
    ClawCommandError,
    ClawJsonError,
    ClawPackagedRuntimeError,
    ClawProviderSecretMissing,
    ClawTimeoutError,
    build_claw_task_args,
    build_claw_env,
    detect_hashi_claw_platform,
    discover_claw_binary,
    find_claw_binary,
    load_packaged_claw_manifest,
    resolve_packaged_claw_binary,
    run_claw_doctor,
    run_claw_json_command,
    run_claw_task,
)
from adapters.registry import get_backend_class
from adapters.stream_events import KIND_TEXT_DELTA, KIND_THINKING, KIND_TOOL_END, KIND_TOOL_START
from orchestrator.flexible_backend_registry import allows_custom_models, get_secret_lookup_order, is_cli_backend


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _write_packaged_claw(
    root: Path,
    *,
    platform_key: str = "linux-x86_64",
    rust_target_triple: str = "x86_64-unknown-linux-gnu",
    body: str = "#!/usr/bin/env python3\nprint('ok')\n",
) -> Path:
    (root / "bin" / platform_key).mkdir(parents=True, exist_ok=True)
    binary = _write_exe(root / "bin" / platform_key / "hashi-claw", body)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "runtime": "hashi-claw",
                "version": "0.0.0-test",
                "binaries": {
                    platform_key: {
                        "path": str(binary.relative_to(root)),
                        "binary_name": "hashi-claw",
                        "rust_target_triple": rust_target_triple,
                        "sha256": digest,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return binary


def test_find_claw_binary_accepts_configured_executable(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )

    assert find_claw_binary(fake) == fake.resolve()


def test_detect_hashi_claw_platform_linux_wsl_candidate():
    platform = detect_hashi_claw_platform(
        system="Linux",
        machine="x86_64",
        release="6.6.0-microsoft-standard-WSL2",
    )

    assert platform.key == "linux-x86_64"
    assert platform.rust_target_triple == "x86_64-unknown-linux-gnu"
    assert platform.is_wsl is True
    assert platform.candidate_keys == ("linux-x86_64-wsl", "linux-x86_64")


def test_load_packaged_claw_manifest_rejects_non_hashi_runtime(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"manifest_version": 1, "runtime": "claw", "version": "1", "binaries": {}}),
        encoding="utf-8",
    )

    with pytest.raises(ClawPackagedRuntimeError, match="hashi-claw"):
        load_packaged_claw_manifest(manifest)


def test_resolve_packaged_claw_binary_validates_checksum(tmp_path):
    root = tmp_path / "hashi_assets" / "claw"
    binary = _write_packaged_claw(root)
    platform = detect_hashi_claw_platform(system="Linux", machine="x86_64", release="6.8")

    resolved = resolve_packaged_claw_binary(root, platform=platform)

    assert resolved.path == binary.resolve()
    assert resolved.source == "packaged"
    assert resolved.packaged_version == "0.0.0-test"


def test_find_claw_binary_uses_packaged_runtime_before_env(tmp_path):
    root = tmp_path / "hashi_assets" / "claw"
    packaged = _write_packaged_claw(root)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    assert find_claw_binary(global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""}) == packaged.resolve()


def test_find_claw_binary_checksum_mismatch_falls_back_to_env(tmp_path):
    root = tmp_path / "hashi_assets" / "claw"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    env_claw = _write_exe(
        tmp_path / "env-claw",
        """
        #!/usr/bin/env python3
        print("env")
        """,
    )
    global_cfg = SimpleNamespace(project_root=tmp_path)

    resolved = discover_claw_binary(global_config=global_cfg, env={"CLAW_BINARY": str(env_claw), "PATH": ""})

    assert resolved.path == env_claw.resolve()
    assert resolved.source == "env:CLAW_BINARY"
    assert any("checksum mismatch" in warning for warning in resolved.warnings)


def test_find_claw_binary_require_packaged_fails_closed(tmp_path):
    root = tmp_path / "hashi_assets" / "claw"
    packaged = _write_packaged_claw(root)
    packaged.write_text("#!/usr/bin/env python3\nprint('tampered')\n", encoding="utf-8")
    packaged.chmod(packaged.stat().st_mode | stat.S_IXUSR)
    global_cfg = SimpleNamespace(project_root=tmp_path)
    agent_cfg = SimpleNamespace(extra={"claw_runtime_policy": "require-packaged"})

    with pytest.raises(ClawBinaryNotFound, match="required but unavailable"):
        find_claw_binary(global_config=global_cfg, agent_config=agent_cfg, env={"PATH": ""})


def test_find_claw_binary_reports_missing_configured_path(tmp_path):
    with pytest.raises(ClawBinaryNotFound):
        find_claw_binary(tmp_path / "missing", env={"PATH": ""})


def test_find_claw_binary_accepts_global_claw_provider_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )
    global_cfg = SimpleNamespace(claw_providers={"binary_path": str(fake)})

    assert find_claw_binary(global_config=global_cfg, env={"PATH": ""}) == fake.resolve()


def test_build_claw_env_uses_allowlist_only():
    env = build_claw_env(
        {
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "must-not-pass",
            "HASHI_REMOTE_SHARED_TOKEN": "must-not-pass",
            "HOME": "/tmp/home",
            "PATH": "/bin",
        }
    )

    assert env == {
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_API_KEY": "secret",
        "HOME": "/tmp/home",
        "PATH": "/bin",
    }


def test_run_claw_doctor_parses_json(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "doctor", "status": "ok"}))
        """,
    )

    assert run_claw_doctor(tmp_path, binary_path=fake) == {"kind": "doctor", "status": "ok"}


def test_run_claw_json_command_raises_for_non_zero_json_error(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        print(json.dumps({"error": "bad key", "kind": "api_http_error"}), file=sys.stderr)
        raise SystemExit(1)
        """,
    )

    with pytest.raises(ClawCommandError) as raised:
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake)

    assert raised.value.returncode == 1
    assert raised.value.parsed_error == {"error": "bad key", "kind": "api_http_error"}


def test_run_claw_json_command_raises_for_non_json_output(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("not json")
        """,
    )

    with pytest.raises(ClawJsonError):
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake)


def test_run_claw_json_command_timeout(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import time
        time.sleep(2)
        """,
    )

    with pytest.raises(ClawTimeoutError):
        run_claw_json_command(["doctor", "--output-format", "json"], cwd=tmp_path, binary_path=fake, timeout_s=0.1)


def test_run_claw_task_builds_safe_one_shot_command(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        assert "--permission-mode" in sys.argv
        assert "read-only" in sys.argv
        assert "--allowedTools" in sys.argv
        assert "read,glob" in sys.argv
        print(json.dumps({
          "message": "done",
          "model": "deepseek/test",
          "iterations": 2,
          "estimated_cost": "$0.0001",
          "tool_uses": [{"name": "read_file"}],
          "tool_results": [{"is_error": False}]
        }))
        """,
    )

    result = run_claw_task(
        tmp_path,
        "inspect",
        "deepseek/test",
        permission_mode="read-only",
        allowed_tools=["read", "glob"],
        binary_path=fake,
    )

    assert result.text == "done"
    assert result.model == "deepseek/test"
    assert result.permission_mode == "read-only"
    assert result.iterations == 2
    assert result.tool_uses == [{"name": "read_file"}]
    assert result.tool_results == [{"is_error": False}]


def test_run_claw_task_rejects_invalid_permission_mode(tmp_path):
    with pytest.raises(ValueError, match="permission_mode"):
        run_claw_task(tmp_path, "prompt", "model", permission_mode="root")


def test_build_claw_task_args_matches_cli_shape():
    assert build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        resume="latest",
        allowed_tools=["read"],
        skip_permissions=True,
    ) == [
        "--model",
        "deepseek/test",
        "--permission-mode",
        "read-only",
        "--output-format",
        "json",
        "--allowedTools",
        "read",
        "--dangerously-skip-permissions",
        "--resume",
        "latest",
        "prompt",
        "hello",
    ]


def test_build_claw_task_args_accepts_stream_json():
    args = build_claw_task_args(
        "hello",
        "deepseek/test",
        permission_mode="read-only",
        output_format="stream-json",
    )

    assert args[args.index("--output-format") + 1] == "stream-json"


def test_registry_exposes_claw_backend():
    assert get_backend_class("claw-cli") is ClawCLIAdapter
    assert is_cli_backend("claw-cli")
    assert allows_custom_models("claw-cli")
    assert not allows_custom_models("codex-cli")
    assert "openrouter_key" in get_secret_lookup_order("claw-cli", "ying")


def test_claw_provider_env_resolves_secret_and_base_url(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="openrouter:deepseek/test",
        extra={},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={"openrouter_key": "provider-secret"},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                    "status": "stable",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._claw_model() == "deepseek/test"
    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://openrouter.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "provider-secret"


def test_claw_provider_missing_secret_raises(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    with pytest.raises(ClawProviderSecretMissing):
        adapter._task_env()


def test_claw_provider_legacy_env_fallback(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"openai_base_url": "https://legacy.invalid/v1"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(claw_providers={})
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="legacy-secret")

    assert adapter._task_env()["OPENAI_BASE_URL"] == "https://legacy.invalid/v1"
    assert adapter._task_env()["OPENAI_API_KEY"] == "legacy-secret"


def test_claw_provider_ollama_dummy_key_is_not_redacted(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="ollama:qwen2.5-coder:32b",
        extra={},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "ollama": {
                    "base_url": "http://localhost:11434/v1",
                    "secret": None,
                    "dummy_api_key": "__ollama_dummy__",
                    "status": "provisional",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert adapter._claw_model() == "qwen2.5-coder:32b"
    assert adapter._task_env()["OPENAI_API_KEY"] == "__ollama_dummy__"


def test_claw_permission_mode_respects_global_max(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"permission_mode": "danger-full-access"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace(claw_providers={"max_permission_mode": "workspace-write"})
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert adapter._permission_mode() == "workspace-write"


@pytest.mark.asyncio
async def test_claw_adapter_degrades_when_binary_missing(tmp_path):
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(tmp_path / "missing")},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_claw_adapter_degrades_when_provider_secret_missing(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json
        print(json.dumps({"kind": "version", "version": "0.1.0"}))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "provider": "openrouter"},
        resolve_access_root=lambda: tmp_path,
        _hashi_secrets={},
    )
    global_cfg = SimpleNamespace(
        claw_providers={
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.invalid/v1",
                    "secret": "openrouter_key",
                }
            }
        }
    )
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key=None)

    assert await adapter.initialize() is False


@pytest.mark.asyncio
async def test_claw_adapter_generate_response_with_fake_binary(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            print(json.dumps({
              "message": "adapter done",
              "model": "deepseek/test",
              "iterations": 1,
              "tool_uses": [],
              "tool_results": [],
              "usage": {"input_tokens": 3, "output_tokens": 2}
            }))
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")

    assert await adapter.initialize() is True
    response = await adapter.generate_response("hello", "req-1")

    assert response.is_success is True
    assert response.text == "adapter done"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_verbose_events(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            assert "stream-json" in sys.argv
            for event in [
                {"kind": "run_started", "model": "deepseek/test"},
                {"kind": "thinking_summary", "summary": "thinking block received (48 chars hidden)", "thinking_chars": 48},
                {"kind": "assistant_delta", "text": "partial answer"},
                {"kind": "tool_start", "name": "read_file", "summary": "reading README.md"},
                {"kind": "tool_end", "name": "read_file", "summary": "read_file completed", "output_preview": "ok"},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7, "thinking_token_source": "estimated"},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "tool_uses": [{"name": "read_file"}], "tool_results": [],
                 "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
                time.sleep(0.01)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response("hello", "req-stream", on_stream_event=collect)

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 7
    assert response.usage.thinking_tokens == 12
    assert adapter.capabilities.supports_thinking_stream is True
    assert KIND_THINKING in [event.kind for event in events]
    assert any(
        event.kind == KIND_THINKING and "Claw stream started" in event.summary
        for event in events
    )
    assert KIND_TEXT_DELTA in [event.kind for event in events]
    assert KIND_TOOL_START in [event.kind for event in events]
    assert KIND_TOOL_END in [event.kind for event in events]
    assert any(event.detail == "thinking_chars=48" for event in events)
    assert not any("may be summarized or hidden" in event.summary for event in events)


@pytest.mark.asyncio
async def test_claw_adapter_stream_json_emits_actual_thinking_delta(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys
        if "--help" in sys.argv:
            print("Usage: claw [--output-format text|json|stream-json] prompt TEXT")
        elif sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0", "git_sha": "fake"}))
        else:
            for event in [
                {"kind": "run_started", "model": "deepseek/test"},
                {"kind": "thinking_delta", "text": "Need to inspect adapter mapping.", "thinking_chars": 32,
                 "reasoning_source": "reasoning", "visibility": "provider_returned"},
                {"kind": "thinking_redacted", "summary": "provider emitted encrypted reasoning block", "thinking_chars": 0,
                 "reasoning_source": "reasoning_details.encrypted", "visibility": "provider_redacted"},
                {"kind": "thinking_summary", "summary": "legacy aggregate should not double count", "thinking_chars": 99},
                {"kind": "usage", "input_tokens": 5, "output_tokens": 7},
                {"kind": "run_finished", "message": "final answer", "model": "deepseek/test", "iterations": 1,
                 "tool_uses": [], "tool_results": [], "usage": {"input_tokens": 5, "output_tokens": 7}},
            ]:
                print(json.dumps(event), flush=True)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only"},
        resolve_access_root=lambda: tmp_path,
    )
    adapter = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    assert await adapter.initialize() is True
    response = await adapter.generate_response("hello", "req-stream", on_stream_event=collect)

    assert response.is_success is True
    assert response.usage.thinking_tokens == 8
    assert response.stream_metadata["claw_thinking"] == {
        "thinking_chars": 32,
        "thinking_tokens": 8,
        "thinking_event_count": 2,
        "thinking_redacted_count": 1,
        "thinking_sources": ["reasoning", "reasoning_details.encrypted"],
    }
    assert any(
        event.kind == KIND_THINKING
        and event.summary == "Need to inspect adapter mapping."
        and event.detail == "thinking_chars=32;source=reasoning"
        for event in events
    )
    assert any(
        event.kind == KIND_THINKING
        and event.detail == "thinking_chars=0;redacted=true;source=reasoning_details.encrypted"
        for event in events
    )


@pytest.mark.asyncio
async def test_claw_adapter_shutdown_kills_running_process(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        import json, sys, time
        if sys.argv[1] == "version":
            print(json.dumps({"kind": "version", "version": "0.1.0"}))
        else:
            time.sleep(20)
        """,
    )
    cfg = SimpleNamespace(
        name="test",
        workspace_dir=tmp_path,
        model="deepseek/test",
        extra={"claw_binary_path": str(fake), "permission_mode": "read-only", "hard_timeout_sec": 30},
        resolve_access_root=lambda: tmp_path,
    )
    global_cfg = SimpleNamespace()
    adapter = ClawCLIAdapter(cfg, global_cfg, api_key="test-key")
    assert await adapter.initialize() is True

    task = asyncio.create_task(adapter.generate_response("hello", "req-slow"))
    for _ in range(50):
        if adapter.current_proc is not None:
            break
        await asyncio.sleep(0.02)
    assert adapter.current_proc is not None

    await adapter.shutdown()
    response = await task

    assert response.is_success is False
