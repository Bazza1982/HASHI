"""Endpoint routing and ACL contracts; real LPAC handshakes are separate."""
import pytest

from orchestrator.permissions.windows_ipc import (
    PipePeerPolicy, _pipe_security_descriptor, _pipe_server_name,
)

SERVICE = "S-1-5-21-1-2-3-1001"
RUNNER = "S-1-5-21-1-2-3-1002"
APP = "S-1-15-2-1-2-3-4-5-6-7"
ENDPOINT = r"\\.\pipe\LOCAL\HASHI.Permissions.execution.fixture"


def test_appcontainer_local_alias_has_a_session_and_sid_scoped_server_name():
    policy = PipePeerPolicy(RUNNER, APP, process_id=123, require_lpac=True)
    assert _pipe_server_name(ENDPOINT, policy, 0) == (
        r"\\.\pipe\Sessions\0\AppContainerNamedObjects" + "\\" + APP +
        r"\HASHI.Permissions.execution.fixture"
    )
    assert _pipe_server_name(ENDPOINT, policy, 1) != _pipe_server_name(ENDPOINT, policy, 0)
    other = PipePeerPolicy(RUNNER, APP + "-8", process_id=124, require_lpac=True)
    assert _pipe_server_name(ENDPOINT, other, 0) != _pipe_server_name(ENDPOINT, policy, 0)


def test_ordinary_role_keeps_the_existing_logical_pipe_and_integrity_label():
    policy = PipePeerPolicy(RUNNER)
    assert _pipe_server_name(ENDPOINT, policy, 0) == ENDPOINT
    assert _pipe_security_descriptor(SERVICE, policy) == (
        f"D:P(A;;GA;;;{SERVICE})(A;;GRGW;;;{RUNNER})"
    )


def test_lowbox_pipe_admits_both_access_checks_without_admitting_everyone():
    policy = PipePeerPolicy(RUNNER, APP, process_id=123, require_lpac=True)
    security = _pipe_security_descriptor(SERVICE, policy)
    assert security == (
        f"D:P(A;;GA;;;{SERVICE})(A;;GRGW;;;{RUNNER})(A;;GRGW;;;{APP})"
        "S:(ML;;NW;;;LW)"
    )
    assert ";;;WD)" not in security and ";;;AC)" not in security


@pytest.mark.parametrize("session", [-1, True, 2**32, "0"])
def test_untrusted_session_values_cannot_select_another_namespace(session):
    with pytest.raises(ValueError):
        _pipe_server_name(ENDPOINT, PipePeerPolicy(RUNNER, APP), session)


def test_a_path_supplied_by_a_caller_cannot_become_a_server_namespace():
    with pytest.raises(ValueError):
        _pipe_server_name(r"\\.\pipe\Sessions\0\other", PipePeerPolicy(RUNNER, APP), 0)
# HASHI-Fingerprint: hfp_ae93d79a15dd4b40b387cad3b9f764ce
