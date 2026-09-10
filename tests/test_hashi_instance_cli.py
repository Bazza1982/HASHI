from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import hashi_instance_cli
from tools import instance_registry


def _record(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "record_id": "r1",
        "name": "alpha",
        "instance_id": "ALPHA",
        "code_root": str(tmp_path / "program"),
        "bridge_home": str(home),
        "managed": False,
        "source_kind": "git",
        "api_port": 18800,
        "gateway_port": 18801,
        "adopted_program_version": None,
    }


def test_onboarding_runs_as_a_module_from_the_program_root(tmp_path, monkeypatch):
    record = _record(tmp_path)
    code_root = tmp_path / "program"
    captured = {}

    monkeypatch.setattr(
        hashi_instance_cli,
        "_select_runtime",
        lambda _code_root, *, full: ["python-runtime"],
    )

    def run(command, **options):
        captured["command"] = command
        captured["options"] = options
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(hashi_instance_cli.subprocess, "run", run)

    assert hashi_instance_cli.run_onboarding(record, code_root) == 0
    assert captured["command"] == [
        "python-runtime",
        "-m",
        "tui.connection",
    ]
    assert captured["options"]["cwd"] == code_root
    assert captured["options"]["env"]["HASHI_ONBOARD_NO_LAUNCH"] == "1"


def test_python_manager_rejects_the_other_os_runtime_path():
    if os.name == "nt":
        unsafe = Path(r"\\wsl.localhost\ExampleDistro\srv\hashi")
    else:
        unsafe = Path("/mnt/c/Python312/python.exe")

    with pytest.raises(instance_registry.InstanceRegistryError):
        hashi_instance_cli._validate_environment_path(unsafe)


def test_stop_busy_instance_does_not_send_shutdown(tmp_path, monkeypatch, capsys):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "lock_held": True,
            "healthy": True,
            "running": True,
            "activity_verified": True,
            "busy": True,
            "busy_agents": ["worker-a"],
        },
    )
    calls = []
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )

    result = hashi_instance_cli.stop_instance(registry, record)

    assert result == hashi_instance_cli.EXIT_BUSY
    assert calls == []
    assert "no work was cancelled" in capsys.readouterr().err


def test_stop_with_active_background_job_does_not_send_shutdown(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "lock_held": True,
            "healthy": True,
            "running": True,
            "activity_verified": True,
            "busy": True,
            "busy_agents": [],
            "busy_background_jobs": ["job-123"],
        },
    )
    calls = []
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )

    result = hashi_instance_cli.stop_instance(registry, record)

    assert result == hashi_instance_cli.EXIT_BUSY
    assert calls == []
    assert "background-job:job-123" in capsys.readouterr().err


def test_stop_unverified_lock_never_force_kills(tmp_path, monkeypatch, capsys):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "lock_held": True,
            "healthy": False,
            "running": False,
            "activity_verified": False,
            "busy": False,
            "busy_agents": [],
        },
    )

    result = hashi_instance_cli.stop_instance(registry, record)

    assert result == hashi_instance_cli.EXIT_BUSY
    assert "cannot be verified" in capsys.readouterr().err


def test_stop_fails_closed_when_live_activity_endpoint_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "lock_held": True,
            "healthy": True,
            "running": True,
            "activity_verified": False,
            "busy": False,
            "busy_agents": [],
        },
    )
    calls = []
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )

    result = hashi_instance_cli.stop_instance(registry, record)

    assert result == hashi_instance_cli.EXIT_BUSY
    assert calls == []
    assert "queue state could not be verified" in capsys.readouterr().err


def test_graceful_stop_uses_exact_local_api_and_waits_for_unlock(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "lock_held": True,
            "healthy": True,
            "running": True,
            "activity_verified": True,
            "busy": False,
            "busy_agents": [],
            "api_port": 18800,
        },
    )
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {"ok": True}

    monkeypatch.setattr(hashi_instance_cli, "_http_json", request)
    monkeypatch.setattr(hashi_instance_cli, "_admin_token", lambda *_args: "secret")
    monkeypatch.setattr(hashi_instance_cli, "_lock_is_held", lambda _path: False)

    result = hashi_instance_cli.stop_instance(registry, record)

    assert result == 0
    assert calls == [
        (
            ("POST", 18800, "/api/admin/shutdown"),
            {
                "token": "secret",
                "body": {"reason": "hashi-cli"},
                "timeout": 5.0,
            },
        )
    ]
    assert "gracefully" in capsys.readouterr().out


def test_status_with_no_registry_is_read_only_and_successful(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HASHI_PROGRAM_ROOT", str(tmp_path / "program"))
    monkeypatch.setenv("HASHI_PROGRAM_VERSION", "test")
    monkeypatch.setenv("HASHI_REGISTRY_ROOT", str(tmp_path / "registry"))
    monkeypatch.setenv("HASHI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HASHI_INVOCATION_CWD", str(tmp_path))

    result = hashi_instance_cli.main(["--json", "status"])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"instances": []}
    assert not (tmp_path / "registry").exists()
    assert not (tmp_path / "data").exists()


def test_first_interactive_onboarding_creates_one_isolated_default_instance(
    tmp_path, monkeypatch
):
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setenv("HASHI_PROGRAM_ROOT", str(program))
    monkeypatch.setenv("HASHI_PROGRAM_VERSION", "test")
    monkeypatch.setenv("HASHI_REGISTRY_ROOT", str(tmp_path / "registry"))
    monkeypatch.setenv("HASHI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HASHI_INVOCATION_CWD", str(tmp_path / "project"))
    monkeypatch.setattr(instance_registry, "_port_available", lambda _port: True)
    monkeypatch.setattr(hashi_instance_cli, "has_interactive_input", lambda: True)
    onboarded = []
    monkeypatch.setattr(hashi_instance_cli, "_run_tui", lambda *a, **k: 0)
    monkeypatch.setattr(
        hashi_instance_cli,
        "run_onboarding",
        lambda record, code_root: onboarded.append((record, code_root)) or 0,
    )

    assert hashi_instance_cli.main(["onboard"]) == 0

    registry = instance_registry.InstanceRegistry(
        program_root=program,
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
    )
    [record] = registry.records()
    assert record["name"] == "default"
    assert record["default"] is True
    assert Path(record["bridge_home"]).parent == registry.instances_root
    assert onboarded[0][0]["record_id"] == record["record_id"]
    assert onboarded[0][1] == program


def test_status_text_does_not_call_healthy_or_not_ready_process_stopped(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    (tmp_path / "program").mkdir()
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    snapshots = [
        (
            {
                "running": True,
                "ready": False,
                "services_ready": True,
                "lock_held": True,
                "healthy": True,
                "instance_id": "ALPHA",
                "pid": 123,
                "api_port": 18800,
                "busy": False,
                "activity_verified": True,
            },
            "State: running/local-services-ready",
        ),
        (
            {
                "running": True,
                "ready": False,
                "lock_held": True,
                "healthy": True,
                "instance_id": "ALPHA",
                "pid": 123,
                "api_port": 18800,
                "busy": False,
                "activity_verified": True,
            },
            "State: running/not-ready",
        ),
        (
            {
                "running": False,
                "ready": True,
                "lock_held": False,
                "healthy": True,
                "instance_id": "ALPHA",
                "pid": None,
                "api_port": 18800,
                "busy": False,
                "activity_verified": False,
            },
            "State: unverified/api-without-lock",
        ),
    ]
    for snapshot, expected in snapshots:
        monkeypatch.setattr(
            hashi_instance_cli,
            "inspect_instance",
            lambda _record, _root, value=snapshot: dict(value),
        )
        assert (
            hashi_instance_cli._status_output(
                registry, record, "explicit", as_json=False
            )
            == 0
        )
        assert expected in capsys.readouterr().out


def test_noninteractive_ambiguity_requires_explicit_instance(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(instance_registry, "_port_available", lambda _port: True)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id=instance_registry.current_environment_id(),
    )
    registry.program_root.mkdir()
    registry.create("alpha")
    registry.create("beta")
    monkeypatch.setenv("HASHI_PROGRAM_ROOT", str(registry.program_root))
    monkeypatch.setenv("HASHI_PROGRAM_VERSION", "test")
    monkeypatch.setenv("HASHI_REGISTRY_ROOT", str(registry.registry_root))
    monkeypatch.setenv("HASHI_DATA_ROOT", str(registry.data_root))
    monkeypatch.setenv("HASHI_INVOCATION_CWD", str(tmp_path / "unbound"))
    monkeypatch.setattr(hashi_instance_cli.sys.stdin, "isatty", lambda: False)

    result = hashi_instance_cli.main(["status"])

    assert result == hashi_instance_cli.EXIT_USAGE
    assert "use --instance" in capsys.readouterr().err


def test_status_sanitizes_agent_metadata_and_verifies_busy_fields(
    tmp_path, monkeypatch
):
    record = _record(tmp_path)
    (tmp_path / "program").mkdir()
    (tmp_path / "home" / "agents.json").write_text(
        json.dumps(
            {
                "global": {"instance_id": "ALPHA", "workbench_port": 18800},
                "agents": [{"name": "worker-a"}],
            }
        ),
        encoding="utf-8",
    )
    responses = iter(
        [
            (200, {"ok": True, "instance_id": "ALPHA", "workbench_port": 18800}),
            (
                200,
                {
                    "ok": True,
                    "agents": [
                        {
                            "name": "worker-a",
                            "status": "online",
                            "is_generating": False,
                            "queue_depth": 0,
                            "active_transfer": False,
                            "worker_pid": 123,
                            "generation_id": "sha256:test",
                            "primary_chat_id": 999,
                            "allowed_backends": [{"secret": "do-not-project"}],
                        }
                    ],
                },
            ),
            (200, {"ok": True, "jobs": []}),
        ]
    )
    monkeypatch.setattr(hashi_instance_cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: next(responses),
    )

    status = hashi_instance_cli.inspect_instance(record, tmp_path / "program")

    assert status["activity_verified"] is True
    assert status["busy"] is False
    assert status["background_jobs"] == []
    assert status["agents"] == [
        {
            "name": "worker-a",
            "status": "online",
            "is_generating": False,
            "queue_depth": 0,
            "active_transfer": False,
            "worker_pid": 123,
            "generation_id": "sha256:test",
        }
    ]
    assert "primary_chat_id" not in json.dumps(status)
    assert "do-not-project" not in json.dumps(status)


def test_health_without_explicit_port_cannot_claim_instance_identity(
    tmp_path, monkeypatch
):
    record = _record(tmp_path)
    (tmp_path / "program").mkdir()
    (tmp_path / "home" / "agents.json").write_text(
        '{"global":{"instance_id":"ALPHA","workbench_port":18800}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(hashi_instance_cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: (200, {"ok": True, "instance_id": "ALPHA"}),
    )

    status = hashi_instance_cli.inspect_instance(record, tmp_path / "program")

    assert status["healthy"] is False
    assert status["foreign_endpoint"] is True
    assert status["running"] is False


@pytest.mark.parametrize(
    ("agents_payload", "jobs_payload"),
    [
        ({"ok": True}, {"ok": True, "jobs": []}),
        ({"ok": True, "agents": []}, {"ok": True}),
        (
            {
                "ok": True,
                "agents": [
                    {
                        "name": "worker-a",
                        "is_generating": False,
                        "queue_depth": "not-an-integer",
                        "active_transfer": False,
                    }
                ],
            },
            {"ok": True, "jobs": []},
        ),
    ],
)
def test_activity_verification_fails_closed_on_incomplete_endpoint_schema(
    tmp_path, monkeypatch, agents_payload, jobs_payload
):
    record = _record(tmp_path)
    (tmp_path / "program").mkdir()
    (tmp_path / "home" / "agents.json").write_text(
        '{"global":{"instance_id":"ALPHA","workbench_port":18800}}\n',
        encoding="utf-8",
    )
    responses = iter(
        [
            (200, {"ok": True, "instance_id": "ALPHA", "workbench_port": 18800}),
            (200, agents_payload),
            (200, jobs_payload),
        ]
    )
    monkeypatch.setattr(hashi_instance_cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        hashi_instance_cli,
        "_http_json",
        lambda *_args, **_kwargs: next(responses),
    )

    status = hashi_instance_cli.inspect_instance(record, tmp_path / "program")

    assert status["activity_verified"] is False


def test_repeat_start_is_idempotent(tmp_path, monkeypatch, capsys):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "running": True,
            "pid": 456,
            "ready": True,
        },
    )

    assert hashi_instance_cli.start_instance(registry, record) == 0
    assert "already running" in capsys.readouterr().out


def test_start_accepts_telegram_only_degraded_local_services(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    program = tmp_path / "program"
    program.mkdir()
    registry = instance_registry.InstanceRegistry(
        program_root=program,
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    snapshots = iter(
        [
            {
                "running": False,
                "lock_held": False,
                "healthy": False,
                "foreign_endpoint": False,
            },
            {
                "running": True,
                "ready": False,
                "services_ready": True,
                "pid": 456,
                "api_port": 18800,
            },
        ]
    )
    monkeypatch.setattr(
        hashi_instance_cli, "inspect_instance", lambda *_args: next(snapshots)
    )
    monkeypatch.setattr(hashi_instance_cli, "_is_provisioned", lambda *_args: True)
    monkeypatch.setattr(
        hashi_instance_cli, "_select_runtime", lambda *_args, **_kwargs: ["python"]
    )

    class Process:
        returncode = 91

        @staticmethod
        def poll():
            return 91

    monkeypatch.setattr(hashi_instance_cli.subprocess, "Popen", lambda *_a, **_k: Process())

    assert hashi_instance_cli.start_instance(registry, record) == 0
    assert "local services ready" in capsys.readouterr().out


def test_local_service_acceptance_rejects_other_degradation():
    health = {
        "startup": {
            "phase": "degraded",
            "services_ready": True,
            "failed_agents": 0,
            "pending_agents": 0,
            "connecting_agents": 0,
            "issues": [
                {
                    "code": "agent_telegram_unavailable",
                    "severity": "warning",
                }
            ],
        },
        "function_workers": [
            {
                "phase": "ACTIVE",
                "alive": True,
                "accepting": True,
            }
        ],
    }
    assert hashi_instance_cli._telegram_only_local_services_ready(health)

    failed_agent = json.loads(json.dumps(health))
    failed_agent["startup"]["failed_agents"] = 1
    assert not hashi_instance_cli._telegram_only_local_services_ready(failed_agent)

    other_issue = json.loads(json.dumps(health))
    other_issue["startup"]["issues"][0]["code"] = "agent_startup_failed"
    assert not hashi_instance_cli._telegram_only_local_services_ready(other_issue)

    unready_worker = json.loads(json.dumps(health))
    unready_worker["function_workers"][0]["accepting"] = False
    assert not hashi_instance_cli._telegram_only_local_services_ready(unready_worker)


def test_start_refuses_matching_api_without_instance_lock(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="test",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "running": False,
            "lock_held": False,
            "healthy": True,
            "foreign_endpoint": False,
        },
    )
    monkeypatch.setattr(
        hashi_instance_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not start a second process")
        ),
    )

    assert hashi_instance_cli.start_instance(registry, record) == hashi_instance_cli.EXIT_BUSY
    assert "unverified process" in capsys.readouterr().err


def test_stopped_managed_instance_cannot_implicitly_adopt_update(
    tmp_path, monkeypatch, capsys
):
    record = _record(tmp_path)
    record.update(
        {
            "managed": True,
            "adopted_program_version": "old",
        }
    )
    registry = instance_registry.InstanceRegistry(
        program_root=tmp_path / "program",
        program_version="new",
        registry_root=tmp_path / "registry",
        data_root=tmp_path / "data",
        environment_id="test-os",
    )
    monkeypatch.setattr(
        hashi_instance_cli,
        "inspect_instance",
        lambda _record, _root: {
            "running": False,
            "lock_held": False,
            "foreign_endpoint": False,
        },
    )

    result = hashi_instance_cli.start_instance(registry, record)

    assert result == hashi_instance_cli.EXIT_NOT_READY
    assert "has not adopted" in capsys.readouterr().err


def test_start_timeout_preserves_actual_child_and_reports_unknown_readiness(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    record = _record(tmp_path)
    root = tmp_path / 'program'
    root.mkdir()
    (root / 'main.py').write_text('import time\ntime.sleep(60)\n')
    registry = instance_registry.InstanceRegistry(program_root=root,program_version='test',
        registry_root=tmp_path/'registry',data_root=tmp_path/'data',environment_id='test-os')
    monkeypatch.setattr(hashi_instance_cli,'_is_provisioned',lambda *_:True)
    monkeypatch.setattr(hashi_instance_cli,'_select_runtime',lambda *a,**k:[sys.executable])
    monkeypatch.setattr(hashi_instance_cli,'inspect_instance',lambda *_:{
        'running':False,'lock_held':False,'healthy':False,'foreign_endpoint':False})
    children=[]
    original=subprocess.Popen
    def launch(*a,**kw):
        child=original(*a,**kw);children.append(child);return child
    monkeypatch.setattr(subprocess,'Popen',launch)
    try:
        assert hashi_instance_cli.start_instance(registry,record,timeout=.01)==75
        assert children[0].poll() is None
        assert 'START_TIMEOUT' in capsys.readouterr().err
    finally:
        for child in children:
            child.terminate();child.wait(timeout=10)


def test_terminal_global_options_json_errors_and_attach_only_are_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HASHI_PROGRAM_ROOT',str(tmp_path/'program'))
    monkeypatch.setenv('HASHI_REGISTRY_ROOT',str(tmp_path/'registry'))
    monkeypatch.setenv('HASHI_DATA_ROOT',str(tmp_path/'data'))
    for argv, code in [(['status','--check','--json'],69),
                       (['status','--instance','one','-i','two','--json'],64),
                       (['status','--all','-i','one','--json'],64),
                       (['tui','--attach-only','--non-interactive','--json'],64),
                       (['start','--timeout','0','--json'],64)]:
        assert hashi_instance_cli.main(argv)==code
        envelope=json.loads(capsys.readouterr().out)
        assert envelope['exit_code']==code and not envelope['ok']
    assert not (tmp_path/'registry').exists()
    assert not (tmp_path/'data').exists()


def test_terminal_purge_error_codes_preserve_external_data(tmp_path, monkeypatch, capsys):
    program=tmp_path/'program';program.mkdir()
    external=tmp_path/'external';external.mkdir()
    config=external/'agents.json'
    config.write_text('{"global":{"instance_id":"EXTERNAL","workbench_port":19991},"agents":[]}')
    before=config.read_bytes()
    (external/'main.py').write_text('')
    (external/'runtime-entry.json').write_text('{}')
    (external/'scripts').mkdir()
    (external/'scripts/check_runtime_contract.py').write_text('')
    monkeypatch.setenv('HASHI_PROGRAM_ROOT',str(program))
    monkeypatch.setenv('HASHI_REGISTRY_ROOT',str(tmp_path/'registry'))
    monkeypatch.setenv('HASHI_DATA_ROOT',str(tmp_path/'data'))
    registry=instance_registry.InstanceRegistry(program_root=program,program_version='development')
    registry.create('external',source_root=external)
    for extra,expected in [([], 'CONFIRMATION_REQUIRED'),(['--confirm','external'],'PURGE_DENIED')]:
        assert hashi_instance_cli.main(['instance','remove','external','--purge','--json',*extra])==77
        envelope=json.loads(capsys.readouterr().out)
        assert envelope['error']['code']==expected
        assert config.read_bytes()==before
        assert registry.get('external')['name']=='external'
