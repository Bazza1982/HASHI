from __future__ import annotations

import subprocess
import sys

import pytest

from scripts import hashi_instance_cli as cli
from tests.test_remote_rescue_control import _client


def test_runtime_selector_keeps_the_real_qualification_failure(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hashi.runtime]\ncore-host-lock = "core.lock"\n', encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check_runtime_selection.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_prepared_role_candidates", lambda root: [[sys.executable]])
    monkeypatch.setattr(cli, "_validate_environment_path", lambda path: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="Traceback fixture\nRuntimeContractError: original file closure changed\n",
    ))
    with pytest.raises(cli.InstanceRegistryError, match="original file closure changed"):
        cli._select_core_runtime(tmp_path)


@pytest.mark.parametrize("error", [None, "original file closure changed"])
def test_authenticated_readonly_qualification_never_launches_core(tmp_path, monkeypatch, error):
    from remote.api import server
    client = _client(tmp_path)
    def qualify():
        if error:
            raise ValueError(error)
        return {"role": "functions", "core_source_digest": "approved"}
    def forbidden_launch(*args, **kwargs):
        raise AssertionError("Readonly qualification attempted to launch Core")
    monkeypatch.setattr(server, "_qualified_restart_runtime", qualify)
    monkeypatch.setattr(server, "_restart_hashi_process", forbidden_launch)
    response = client.get("/control/hashi/qualification")
    assert response.status_code == (503 if error else 200)
    result = response.json()
    assert result["code_root"] == str(tmp_path)
    if error:
        assert result["error"] == error
        assert result["phase"] == "runtime_qualification"
    else:
        assert result["runtime"]["role"] == "functions"
# HASHI-Fingerprint: hfp_2398a8a3b92a4309b154ebcab6f4b795
