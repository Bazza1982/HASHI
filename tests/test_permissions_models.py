"""Permission configuration and scope contracts (PAO, Functions)."""
import pytest

from orchestrator.permissions.models import (
    ExecutionContext,
    PermissionLevel,
    PermissionSettings,
    Scope,
    canonical_scope_set,
    normalize_scope,
    scope_covers,
    scope_digest,
)


def test_legacy_and_explicit_l0_configuration_are_equivalent(tmp_path):
    before = tuple(tmp_path.iterdir())
    legacy = PermissionSettings.from_mapping(None)
    explicit = PermissionSettings.from_mapping({"requested_level": "L0"})

    assert legacy == explicit
    assert legacy.requested_level is PermissionLevel.L0
    assert legacy.task_default_seconds == 4 * 60 * 60
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("value", ["L3", "l1", "", None, True, 1])
def test_invalid_explicit_level_never_silently_falls_back_to_l0(value):
    with pytest.raises(ValueError):
        PermissionSettings.from_mapping({"requested_level": value})


@pytest.mark.parametrize("value", [0, -1, True, "14400", 0.5])
def test_invalid_duration_is_rejected_instead_of_defaulted(value):
    with pytest.raises(ValueError):
        PermissionSettings.from_mapping({"task_default_seconds": value})


def test_context_requires_all_trusted_bindings():
    fields = dict(
        instance_id="instance",
        owner_id="owner",
        agent_id="agent",
        task_id="task",
        run_id="run",
        session_id="session",
        turn_id="turn",
        worker_generation="worker",
        context_generation="context",
        execution_epoch="epoch",
        backend_id="herv3",
        executor_id="registry",
        policy_revision=1,
    )
    context = ExecutionContext(**fields)
    assert context.task_id == "task"
    assert context.run_id == "run"
    with pytest.raises(ValueError):
        ExecutionContext(**{**fields, "execution_epoch": ""})
    with pytest.raises(ValueError):
        ExecutionContext(**{**fields, "policy_revision": True})


def test_file_scope_normalizes_traversal_and_does_not_match_sibling(tmp_path):
    permitted = tmp_path / "project"
    permitted.mkdir()
    child = permitted / "child"
    child.mkdir()
    sibling = tmp_path / "project-extra"
    sibling.mkdir()

    parent = normalize_scope("file", str(permitted), ["read", "list"], cwd=tmp_path)
    inner = normalize_scope("file", "project/child/../child", ["read"], cwd=tmp_path)
    outside = normalize_scope("file", str(sibling), ["read"], cwd=tmp_path)
    write = normalize_scope("file", str(child), ["write"], cwd=tmp_path)

    assert scope_covers(parent, inner)
    assert not scope_covers(parent, outside)
    assert not scope_covers(parent, write)


def test_scope_set_is_stable_but_scope_changes_change_the_digest(tmp_path):
    root = normalize_scope("file", str(tmp_path), ["list", "read", "read"], cwd=tmp_path)
    same = normalize_scope("file", str(tmp_path), ["read", "list"], cwd=tmp_path)
    network = normalize_scope("network", "https://EXAMPLE.com", ["fetch"], cwd=tmp_path)

    assert root == same
    assert canonical_scope_set([network, root, same]) == canonical_scope_set([root, network])
    assert scope_digest([network, root, same]) == scope_digest([root, network])
    assert scope_digest([root]) != scope_digest([network, root])


def test_network_scopes_bind_scheme_host_port_and_action(tmp_path):
    grant = normalize_scope("network", "https://example.com", ["fetch"], cwd=tmp_path)
    equivalent = normalize_scope("network", "https://EXAMPLE.com:443/", ["fetch"], cwd=tmp_path)
    different_port = normalize_scope("network", "https://example.com:8443", ["fetch"], cwd=tmp_path)
    upload = normalize_scope("network", "https://example.com", ["upload"], cwd=tmp_path)

    assert scope_covers(grant, equivalent)
    assert not scope_covers(grant, different_port)
    assert not scope_covers(grant, upload)


@pytest.mark.parametrize("resource", [
    "https://user:secret@example.com",
    "https://example.com/a",
    "https://*.example.com",
    "ftp://example.com",
    "https://example.com?token=secret",
])
def test_ambiguous_network_scope_fails_closed(resource, tmp_path):
    with pytest.raises(ValueError):
        normalize_scope("network", resource, ["fetch"], cwd=tmp_path)


def test_unknown_scope_or_action_is_not_granted_by_wildcard(tmp_path):
    with pytest.raises(ValueError):
        normalize_scope("unknown", "resource", ["read"], cwd=tmp_path)
    with pytest.raises(ValueError):
        normalize_scope("file", str(tmp_path), ["*"], cwd=tmp_path)
    with pytest.raises(ValueError):
        Scope(kind="file", resource=str(tmp_path), actions=())


def test_invalid_direct_settings_and_unknown_keys_do_not_weaken_controls():
    with pytest.raises(ValueError):
        PermissionSettings(requested_level="L3")
    with pytest.raises(ValueError):
        PermissionSettings(task_default_seconds=0)
    with pytest.raises(ValueError):
        PermissionSettings.from_mapping({"requested_levl": "L1"})


def test_an_empty_scope_cannot_be_used_as_an_approval_digest():
    with pytest.raises(ValueError):
        scope_digest([])
