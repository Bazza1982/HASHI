from __future__ import annotations

import json

from tools.enterprise_k8s_backend_doctor import main, run_doctor


def test_kubernetes_backend_doctor_static_contract_passes():
    result = run_doctor(import_name="definitely_missing_hashi_kubernetes_test_module")

    assert result["ok"] is True
    assert result["checks"]["pyproject_extra"] is True
    assert result["checks"]["requirements_comment"] is True
    assert result["checks"]["docker_build_arg"] is True
    assert result["checks"]["docker_installs_extra"] is True
    assert result["checks"]["module_installed"] is False
    assert result["missing"] == []


def test_kubernetes_backend_doctor_can_require_installed_module():
    result = run_doctor(
        require_installed=True,
        import_name="definitely_missing_hashi_kubernetes_test_module",
    )

    assert result["ok"] is False
    assert result["missing"] == ["module_installed"]


def test_kubernetes_backend_doctor_cli_json(capsys):
    rc = main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"]["pyproject_extra"] is True
