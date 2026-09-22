import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import workzone as workzone_module
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.workzone import (
    access_roots_for_workzone_snapshot_lexically,
    access_root_for_workzone,
    access_roots_for_workzones,
    build_workzone_prompt,
    clear_workzone,
    load_workzone,
    normalize_workzone_state,
    preflight_request_paths,
    resolve_workzone_input,
    save_workzone,
)
from tools.builtins import _resolve_path
from tools.registry import ToolRegistry
from tools.schemas import ALL_TOOL_NAMES


def test_workzone_off_has_no_prompt(tmp_path: Path):
    workspace = tmp_path / "agent"
    workspace.mkdir()

    assert load_workzone(workspace) is None
    assert build_workzone_prompt(load_workzone(workspace), workspace) is None


def test_workzone_set_and_clear(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = project / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir()

    resolved = resolve_workzone_input("repo", project, workspace)
    assert resolved == zone.resolve()

    save_workzone(workspace, resolved)
    assert load_workzone(workspace) == zone.resolve()
    section = build_workzone_prompt(load_workzone(workspace), workspace)
    assert section is not None
    assert section[0] == "WORKZONE"
    assert str(zone.resolve()) in section[1]
    assert "Ignore the agent home workspace" in section[1]
    assert "does not currently have filesystem tools" not in section[1]

    clear_workzone(workspace)
    assert load_workzone(workspace) is None


def test_workzone_keeps_default_access_root_when_inside_scope(tmp_path: Path):
    project = tmp_path / "project"
    zone = project / "repo"
    zone.mkdir(parents=True)

    assert access_root_for_workzone(project, zone) == project.resolve()


def test_workzone_uses_zone_as_access_root_when_outside_scope(tmp_path: Path):
    project = tmp_path / "project"
    zone = tmp_path / "external"
    project.mkdir()
    zone.mkdir()

    assert access_root_for_workzone(project, zone) == zone.resolve()


def test_request_path_preflight_blocks_an_explicit_outside_location(tmp_path: Path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside" / "project"
    allowed.mkdir()

    decision = preflight_request_paths(
        f"Inspect `{outside}` and fix the project.",
        access_roots=(allowed,),
        workspace_dir=allowed,
    )

    assert decision.blocked is True
    assert decision.outside_paths == (str(outside),)
    assert "outside this Session's Workspace/Workzones" in decision.clarification


def test_request_path_preflight_allows_locations_inside_an_exact_root(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    decision = preflight_request_paths(
        f"Inspect `{allowed / 'project'}`.",
        access_roots=(allowed,),
        workspace_dir=allowed,
    )

    assert decision.blocked is False
    assert decision.outside_paths == ()


@pytest.mark.parametrize(
    "input_text",
    [
        "Explain `/stop` and why it releases the Worker.",
        "Review https://example.com/outside/path before answering.",
    ],
)
def test_request_path_preflight_does_not_treat_commands_or_urls_as_paths(
    tmp_path: Path,
    input_text: str,
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    decision = preflight_request_paths(
        input_text,
        access_roots=(allowed,),
        workspace_dir=allowed,
    )

    assert decision.requested_paths == ()
    assert decision.blocked is False


def test_preflight_projects_frozen_workzone_roots_without_filesystem_access(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    state = {
        "slots": [
            {
                "slot_id": "main",
                "path": str(allowed),
                "enabled": True,
                "available": True,
            }
        ]
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preflight touched the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)

    assert access_roots_for_workzone_snapshot_lexically(
        workspace,
        state,
        workspace_dir=workspace,
    ) == (allowed,)


def test_tool_scope_rejects_outside_path_before_resolving_it(
    tmp_path: Path, monkeypatch
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    original_resolve = Path.resolve
    resolved = []

    def guarded_resolve(path, *args, **kwargs):
        resolved.append(path)
        if path == outside:
            raise AssertionError("outside target reached the filesystem resolver")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    with pytest.raises(ValueError, match="outside the allowed access scopes"):
        _resolve_path(str(outside), allowed, allowed)

    assert outside not in resolved


def test_workzone_rejects_file_paths(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    file_path = project / "README.md"
    workspace.mkdir(parents=True)
    file_path.write_text("hello", encoding="utf-8")

    try:
        resolve_workzone_input(str(file_path), project, workspace)
    except ValueError as exc:
        assert "file, not a directory" in str(exc)
    else:
        raise AssertionError("file path should be rejected")


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="WSL path translation contract")
def test_workzone_accepts_windows_absolute_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workzone_module, "is_wsl", lambda: True)
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = Path("/mnt/c/Users/tester/projects/demo")
    workspace.mkdir(parents=True)
    zone.mkdir(parents=True, exist_ok=True)

    resolved = resolve_workzone_input(r"C:\Users\tester\projects\demo", project, workspace)

    assert resolved == zone.resolve()


def test_workzone_accepts_windows_relative_separators(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = project / "nested" / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir(parents=True)

    resolved = resolve_workzone_input(r"nested\repo", project, workspace)

    assert resolved == zone.resolve()


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="WSL path translation contract")
def test_workzone_accepts_windows_wsl_unc_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workzone_module, "is_wsl", lambda: True)
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = tmp_path / "wsl-home" / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir(parents=True)
    unc_path = r"\\wsl.localhost\Ubuntu-22.04" + "\\" + str(zone.resolve()).lstrip("/").replace("/", "\\")

    resolved = resolve_workzone_input(unc_path, project, workspace)

    assert resolved == zone.resolve()


def test_workzone_prompt_for_backend_without_file_access(tmp_path: Path):
    workspace = tmp_path / "agent"
    zone = tmp_path / "repo"
    workspace.mkdir()
    zone.mkdir()

    section = build_workzone_prompt(zone, workspace, can_access_files=False)

    assert section is not None
    assert "does not currently have filesystem tools" in section[1]
    assert "do not claim to inspect files" in section[1]


def test_tool_registry_uses_exact_workzone_root_inside_project_scope(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = project / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir()

    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.current_backend = SimpleNamespace(tool_registry=None)
    manager.secrets = {}
    manager.global_config = SimpleNamespace(authorized_id=123)
    manager.config = SimpleNamespace(name="agent", telegram_token_key="agent")
    manager.logger = SimpleNamespace(error=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
    adapter_cfg = SimpleNamespace(
        name="agent",
        extra={"workzone_dir": str(zone)},
        workspace_dir=workspace,
        resolve_access_root=lambda: project,
    )

    manager._attach_tool_registry({"allowed": ["bash"]}, adapter_cfg)

    assert manager.current_backend.tool_registry.workspace_dir == zone.resolve()
    assert manager.current_backend.tool_registry.access_root == zone.resolve()
    assert manager.current_backend.tool_registry.access_roots == (zone.resolve(),)


def test_tool_registry_uses_external_workzone_as_access_root_when_outside_scope(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = tmp_path / "external"
    workspace.mkdir(parents=True)
    zone.mkdir()

    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.current_backend = SimpleNamespace(tool_registry=None)
    manager.secrets = {}
    manager.global_config = SimpleNamespace(authorized_id=123)
    manager.config = SimpleNamespace(name="agent", telegram_token_key="agent")
    manager.logger = SimpleNamespace(error=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
    adapter_cfg = SimpleNamespace(
        name="agent",
        extra={"workzone_dir": str(zone)},
        workspace_dir=workspace,
        resolve_access_root=lambda: project,
    )

    manager._attach_tool_registry({"allowed": ["bash"]}, adapter_cfg)

    assert manager.current_backend.tool_registry.workspace_dir == zone.resolve()
    assert manager.current_backend.tool_registry.access_root == zone.resolve()


def test_multi_workzone_roots_never_widen_to_common_parent(tmp_path: Path):
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    one = tmp_path / "client-a"
    two = tmp_path / "client-b"
    for path in (workspace, one, two):
        path.mkdir(parents=True)
    state = normalize_workzone_state(
        {
            "slots": [
                {"slot_id": "main", "path": str(one), "enabled": True},
                {"slot_id": "1", "path": str(two), "enabled": True},
            ]
        }
    )

    roots = access_roots_for_workzones(project, state, workspace_dir=workspace)

    assert roots == (one.resolve(), two.resolve())
    assert tmp_path.resolve() not in roots


@pytest.mark.asyncio
async def test_file_tools_accept_each_exact_root_but_reject_their_common_parent(
    tmp_path: Path,
):
    one = tmp_path / "client-a"
    two = tmp_path / "client-b"
    one.mkdir()
    two.mkdir()
    (one / "one.txt").write_text("one", encoding="utf-8")
    (two / "two.txt").write_text("two", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    registry = ToolRegistry(
        allowed_tools=["file_read"],
        access_root=one,
        access_roots=[one, two],
        workspace_dir=one,
        secrets={},
    )

    relative = await registry.execute("file_read", {"path": "one.txt"})
    attached = await registry.execute("file_read", {"path": str(two / "two.txt")})
    rejected = await registry.execute("file_read", {"path": str(outside)})

    assert relative.is_error is False and relative.output.endswith("\none")
    assert attached.is_error is False and attached.output.endswith("\ntwo")
    assert rejected.is_error is True
    assert "outside the allowed access scopes" in rejected.output


def test_multi_workzone_prompt_lists_only_enabled_slots(tmp_path: Path):
    workspace = tmp_path / "agent"
    main = tmp_path / "main"
    attached = tmp_path / "attached"
    disabled = tmp_path / "disabled"
    for path in (workspace, main, attached, disabled):
        path.mkdir()
    state = {
        "revision": 18,
        "slots": [
            {"slot_id": "main", "path": str(main), "enabled": True},
            {"slot_id": "1", "path": str(attached), "enabled": True},
            {"slot_id": "2", "path": str(disabled), "enabled": False},
        ],
    }

    section = build_workzone_prompt(state, workspace)

    assert section is not None and section[0] == "WORKZONES"
    entries = [
        json.loads(line.removeprefix("- "))
        for line in section[1].splitlines()
        if line.startswith("- {")
    ]
    assert {entry["path"] for entry in entries} == {
        str(main.resolve()),
        str(attached.resolve()),
    }
    assert str(disabled.resolve()) not in {entry["path"] for entry in entries}
    assert "revision" not in section[1].lower()


def test_tool_registry_wildcard_survives_global_default_merge():
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager._agents_json_global = {
        "default_tools": {"allowed": ["telegram_send_file"], "max_loops": 5}
    }
    warnings = []
    manager.logger = SimpleNamespace(
        warning=lambda *args, **kwargs: warnings.append((args, kwargs))
    )

    merged = manager._resolve_tools_config(
        {"tools": {"allowed": ["*"], "max_loops": 25}}
    )

    assert merged == {"allowed": ["*"]}
    assert len(warnings) == 1


def test_tool_registry_defaults_to_open_wildcard_without_instance_override():
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager._agents_json_global = {}
    manager.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

    merged = manager._resolve_tools_config({})

    assert merged == {"allowed": ["*"]}


def test_tool_registry_preserves_explicit_instance_restriction():
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager._agents_json_global = {
        "default_tools": {"allowed": ["wiki_search"]}
    }
    manager.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

    merged = manager._resolve_tools_config({})

    assert merged == {"allowed": ["wiki_search"]}


def test_tool_registry_fails_closed_after_global_config_read_failure():
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager._agents_json_global = None
    manager.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

    assert manager._resolve_tools_config({}) is None


def test_tool_registry_wildcard_excludes_explicit_opt_in_tools(tmp_path: Path):
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.current_backend = SimpleNamespace(tool_registry=None)
    manager.secrets = {}
    manager.global_config = SimpleNamespace(authorized_id=123)
    manager.config = SimpleNamespace(name="agent", telegram_token_key="agent")
    manager.logger = SimpleNamespace(error=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
    adapter_cfg = SimpleNamespace(
        name="agent",
        extra={},
        workspace_dir=tmp_path,
        resolve_access_root=lambda: tmp_path,
    )

    manager._attach_tool_registry({"allowed": ["*"], "max_loops": 25}, adapter_cfg)

    registry = manager.current_backend.tool_registry
    assert registry.max_loops is None
    assert set(registry._allowed) == set(ALL_TOOL_NAMES) - {
        "media_read",
        "vision_inspect",
    }
