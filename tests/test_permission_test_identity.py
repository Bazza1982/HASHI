"""Disposable Windows test identities must never modify another account."""
import json

import pytest

from scripts.permissions.windows_test_identity import (
    CleanupUnverified,
    DisposableTestIdentity,
    WindowsAccounts,
    recover_identity,
)


class FakeAccounts:
    def __init__(self):
        self.users = {}
        self.deleted = []
        self.admin = False

    def create(self, name, password, comment):
        if name in self.users:
            raise RuntimeError("already exists")
        self.users[name] = {"sid": "S-1-5-21-1-2-3-1101", "comment": comment,
                            "password": password}

    def info(self, name):
        return self.users.get(name)

    def group_sids(self, name):
        assert name in self.users
        return {"S-1-5-32-544"} if self.admin else {"S-1-5-32-545"}

    def delete(self, name):
        self.deleted.append(name)
        del self.users[name]


def test_identity_is_created_nonadmin_and_deleted_without_persisting_password(tmp_path):
    api = FakeAccounts()
    manifest = tmp_path / "identity.json"
    identity = DisposableTestIdentity(api, manifest)
    with identity:
        name = identity.name
        secret = identity.password
        assert name in api.users
        assert identity.sid == api.users[name]["sid"]
        assert secret not in manifest.read_text(encoding="utf-8")
        assert "password" not in manifest.read_text(encoding="utf-8")
    assert api.deleted == [name]
    assert not api.users
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "cleaned"


def test_failed_nonadmin_check_cleans_up_the_owned_account(tmp_path):
    api = FakeAccounts()
    api.admin = True
    identity = DisposableTestIdentity(api, tmp_path / "identity.json")
    with pytest.raises(PermissionError):
        with identity:
            pytest.fail("administrator identity must not be used")
    assert api.deleted == [identity.name]
    assert not api.users


def test_cleanup_refuses_replaced_account_even_with_same_name(tmp_path):
    api = FakeAccounts()
    identity = DisposableTestIdentity(api, tmp_path / "identity.json")
    identity.__enter__()
    api.users[identity.name]["comment"] = "different owner"
    with pytest.raises(CleanupUnverified):
        identity.__exit__(None, None, None)
    assert api.deleted == []
    assert identity.name in api.users


def test_recovery_only_deletes_account_matching_manifest_and_sid(tmp_path):
    api = FakeAccounts()
    manifest = tmp_path / "identity.json"
    identity = DisposableTestIdentity(api, manifest)
    identity.__enter__()
    api.users[identity.name]["sid"] = "S-1-5-21-9-9-9-1102"
    with pytest.raises(CleanupUnverified):
        recover_identity(api, manifest)
    assert api.deleted == []
    api.users[identity.name]["sid"] = identity.sid
    recover_identity(api, manifest)
    assert api.deleted == [identity.name]
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "cleaned"


def test_unknown_manifest_never_deletes_accounts(tmp_path):
    api = FakeAccounts()
    manifest = tmp_path / "identity.json"
    manifest.write_text(json.dumps({"name": "Administrator", "state": "created"}), encoding="utf-8")
    with pytest.raises(CleanupUnverified):
        recover_identity(api, manifest)
    assert api.deleted == []


def test_manifest_write_failure_after_account_creation_still_removes_owned_identity(tmp_path, monkeypatch):
    from scripts.permissions import windows_test_identity

    api = FakeAccounts()
    identity = DisposableTestIdentity(api, tmp_path / "identity.json")
    write = windows_test_identity._write_manifest

    def fail_record_once(path, value, *, exclusive=False):
        if value["state"] == "created":
            raise OSError("injected manifest failure")
        return write(path, value, exclusive=exclusive)

    monkeypatch.setattr(windows_test_identity, "_write_manifest", fail_record_once)
    with pytest.raises(OSError, match="injected manifest failure"):
        identity.__enter__()
    assert api.deleted == [identity.name]
    assert not api.users


def test_cleaned_manifest_must_not_hide_a_returned_account(tmp_path):
    api = FakeAccounts()
    manifest = tmp_path / "identity.json"
    identity = DisposableTestIdentity(api, manifest)
    with identity:
        saved = dict(api.users[identity.name])
    api.users[identity.name] = saved
    with pytest.raises(CleanupUnverified):
        recover_identity(api, manifest)
    assert api.deleted == [identity.name]


def test_unverifiable_cleanup_is_reported_and_never_deletes_by_name(tmp_path):
    class UnavailableAccountInfo(FakeAccounts):
        def info(self, name):
            raise OSError("account database unavailable")

    api = UnavailableAccountInfo()
    identity = DisposableTestIdentity(api, tmp_path / "identity.json")
    with pytest.raises(CleanupUnverified):
        identity.__enter__()
    assert api.deleted == []
    assert identity.name in api.users
    assert identity.password is None


def test_ambiguous_create_never_deletes_an_unproven_account(tmp_path):
    class AmbiguousCreate(FakeAccounts):
        def create(self, name, password, comment):
            super().create(name, password, comment)
            raise OSError("creation outcome unknown")

    api = AmbiguousCreate()
    identity = DisposableTestIdentity(api, tmp_path / "identity.json")
    with pytest.raises(CleanupUnverified):
        identity.__enter__()
    assert api.deleted == []
    assert identity.name in api.users
    assert identity.password is None

def test_local_group_resolution_includes_both_machine_and_builtin_aliases(monkeypatch):
    api = WindowsAccounts.__new__(WindowsAccounts)
    api._computer = "LOCALHOST"
    aliases = {
        "LOCALHOST\\Administrators": "S-1-5-21-1-2-3-1001",
        "BUILTIN\\Administrators": "S-1-5-32-544",
    }

    def resolve(name):
        if name not in aliases:
            raise OSError(1332, "not mapped")
        return aliases[name]

    monkeypatch.setattr(api, "_sid", resolve)
    assert api._local_group_sids("Administrators") == set(aliases.values())
    with pytest.raises(CleanupUnverified):
        api._local_group_sids("Unknown")


def test_group_resolution_does_not_hide_system_errors(monkeypatch):
    api = WindowsAccounts.__new__(WindowsAccounts)
    api._computer = "LOCALHOST"

    def fail(name):
        raise OSError(5, "access denied")

    monkeypatch.setattr(api, "_sid", fail)
    with pytest.raises(OSError) as error:
        api._local_group_sids("Administrators")
    assert error.value.errno == 5
