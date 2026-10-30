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
