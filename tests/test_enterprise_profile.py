from __future__ import annotations

import json

import pytest

from orchestrator.config import ConfigManager


def _write_config(
    tmp_path,
    *,
    global_extra: dict | None = None,
    profile: str | None = None,
    bootstrap_complete: bool = False,
    use_governed_defaults: bool = True,
):
    global_cfg = {
        "authorized_id": 0,
        "base_logs_dir": "logs",
        "base_media_dir": "media",
    }
    if profile is not None:
        global_cfg["deployment_profile"] = profile
    if use_governed_defaults and global_cfg.get("deployment_profile") in {"team", "enterprise"}:
        if "organization_id" not in (global_extra or {}):
            global_cfg["organization_id"] = "ORG-001"
        if bootstrap_complete:
            global_cfg["enterprise_bootstrap_complete"] = True
    if global_extra:
        global_cfg.update(global_extra)

    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps({"global": global_cfg, "agents": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"authorized_telegram_id": 0}, ensure_ascii=False), encoding="utf-8")
    return config_path, secrets_path


def test_default_profile_is_personal(tmp_path):
    config_path, secrets_path = _write_config(tmp_path)
    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()
    assert global_cfg.deployment_profile == "personal"
    assert global_cfg.organization_id is None


def test_profile_from_env_is_respected(tmp_path, monkeypatch):
    config_path, secrets_path = _write_config(
        tmp_path,
        profile="team",
        bootstrap_complete=True,
        global_extra={"organization_id": "ENV-ORG"},
    )
    monkeypatch.setenv("HASHI_DEPLOYMENT_PROFILE", "team")
    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()
    assert global_cfg.deployment_profile == "team"
    assert global_cfg.organization_id == "ENV-ORG"


def test_profile_env_bootstrap_fields_are_respected(tmp_path, monkeypatch):
    config_path, secrets_path = _write_config(
        tmp_path,
        profile="team",
        bootstrap_complete=False,
        global_extra={"organization_id": "CFG-ORG"},
    )
    monkeypatch.setenv("HASHI_ORGANIZATION_ID", "ENV-ORG")
    monkeypatch.setenv("HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE", "true")

    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()
    assert global_cfg.deployment_profile == "team"
    assert global_cfg.organization_id == "ENV-ORG"


@pytest.mark.parametrize("profile", ["team", "enterprise"])
def test_governed_profiles_require_organization(tmp_path, profile):
    config_path, secrets_path = _write_config(
        tmp_path,
        profile=profile,
        use_governed_defaults=False,
    )

    with pytest.raises(ValueError, match="requires global.organization_id"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


@pytest.mark.parametrize("profile", ["team", "enterprise"])
def test_governed_profiles_require_bootstrap_after_org(tmp_path, profile):
    config_path, secrets_path = _write_config(
        tmp_path,
        profile=profile,
        bootstrap_complete=False,
        global_extra={"organization_id": "ORG-BOOT"},
    )

    with pytest.raises(ValueError, match="bootstrap initialization"):
        ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()


@pytest.mark.parametrize("profile", ["team", "enterprise"])
def test_governed_profiles_allow_valid_bootstrap(tmp_path, profile):
    config_path, secrets_path = _write_config(
        tmp_path,
        profile=profile,
        bootstrap_complete=True,
    )
    global_cfg, _, _ = ConfigManager(config_path, secrets_path, bridge_home=tmp_path).load()
    assert global_cfg.deployment_profile == profile
    assert global_cfg.organization_id == "ORG-001"
