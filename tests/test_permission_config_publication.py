"""PAO configuration publication must keep the installed authority readable."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess

import pytest

from orchestrator.agent_incarnation import ensure_agent_lifecycle_id
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.pathing import build_bridge_paths

_SAFETY_SID = "S-1-5-21-11111111-22222222-33333333-1042"


def _installed(tmp_path):
    path = tmp_path / "agents.json"
    value = {"global": {"instance_id": "TEST", "permission_service": {
        "version": 1, "instance_id": "TEST",
        "pao_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.pao.config_test",
        "control_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.control.config_test",
        "public_key": base64.b64encode(bytes(range(32))).decode(),
        "service_sid": _SAFETY_SID,
    }}, "agents": [{"name": "qualification", "is_active": True}],
        "unowned": {"preserve": True}}
    # The installer explicitly grants access; normal PAO writes must keep only read.
    write_config_json(path, value, private_full_control_sids=(_SAFETY_SID,))
    return path


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL publication")
@pytest.mark.parametrize("publisher", ["incarnation", "admin", "migration", "group"])
def test_normal_pao_publication_keeps_only_installed_authority_read_access(tmp_path, publisher):
    path = _installed(tmp_path)
    if publisher == "incarnation":
        ensure_agent_lifecycle_id(path, "qualification")
    elif publisher == "migration":
        from orchestrator.config import ConfigManager
        manager = ConfigManager(path, tmp_path / "secrets.json", bridge_home=tmp_path)
        document = read_config_json(path)
        document["migration_observation"] = True
        manager._write_config_atomic(document, label="qualification")
    elif publisher == "group":
        from orchestrator.agent_directory import AgentDirectory
        directory = AgentDirectory(path, tmp_path / "capabilities.json", [])
        ok, message = directory.create_group("qualification")
        assert ok, message
    else:
        admin = ConfigAdmin(build_bridge_paths(tmp_path, bridge_home=tmp_path))
        document = admin.load_raw_config()
        document["agents"][0]["is_active"] = False
        admin.write_raw_config(document)
    acl = subprocess.run(["icacls.exe", str(path)], capture_output=True,
                         text=True, check=True).stdout
    assert _SAFETY_SID + ":(R)" in acl, acl
    assert _SAFETY_SID + ":(F)" not in acl, acl
    assert "Everyone:" not in acl and "BUILTIN\\Users:" not in acl
    assert read_config_json(path)["unowned"] == {"preserve": True}
# HASHI-Fingerprint: hfp_8d08b664fbcf4d71880cdae56326457e


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL publication")
@pytest.mark.parametrize("sid", ["S-1-1-0", "S-1-5-32-544", "S-1-5-21-invalid"])
def test_invalid_or_broad_service_reader_cannot_publish(tmp_path, sid):
    from orchestrator.agent_config_json import write_agent_config_json
    path = _installed(tmp_path)
    document = read_config_json(path)
    before = path.read_bytes()
    document["global"]["permission_service"]["service_sid"] = sid
    with pytest.raises(ValueError, match="private account SID"):
        write_agent_config_json(path, document)
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL publication")
def test_cross_account_publication_keeps_revision_conflicts_and_private_secrets(tmp_path):
    from orchestrator.agent_config_json import write_agent_config_json
    from orchestrator.config_json import ConfigConflictError
    path = _installed(tmp_path)
    first, stale = read_config_json(path), read_config_json(path)
    first["unowned"]["newer"] = True
    write_agent_config_json(path, first)
    before = path.read_bytes()
    stale["unowned"]["lost"] = True
    with pytest.raises(ConfigConflictError):
        write_agent_config_json(path, stale)
    assert path.read_bytes() == before
    private = tmp_path / "secrets.json"
    write_config_json(private, {"workbench_admin_token": "synthetic-only"})
    acl = subprocess.run(["icacls.exe", str(private)], capture_output=True,
                         text=True, check=True).stdout
    assert _SAFETY_SID not in acl


def test_retained_legacy_array_keeps_its_read_revision(tmp_path):
    from orchestrator.agent_config_json import write_agent_config_json
    from orchestrator.config_json import ConfigConflictError, read_managed_json
    path = tmp_path / "agents.json"
    path.write_text('[{"name": "legacy"}]\n', encoding="utf-8")
    first, stale = read_managed_json(path), read_managed_json(path)
    first[0]["extra"] = "preserved"
    write_agent_config_json(path, first)
    saved = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        write_agent_config_json(path, stale)
    assert path.read_bytes() == saved
    assert read_managed_json(path)[0]["extra"] == "preserved"
