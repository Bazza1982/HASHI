"""Qualification tools must not adopt or remove another service/account right."""
import pytest

from orchestrator.config_json import ConfigConflictError, read_config_json
from scripts.permissions.windows_test_identity import CleanupUnverified
from scripts.permissions.windows_test_service import (
    DisposableTestService, TestServiceSpec, disposable_service_logon,
)


class Services:
    def __init__(self):
        self.observed, self.calls = None, []
        self.fail_hardening = False
        self.replace_on_stop = False

    def query(self, _name):
        return dict(self.observed) if self.observed else None

    def create(self, spec, password):
        assert password == "memory-only"
        self.calls.append("create")
        self.observed = spec.facts

    def harden(self, _name):
        self.calls.append("harden")
        if self.fail_hardening:
            raise OSError("hardening failed")

    def stop(self, _name):
        self.calls.append("stop")
        if self.replace_on_stop:
            self.observed = {**self.observed, "sid": "different-owner"}

    def delete(self, _name):
        self.calls.append("delete")
        self.observed = None


def spec():
    return TestServiceSpec("HashiSafetyTest_abcd1234", ("fixed-python.exe", "-I", "fixed-entry.py"),
                           r".\HashiProbe_1234abcd", "S-1-5-21-1-2-3-1001",
                           "HASHI disposable SCM probe abcdef")


def test_successful_registration_has_no_password_in_journal_and_cleanup_is_confirmed(tmp_path):
    api, path = Services(), tmp_path / "owned.json"
    with DisposableTestService(api, spec(), "memory-only", path):
        record = read_config_json(path)
        assert record["state"] == "created"
        assert "memory-only" not in path.read_text()
    assert api.observed is None
    assert read_config_json(path)["state"] == "cleaned"


def test_existing_registration_is_preserved(tmp_path):
    api = Services()
    api.observed = spec().facts
    with pytest.raises(CleanupUnverified, match="already exists"):
        with DisposableTestService(api, spec(), "memory-only", tmp_path / "owned.json"):
            pytest.fail("adopted another service")
    assert api.calls == [] and api.observed == spec().facts


def test_failed_hardening_rolls_back_only_confirmed_new_registration(tmp_path):
    api = Services()
    api.fail_hardening = True
    with pytest.raises(OSError, match="hardening failed"):
        with DisposableTestService(api, spec(), "memory-only", tmp_path / "owned.json"):
            pytest.fail("hardening failed open")
    assert api.observed is None and api.calls[-2:] == ["stop", "delete"]


def test_replacement_during_stop_is_preserved_and_left_unverified(tmp_path):
    api = Services()
    path = tmp_path / "owned.json"
    with pytest.raises(CleanupUnverified, match="changed during stop"):
        with DisposableTestService(api, spec(), "memory-only", path):
            api.replace_on_stop = True
    assert "delete" not in api.calls
    assert read_config_json(path)["state"] == "created"


def test_journal_conflict_after_create_retains_registration_for_reconciliation(tmp_path, monkeypatch):
    from scripts.permissions import windows_test_service as subject
    write = subject.write_config_json
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise ConfigConflictError("operator changed journal")
        return write(*args)

    monkeypatch.setattr(subject, "write_config_json", fail_second)
    api = Services()
    with pytest.raises(CleanupUnverified, match="needs reconciliation"):
        with DisposableTestService(api, spec(), "memory-only", tmp_path / "owned.json"):
            pytest.fail("continued after conflict")
    assert api.calls == ["create"] and api.observed == spec().facts


def test_production_service_and_shared_account_names_are_rejected():
    with pytest.raises(ValueError, match="test service"):
        TestServiceSpec("HASHI4", ("python.exe",), spec().account, spec().sid, spec().marker)
    with pytest.raises(ValueError, match="disposable local"):
        TestServiceSpec(spec().name, ("python.exe",), r".\Administrator", spec().sid, spec().marker)


def test_only_new_service_logon_right_is_removed():
    class Rights:
        values = {"SeBatchLogonRight"}
        def list(self, _sid):
            return set(self.values)
        def change(self, _sid, *, remove):
            if remove:
                self.values.remove("SeServiceLogonRight")
            else:
                self.values.add("SeServiceLogonRight")

    rights = Rights()
    with disposable_service_logon(rights, sid=spec().sid):
        assert rights.values == {"SeServiceLogonRight", "SeBatchLogonRight"}
    assert rights.values == {"SeBatchLogonRight"}
    rights.values.add("SeServiceLogonRight")
    with pytest.raises(CleanupUnverified, match="already has"):
        with disposable_service_logon(rights, sid=spec().sid):
            pytest.fail("adopted existing account right")
    assert "SeServiceLogonRight" in rights.values
