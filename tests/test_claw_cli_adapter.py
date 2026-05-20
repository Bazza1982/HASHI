from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from adapters.claw_cli import (
    ClawBinaryNotFound,
    ClawCommandError,
    ClawJsonError,
    ClawTimeoutError,
    build_claw_env,
    find_claw_binary,
    run_claw_doctor,
    run_claw_json_command,
)


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_claw_binary_accepts_configured_executable(tmp_path):
    fake = _write_exe(
        tmp_path / "claw",
        """
        #!/usr/bin/env python3
        print("ok")
        """,
    )

    assert find_claw_binary(fake) == fake.resolve()


def test_find_claw_binary_reports_missing_configured_path(tmp_path):
    with pytest.raises(ClawBinaryNotFound):
        find_claw_binary(tmp_path / "missing", env={"PATH": ""})


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

