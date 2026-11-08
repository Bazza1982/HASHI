from __future__ import annotations

import json

from tools.enterprise_k8s_backend_doctor import main, run_doctor


def test_kubernetes_backend_doctor_static_contract_passes():
    result = run_doctor(import_name="definitely_missing_hashi_kubernetes_test_module")

    assert result["ok"] is True
    assert result["checks"]["pyproject_extra"] is True
    assert result["checks"]["standard_profile_excludes_kubernetes"] is True
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


def test_kubernetes_backend_doctor_rejects_unlocked_or_unselected_extra(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("pyproject.toml", "requirements.txt",
                 "scripts/compile_enterprise_functions.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    docker = (root / "Dockerfile.enterprise").read_text(encoding="utf-8")
    for required in ("--require-hashes", '--extras "$HASHI_ENTERPRISE_EXTRAS"',
                     "--extra-lock /opt/hashi-build/functions.hashes.lock"):
        assert required in docker
        (tmp_path / "Dockerfile.enterprise").write_text(
            docker.replace(required, ""), encoding="utf-8")
        result = run_doctor(tmp_path, import_name="definitely_missing_hashi_kubernetes_test_module")
        assert result["ok"] is False
        assert "docker_installs_extra" in result["missing"]
# HASHI-Fingerprint: hfp_68056369b76f40ab91c83678348db6dc
