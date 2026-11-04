"""Deferred native cleanup preserves the failed receipt and every unknown owner."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config_json import new_config_json, read_config_json, write_config_json
from scripts.permissions import windows_service_probe as subject
from scripts.permissions.windows_test_identity import CleanupUnverified


def retained(tmp_path):
    account_rows, profile_paths, service_rows = {}, {}, {}
    deleted = []
    output = tmp_path / "probe"
    output.mkdir()

    def save(path, value):
        write_config_json(path, new_config_json(path, value))

    results = []
    for number, role in enumerate(("safety", "runner"), 1):
        identifier = str(number) * 32
        name, sid = "HashiProbe_" + identifier[:8], f"S-1-5-21-1-2-3-100{number}"
        marker = "HASHI disposable permission probe " + identifier
        account_rows[name] = {"sid": sid, "comment": marker}
        directory = output / (role + "-profile")
        directory.mkdir()
        profile_paths[sid] = directory
        save(output / (role + "-account.json"), {"version": 1, "id": identifier, "name": name,
             "sid": sid, "marker": marker, "state": "created"})
        save(output / (role + "-profile.json"), {"version": 1, "type": "hashi.disposable-user-profile",
             "name": name, "sid": sid, "marker": marker, "path": str(directory), "state": "created"})
        service = "Hashi" + role.title() + "Test_" + identifier[:12]
        save(output / (identifier + "-service.json"), {"version": 1, "id": identifier, "name": service,
             "binary": "fixed-python.exe -I fixed-entry", "account": ".\\" + name, "sid": sid,
             "display": "HASHI disposable SCM probe " + identifier, "state": "cleaned"})
        results.append({"role": role, "service": service})
        service_rows[service] = None
    save(output / "report.json", {"version": 1, "component_only": True, "source_commit": "a" * 40,
         "results": results, "failure": None, "cleanup_errors": [{"error": "still loaded"}], "pass": False})

    def delete_account(name):
        deleted.append(name)
        del account_rows[name]

    def delete_profile(sid):
        profile_paths.pop(sid).rmdir()

    return SimpleNamespace(
        output=output, deleted=deleted, accounts=SimpleNamespace(
            info=lambda name: account_rows.get(name), delete=delete_account, disabled=lambda name: True),
        profiles=SimpleNamespace(path=lambda sid: str(profile_paths[sid]) if sid in profile_paths else None,
                                 loaded=lambda sid: False, delete=delete_profile),
        rights=SimpleNamespace(list=lambda sid: set()),
        services=SimpleNamespace(query=lambda name: service_rows[name]),
        service_rows=service_rows, account_rows=account_rows,
    )


def recover(env):
    return subject.recover_probe(env.output, accounts=env.accounts, profiles=env.profiles,
                                 rights=env.rights, services=env.services)


def test_cleanup_recovery_keeps_original_failure_and_is_durably_idempotent(tmp_path):
    env = retained(tmp_path)
    original = (env.output / "report.json").read_bytes()
    result = recover(env)
    assert result["cleanup_confirmed"] is True and result["original_pass"] is False
    assert len(env.deleted) == 2
    assert (env.output / "report.json").read_bytes() == original
    assert all(read_config_json(env.output / (role + "-account.json"))["state"] == "cleaned"
               for role in ("safety", "runner"))
    recover(env)
    assert len(env.deleted) == 2


@pytest.mark.parametrize("boundary", ["profile", "service", "rights", "identity"])
def test_recovery_preflight_refuses_unreleased_or_replaced_resources(tmp_path, boundary):
    env = retained(tmp_path)
    if boundary == "profile":
        env.profiles.loaded = lambda sid: True
    elif boundary == "service":
        env.service_rows[next(iter(env.service_rows))] = {"replacement": True}
    elif boundary == "rights":
        env.rights.list = lambda sid: {"SeServiceLogonRight"}
    else:
        env.account_rows[next(iter(env.account_rows))]["sid"] = "S-1-5-21-1-2-3-9999"
    with pytest.raises(CleanupUnverified):
        recover(env)
    assert env.deleted == []
    assert all((env.output / (role + "-profile")).exists() for role in ("safety", "runner"))
# HASHI-Fingerprint: hfp_ea3919794ab74a52b9af479ade604ab4
