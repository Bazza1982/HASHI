from pathlib import Path
from types import SimpleNamespace

from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.workzone import (
    access_root_for_workzone,
    build_workzone_prompt,
    clear_workzone,
    load_workzone,
    resolve_workzone_input,
    save_workzone,
)


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


def test_workzone_accepts_windows_absolute_paths(tmp_path: Path):
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


def test_workzone_accepts_windows_wsl_unc_paths(tmp_path: Path):
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


def test_tool_registry_uses_workzone_as_workspace_but_keeps_project_access_root(tmp_path: Path):
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
    assert manager.current_backend.tool_registry.access_root == project.resolve()


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
