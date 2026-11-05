"""The test profile cleaner must preserve every identity it does not own."""
import json
from types import SimpleNamespace

import pytest

from scripts.permissions.windows_test_identity import CleanupUnverified
from scripts.permissions.windows_test_profile import DisposableWindowsProfile


def setup(tmp_path):
    identity = SimpleNamespace(
        name="HashiProbe_01234567", sid="S-1-5-21-1-2-3-1000", _value={"marker": "owned"},
    )
    current = {"sid": identity.sid, "comment": "owned"}
    accounts = SimpleNamespace(info=lambda name: current)

    class Profiles:
        directory = None
        deleted = []

        def path(self, sid):
            return str(self.directory) if self.directory is not None else None

        def loaded(self, sid):
            return False

        def delete(self, sid):
            self.deleted.append(sid)
            self.directory.rmdir()
            self.directory = None

    return identity, current, accounts, Profiles()


def test_profile_cleanup_proves_new_ownership_and_never_adopts_existing_data(tmp_path):
    identity, current, accounts, profiles = setup(tmp_path)
    directory = tmp_path / "existing"
    directory.mkdir()
    profiles.directory = directory
    with pytest.raises(CleanupUnverified, match="pre-existing"):
        with DisposableWindowsProfile(accounts, identity, profiles, tmp_path / "a.json"):
            pytest.fail("an existing profile must not be loaded")
    assert directory.exists() and profiles.deleted == []

    profiles.directory = None
    guard = DisposableWindowsProfile(accounts, identity, profiles, tmp_path / "b.json")
    guard.__enter__()
    profiles.directory = directory
    current["sid"] = "S-1-5-21-1-2-3-2000"
    with pytest.raises(CleanupUnverified, match="owner changed"):
        guard.__exit__(None, None, None)
    assert directory.exists() and profiles.deleted == []


def test_new_owned_profile_is_deleted_and_cleanup_is_durable(tmp_path):
    identity, _, accounts, profiles = setup(tmp_path)
    manifest = tmp_path / "profile.json"
    with DisposableWindowsProfile(accounts, identity, profiles, manifest) as guard:
        guard.before_logon(identity.sid)
        directory = tmp_path / "new-profile"
        directory.mkdir()
        profiles.directory = directory
    assert profiles.deleted == [identity.sid] and not directory.exists()
    receipt = json.loads(manifest.read_text(encoding="utf-8"))
    assert receipt["state"] == "cleaned" and receipt["sid"] == identity.sid
    assert receipt["path"] == str(directory)
    assert "password" not in receipt
    with pytest.raises(CleanupUnverified):
        guard.before_logon(identity.sid)
def retained_profile(tmp_path):
    from orchestrator.config_json import read_config_json, write_config_json

    identity, current, accounts, profiles = setup(tmp_path)
    identifier = "01234567" + "a" * 24
    marker = "HASHI disposable permission probe " + identifier
    identity._value["marker"] = current["comment"] = marker
    account = tmp_path / "account.json"
    account.write_text(json.dumps({"version": 1, "id": identifier, "name": identity.name,
                                  "sid": identity.sid, "marker": marker, "state": "created"}))
    manifest = tmp_path / "profile.json"
    guard = DisposableWindowsProfile(accounts, identity, profiles, manifest)
    guard.__enter__()
    directory = tmp_path / "owned-profile"
    directory.mkdir()
    profiles.directory = directory
    record = read_config_json(manifest)
    record.update(path=str(directory), state="created")
    write_config_json(manifest, record)
    return current, accounts, profiles, account, manifest, directory


def test_retained_profile_waits_for_windows_release_then_recovers_exact_owner(tmp_path):
    from orchestrator.config_json import read_config_json
    from scripts.permissions import windows_test_profile as subject

    _, accounts, profiles, account, manifest, directory = retained_profile(tmp_path)
    profiles.loaded = lambda _sid: True
    before = manifest.read_bytes()
    with pytest.raises(CleanupUnverified, match="still loaded"):
        subject.recover_profile(accounts, profiles, account, manifest)
    assert profiles.deleted == [] and directory.exists() and manifest.read_bytes() == before
    profiles.loaded = lambda _sid: False
    subject.recover_profile(accounts, profiles, account, manifest)
    assert not directory.exists() and read_config_json(manifest)["state"] == "cleaned"
    assert profiles.deleted == [json.loads(account.read_text())["sid"]]
    subject.recover_profile(accounts, profiles, account, manifest)
    assert len(profiles.deleted) == 1


@pytest.mark.parametrize("changed", ["account", "path"])
def test_retained_profile_refuses_replaced_identity_or_profile_path(tmp_path, changed):
    from scripts.permissions import windows_test_profile as subject

    current, accounts, profiles, account, manifest, directory = retained_profile(tmp_path)
    if changed == "account":
        current["sid"] = "S-1-5-21-1-2-3-2000"
    else:
        profiles.directory = tmp_path / "replacement"
        profiles.directory.mkdir()
    before = manifest.read_bytes()
    with pytest.raises(CleanupUnverified):
        subject.recover_profile(accounts, profiles, account, manifest)
    assert directory.exists() and not profiles.deleted and manifest.read_bytes() == before


def test_retained_profile_conflict_after_delete_is_not_retried_or_overwritten(tmp_path):
    from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
    from scripts.permissions import windows_test_profile as subject

    _, accounts, profiles, account, manifest, directory = retained_profile(tmp_path)
    delete = profiles.delete

    def concurrent_change(sid):
        delete(sid)
        other = read_config_json(manifest)
        other["state"] = "intent"
        write_config_json(manifest, other)

    profiles.delete = concurrent_change
    with pytest.raises(ConfigConflictError):
        subject.recover_profile(accounts, profiles, account, manifest)
    assert not directory.exists() and len(profiles.deleted) == 1
    assert read_config_json(manifest)["state"] == "intent"

# HASHI-Fingerprint: hfp_d750cb0376894d6580593c46592f148c
