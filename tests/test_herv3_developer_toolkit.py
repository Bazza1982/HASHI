"""Non-production acceptance tests for the HERV3 daily/developer toolkit."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.registry import ToolRegistry
from tools.tool_presentation import OnDemandToolView, on_demand_requested
from tools.developer_toolkit_schemas import TOOLKIT_TOOL_NAMES, TOOLKIT_READ_ONLY_TOOLS


def _registry(root: Path, *, allowed=None, tool_options=None) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=list(allowed or TOOLKIT_TOOL_NAMES),
        access_root=root,
        workspace_dir=root,
        secrets={},
        tool_options=tool_options or {},
    )


async def _run(registry, name: str, args: dict) -> dict:
    result = await registry.execute(name, args, tool_call_id=f"test-{name}")
    return json.loads(result.output)


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def test_toolkit_schemas_and_permissions(tmp_path):
    registry = _registry(tmp_path)
    assert set(TOOLKIT_TOOL_NAMES).issubset(set(registry.allowed_tool_names()))
    assert {
        schema["function"]["name"]
        for schema in registry.get_tool_definitions(tiers=["development", "daily"])
    } >= set(TOOLKIT_TOOL_NAMES)
    for name in TOOLKIT_TOOL_NAMES:
        assert registry.is_read_only(name) is (name in TOOLKIT_READ_ONLY_TOOLS)
    assert not on_demand_requested({}, model="qwen3.5-9b", is_herv3=True)
    assert on_demand_requested(
        {"herv3_tool_presentation": {"mode": "on_demand"}},
        model="deepseek-v4-pro", is_herv3=True
    )
    assert not on_demand_requested(
        {"herv3_tool_presentation": {"mode": "on_demand"}},
        model="qwen3.5-9b", is_herv3=False
    )


@pytest.mark.asyncio
async def test_model_finds_and_reveals_tools_without_changing_authority(tmp_path):
    registry = _registry(
        tmp_path, allowed=["tool_find", "shell", "file_read", "workspace_inspect", "git_inspect"]
    )
    view = OnDemandToolView(registry)
    before = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert "tool_find" in before
    assert "workspace_inspect" not in before
    outcome = await view.execute("tool_find", {"pack": "verification"}, "find")
    assert not outcome.is_error
    announced = json.loads(outcome.output)
    assert "workspace_inspect" in {item["name"] for item in announced["tools"]}
    after = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert "workspace_inspect" in after
    assert view.is_allowed("workspace_inspect")
    assert not view.is_allowed("file_manage")
    assert "file_manage" not in after
    # Presentation never mutates the shared/long-lived Registry's own catalogue.
    assert "workspace_inspect" in {
        item["function"]["name"] for item in registry.get_tool_definitions(tiers=None)
    }


@pytest.mark.asyncio
async def test_on_demand_view_keeps_scoped_long_result_continuation(tmp_path, monkeypatch):
    original = "first section " * 3000 + "final sentinel"
    registry = ToolRegistry(
        ["tool_find", "file_read"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context={
            "instance_id": "i", "agent_name": "agent", "owner_id": "owner",
            "hashi_session_id": "session", "hashi_run_id": "run", "request_id": "request",
        },
    )
    dispatch = registry._dispatch

    async def fixture(name, arguments, *, tool_call_id=""):
        if name == "file_read":
            return original
        return await dispatch(name, arguments, tool_call_id=tool_call_id)

    monkeypatch.setattr(registry, "_dispatch", fixture)
    view = OnDemandToolView(registry)
    names = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert {"tool_find", "tool_catalog", "tool_output_read"} <= names

    result = await view.execute("file_read", {"path": "synthetic.txt"}, "read")
    reference = result.details["output_continuation"]
    offset, parts = 0, []
    while True:
        page = await view.execute("tool_output_read", {"reference": reference, "offset": offset}, "page")
        assert not page.is_error
        decoded = json.loads(page.output)
        parts.append(decoded["text"])
        if decoded["complete"]:
            break
        assert decoded["next_offset"] > offset
        offset = decoded["next_offset"]
    assert "".join(parts) == original

    denied = await view.execute_with_audit_context(
        "tool_output_read", {"reference": reference, "offset": 0}, "other",
        audit_context={"hashi_run_id": "different-run", "request_id": "different-request"},
    )
    assert denied.is_error


def test_on_demand_view_reflects_live_continuation_binding(tmp_path):
    registry = ToolRegistry(
        ["tool_find", "file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
    )
    view = OnDemandToolView(registry)
    def names():
        return {item["function"]["name"] for item in view.get_tool_definitions()}
    assert "tool_output_read" not in names()
    registry.audit_context.update({"hashi_run_id": "run", "request_id": "request"})
    assert "tool_output_read" in names()
    registry.audit_context.clear()
    assert "tool_output_read" not in names()



@pytest.mark.asyncio
async def test_on_demand_discovery_does_not_forget_earlier_authorized_tools():
    from tools.registry import ToolResult

    class Registry:
        def __init__(self):
            self.names = {"tool_find", *(f"fixture_{index:02}" for index in range(60))}

        def get_tool_definitions(self, tiers=None):
            return [{"function": {"name": name}} for name in sorted(self.names)]

        async def execute(self, _tool, _arguments, call_id):
            return ToolResult(tool_call_id=call_id, output='{"ok": true}')

    base = Registry()
    view = OnDemandToolView(base)
    for start in range(0, 60, 20):
        await view.execute("tool_find", {
            "names": [f"fixture_{index:02}" for index in range(start, start + 20)],
            "limit": 20,
        }, f"find-{start}")
    names = {item["function"]["name"] for item in view.get_tool_definitions()}
    assert {f"fixture_{index:02}" for index in range(60)} <= names
    base.names.remove("fixture_00")
    names = {item["function"]["name"] for item in view.get_tool_definitions()}
    assert "fixture_00" not in names



@pytest.mark.asyncio
async def test_project_locate_uses_repo_name_and_bounded_root(tmp_path):
    root = tmp_path / "Projects"
    target = root / "content_generation"
    target.mkdir(parents=True)
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    _git(target, "init", "--quiet")
    _git(target, "remote", "add", "origin", "https://example.com/Bazza/SOK.git")
    other = root / "unrelated"
    other.mkdir()
    found = await _run(
        _registry(root), "project_locate", {"name": "sok", "root": str(root), "max_depth": 2}
    )
    assert found["ok"] and found["matches"]
    assert found["matches"][0]["path"] == str(target.resolve())
    assert found["matches"][0]["remote_repo"] == "sok"


@pytest.mark.asyncio
async def test_code_search_is_bounded_and_reports_partial(tmp_path):
    (tmp_path / "a.py").write_text("class Alpha:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class AlphaTwo:\n    pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    found = await _run(registry, "code_search", {
        "query": "class Alpha", "mode": "content", "max_results": 1
    })
    assert found["ok"] and found["count"] == 1 and found["partial"]
    assert found["stop_reason"] == "result_limit"
    names = await _run(registry, "code_search", {
        "query": "a.py", "mode": "files", "max_results": 5
    })
    assert names["ok"] and any("a.py" in line for line in names["matches"])


@pytest.mark.asyncio
async def test_git_inspect_read_only_and_remote_credentials_redacted(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "remote", "add", "origin", "https://user:topsecret@example.com/Bazza/SOK.git")
    (tmp_path / "untracked.txt").write_text("x", encoding="utf-8")
    registry = _registry(tmp_path)
    status = await _run(registry, "git_inspect", {"operation": "status"})
    assert status["ok"] and "untracked.txt" in status["output"]
    remotes = await _run(registry, "git_inspect", {"operation": "remote"})
    assert remotes["ok"]
    assert "example.com/Bazza/SOK.git" in remotes["output"]
    assert "topsecret" not in remotes["output"]


@pytest.mark.asyncio
async def test_text_replace_checks_count_and_does_not_overwrite_on_mismatch(tmp_path):
    path = tmp_path / "app.py"
    path.write_text("old\nold\n", encoding="utf-8")
    registry = _registry(tmp_path)
    mismatch = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new"
    })
    assert mismatch["ok"] is False
    assert path.read_text(encoding="utf-8") == "old\nold\n"
    preview = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new",
        "expected_count": 2, "preview_only": True
    })
    assert preview["ok"] and not preview["changed"]
    changed = await _run(registry, "text_replace", {
        "path": "app.py", "find": "old", "replace": "new", "expected_count": 2
    })
    assert changed["ok"] and changed["changed"]
    assert path.read_text(encoding="utf-8") == "new\nnew\n"


@pytest.mark.asyncio
async def test_file_manage_copy_move_mkdir_preserve_existing(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("proof", encoding="utf-8")
    registry = _registry(tmp_path)
    created = await _run(registry, "file_manage", {
        "operation": "mkdir", "destination": "reports"
    })
    assert created["ok"] and (tmp_path / "reports").is_dir()
    copied = await _run(registry, "file_manage", {
        "operation": "copy", "source": "source.txt", "destination": "reports/copy.txt"
    })
    assert copied["ok"] and (tmp_path / "reports/copy.txt").read_text() == "proof"
    collision = await _run(registry, "file_manage", {
        "operation": "copy", "source": "source.txt", "destination": "reports/copy.txt"
    })
    assert collision["ok"] is False and source.read_text() == "proof"
    moved = await _run(registry, "file_manage", {
        "operation": "move", "source": "reports/copy.txt", "destination": "reports/moved.txt"
    })
    assert moved["ok"] and (tmp_path / "reports/moved.txt").is_file()
    assert not (tmp_path / "reports/copy.txt").exists()


@pytest.mark.asyncio
async def test_data_inspect_json_csv_without_model_written_python(tmp_path):
    (tmp_path / "rows.csv").write_text("name,value\nA,1\nB,2\n", encoding="utf-8")
    (tmp_path / "info.json").write_text('{"name":"SOK","ready":true}', encoding="utf-8")
    registry = _registry(tmp_path)
    csv_data = await _run(registry, "data_inspect", {"path": "rows.csv", "limit": 1})
    assert csv_data["ok"] and csv_data["columns"] == ["name", "value"]
    assert csv_data["samples"] == [{"name": "A", "value": "1"}]
    json_data = await _run(registry, "data_inspect", {"path": "info.json"})
    assert json_data["ok"] and set(json_data["keys"]) == {"name", "ready"}
    (tmp_path / "wide.csv").write_text(
        "name,notes\nA," + "x" * 25000 + "\n", encoding="utf-8"
    )
    large = await registry.execute("data_inspect", {"path": "wide.csv"}, "wide")
    assert len(large.output) < 20000
    assert json.loads(large.output)["ok"]


@pytest.mark.asyncio
async def test_no_cross_workzone_access_or_tool_permission_growth(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("private", encoding="utf-8")
    registry = _registry(allowed, allowed=["tool_find", "project_locate", "file_manage"])
    denied = await registry.execute("file_manage", {
        "operation": "copy", "source": str(external / "secret.txt"),
        "destination": "copied.txt"
    }, "outside")
    assert denied.is_error
    assert not (allowed / "copied.txt").exists()
    discovery = await registry.execute(
        "tool_find", {"names": ["file_manage", "web_search", "office_edit"]}, "find"
    )
    names = [item["name"] for item in json.loads(discovery.output)["tools"]]
    assert names == ["file_manage"]
    forbidden = await registry.execute("text_replace", {
        "path": "missing.txt", "find": "x", "replace": "y"
    }, "forbidden")
    assert forbidden.is_error


@pytest.mark.asyncio
async def test_git_inspection_cannot_walk_to_repo_outside_accessible_root(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    repo = tmp_path / "parent_repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    nested = repo / "nested"
    nested.mkdir()
    registry = _registry(nested, allowed=["git_inspect"])
    result = await registry.execute("git_inspect", {"operation": "status"}, "scope")
    assert result.is_error


def test_wildcard_does_not_silently_adopt_toolkit_tools(tmp_path):
    """A '*' allowlist keeps its prior behaviour: the new toolkit schemas do not
    broaden a wildcard instance unless they are listed explicitly (same
    precedent as media_read and vision_inspect)."""
    wildcard = _registry(tmp_path, allowed=["*"])
    for name in TOOLKIT_TOOL_NAMES:
        assert not wildcard.is_allowed(name), name
    assert not wildcard.is_allowed("media_read")
    assert not wildcard.is_allowed("vision_inspect")
    explicit = _registry(tmp_path, allowed=["*", "tool_find", "file_manage"])
    assert explicit.is_allowed("tool_find")
    assert explicit.is_allowed("file_manage")
    assert not explicit.is_allowed("text_replace")

def _with_protected(registry, protected: Path):
    """Attach a minimal live-runtime policy so toolkit tools receive
    protected_read_paths exactly as the production Registry dispatches them."""
    from types import SimpleNamespace
    registry._live_runtime_policy_cache = SimpleNamespace(
        protected_read_paths=[str(protected)],
        qualification_error=None,
        protection_complete=True,
        code_root=None, bridge_home=None, runtime_roots=(),
        protected_write_paths=(), service_targets=(), core_pid=None,
        policy_path=None, configuration_error=None,
    )
    return registry


@pytest.mark.asyncio
async def test_code_search_never_reads_protected_descendants(tmp_path, monkeypatch):
    """P1A: protected files inside an otherwise authorised root must never be
    opened or reported, for the Python fallback and for a real ripgrep run."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "notes.txt").write_text("PROJECT-TOP-SECRET-KEYWORD", encoding="utf-8")
    (tmp_path / "open.py").write_text("# PROJECT-TOP-SECRET-KEYWORD\n", encoding="utf-8")
    registry = _with_protected(_registry(tmp_path), secret)

    # Python fallback (both engines must behave identically).
    monkeypatch.setattr("tools.developer_toolkit.shutil.which", lambda name: None)
    fallback = await _run(registry, "code_search", {
        "query": "PROJECT-TOP-SECRET-KEYWORD", "mode": "content"})
    assert fallback["ok"] and fallback["engine"] == "python_literal"
    assert any("open.py" in line for line in fallback["matches"])
    assert not any("secret" in line for line in fallback["matches"]), fallback
    files_fallback = await _run(registry, "code_search", {
        "query": "notes", "mode": "files"})
    assert files_fallback["ok"]
    assert not any("secret" in line for line in files_fallback["matches"])

    # Real ripgrep when available: the guard inventory must exclude the
    # protected directory before rg opens anything.
    monkeypatch.undo()
    if shutil.which("rg"):
        rg_hit = await _run(registry, "code_search", {
            "query": "PROJECT-TOP-SECRET-KEYWORD", "mode": "content"})
        assert rg_hit["ok"] and rg_hit["engine"] == "ripgrep"
        assert any("open.py" in line for line in rg_hit["matches"])
        assert not any("secret" in line for line in rg_hit["matches"]), rg_hit


@pytest.mark.asyncio
async def test_code_search_guard_inventory_excludes_links_and_protected(tmp_path, monkeypatch):
    """The rg guard walk itself must exclude protected directories, junctions
    and links escaping the authorised roots (no content is read here)."""
    from tools.developer_toolkit import _rg_guard
    import time
    protected = tmp_path / "p"
    protected.mkdir()
    (protected / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
    link = tmp_path / "into_p"
    try:
        link.symlink_to(protected, target_is_directory=True)
        linked = True
    except (OSError, NotImplementedError):
        linked = False
    globs = _rg_guard(tmp_path, (tmp_path,), (protected,), time.monotonic() + 5)
    assert globs is not None
    assert "!p" in globs and "!p/**" in globs
    if linked:
        assert "!into_p" in globs


@pytest.mark.asyncio
async def test_code_search_user_glob_cannot_reinclude_protected_paths(tmp_path, monkeypatch):
    """A user-supplied positive glob must never override the mandatory
    protected/link exclusions (ripgrep last-match-wins). Covers content and
    files modes with a protected descendant and a symlink to a protected
    file, including when ripgrep is configured to follow symlinks."""
    if not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "notes.txt").write_text("needle-public", encoding="utf-8")
    (tmp_path / "open.txt").write_text("needle-public in the open file", encoding="utf-8")
    link = tmp_path / "linked.txt"
    linked = False
    try:
        link.symlink_to(secret / "notes.txt")
        linked = True
    except (OSError, NotImplementedError):
        pass
    # Make ripgrep follow symlinks via its config file so the link exclusion
    # is exercised for real instead of being skipped by rg's default no-follow.
    rg_conf = tmp_path / "rg.conf"
    rg_conf.write_text("--follow\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(rg_conf))
    registry = _with_protected(_registry(tmp_path), secret)

    content = await _run(registry, "code_search", {
        "query": "needle-public", "mode": "content", "glob": "*.txt"})
    assert content["ok"] and content["engine"] == "ripgrep"
    hits = content["matches"]
    assert any("open.txt" in line for line in hits), hits
    assert not any("secret" in line for line in hits), hits
    if linked:
        assert not any("linked.txt" in line for line in hits), hits

    files = await _run(registry, "code_search", {
        "query": "txt", "mode": "files", "glob": "*.txt"})
    assert files["ok"]
    assert any("open.txt" in line for line in files["matches"]), files
    assert not any("secret" in line for line in files["matches"]), files
    if linked:
        assert not any("linked.txt" in line for line in files["matches"]), files


@pytest.mark.asyncio
async def test_project_locate_skips_protected_repositories(tmp_path):
    """P1A: a protected tree containing a matching .git/config must never be
    reported; an authorised sibling repository is still found."""
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    root = tmp_path
    hidden = root / "hidden"
    hidden.mkdir()
    _git(hidden, "init", "--quiet")
    _git(hidden, "remote", "add", "origin", "https://example.com/team/SOK.git")
    visible = root / "ok_repo"
    visible.mkdir()
    _git(visible, "init", "--quiet")
    _git(visible, "remote", "add", "origin", "https://example.com/team/OK.git")
    registry = _with_protected(_registry(root), hidden)
    found = await _run(registry, "project_locate", {"name": "sok", "root": str(root), "max_depth": 2})
    assert found["ok"]
    paths = [m["path"] for m in found["matches"]]
    assert str(hidden.resolve()) not in paths, found
    ok_found = await _run(registry, "project_locate", {"name": "ok", "root": str(root), "max_depth": 2})
    assert ok_found["ok"] and any("ok_repo" in m["path"] for m in ok_found["matches"])


@pytest.mark.asyncio
async def test_git_inspect_rejects_metadata_outside_authorised_scope(tmp_path):
    """P1A: a worktree whose .git pointer lives in a protected location must be
    denied before any Git command runs."""
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    real = tmp_path / "real"
    real.mkdir()
    _git(real, "init", "--quiet")
    (real / "README.md").write_text("# hi\n", encoding="utf-8")
    _git(real, "add", "README.md")
    _git(real, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init", "--quiet")
    wt = tmp_path / "wt"
    wt.mkdir()
    # Simulate a linked worktree whose metadata is the protected real/.git.
    (wt / ".git").write_text("gitdir: " + str(real / ".git") + "\n", encoding="utf-8")
    registry = _registry(wt, allowed=["git_inspect"])
    result = await registry.execute("git_inspect", {"operation": "status"}, "meta")
    assert result.is_error


@pytest.mark.asyncio
async def test_file_manage_never_copies_protected_source_descendants(tmp_path):
    """P1B: a protected descendant of an authorised source tree must abort the
    whole recursive copy before anything is written."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "ok.txt").write_text("ok", encoding="utf-8")
    secret = src / "sub" / "secret.txt"
    secret.write_text("PRIVATE", encoding="utf-8")
    registry = _with_protected(_registry(tmp_path), secret)
    copied = await _run(registry, "file_manage", {
        "operation": "copy", "source": "src", "destination": "dst", "recursive": True})
    assert copied["ok"] is False, copied
    assert not (tmp_path / "dst").exists() or not any((tmp_path / "dst").iterdir())
    assert secret.read_text(encoding="utf-8") == "PRIVATE"


@pytest.mark.asyncio
async def test_file_manage_never_writes_through_destination_links(tmp_path):
    """P1B: an existing destination symlink/junction to an outside or
    protected file must never be followed and overwritten."""
    src = tmp_path / "source.txt"
    src.write_text("harmless", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL", encoding="utf-8")
    link = tmp_path / "dest.txt"
    link.symlink_to(outside)
    registry = _with_protected(_registry(tmp_path), outside)
    result = await _run(registry, "file_manage", {
        "operation": "copy", "source": "source.txt", "destination": "dest.txt", "overwrite": True})
    assert result["ok"] is False, result
    assert outside.read_text(encoding="utf-8") == "ORIGINAL"
    assert link.is_symlink()


@pytest.mark.asyncio
async def test_file_manage_directory_copy_works_when_clean(tmp_path):
    """P1B: ordinary recursive copy still succeeds after the guards."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "sub" / "b.txt").write_text("b", encoding="utf-8")
    registry = _registry(tmp_path)
    copied = await _run(registry, "file_manage", {
        "operation": "copy", "source": "src", "destination": "dst", "recursive": True})
    assert copied["ok"] and copied["copied_files"] == 2
    assert (tmp_path / "dst" / "a.txt").read_text() == "a"
    assert (tmp_path / "dst" / "sub" / "b.txt").read_text() == "b"


@pytest.mark.asyncio
async def test_cancelled_recursive_copy_makes_no_further_writes(tmp_path, monkeypatch):
    """P1C: cancelling an in-progress recursive copy must settle the worker
    thread deterministically: cancellation is acknowledged as stopped, no new
    files appear after confirmed settlement, staging artifacts are removed,
    and ordinary completed file operations keep working afterwards."""
    import threading
    import time as _time
    from tools import developer_toolkit as toolkit
    monkeypatch.setattr(toolkit, "_COPY_CHUNK_BYTES", 512)

    # Deterministic barrier: pause the real copy inside copystat — after the
    # first file is atomically placed and before the next one begins. The gate
    # is released only after cancellation has been requested, so the worker's
    # very next action is its cooperative cancel check.
    gate = threading.Event()
    entered = threading.Event()
    real_copystat = toolkit.shutil.copystat

    def blocking_copystat(source_file, dest_file, **kwargs):
        entered.set()
        gate.wait(timeout=30)
        return real_copystat(source_file, dest_file, **kwargs)

    monkeypatch.setattr(toolkit.shutil, "copystat", blocking_copystat)

    src = tmp_path / "big"
    src.mkdir()
    for index in range(8):
        (src / f"f{index}.bin").write_bytes(b"x" * 200000)
    registry = _registry(tmp_path)
    task = asyncio.create_task(registry.execute("file_manage", {
        "operation": "copy", "source": "big", "destination": "out", "recursive": True
    }, "cancel-me"))
    # Wait until the copy is genuinely in progress and paused at the barrier.
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), timeout=15)
    assert (tmp_path / "out" / "f0.bin").exists()
    task.cancel()
    # Give the loop a moment to deliver cancellation so the worker's cancel
    # Event is set before the gate opens; the worker then aborts at its
    # next check and the settle window closes promptly.
    await asyncio.sleep(0.05)
    gate.set()
    started = _time.monotonic()
    with pytest.raises(asyncio.CancelledError) as excinfo:
        await task
    elapsed = _time.monotonic() - started
    details = getattr(excinfo.value, "hashi_tool_details", None) or {}
    mutation = details.get("toolkit_mutation") or {}
    assert mutation.get("status") == "stopped", details
    assert elapsed < 5.0, f"settle took {elapsed:.2f}s; /stop responsiveness broken"
    # Nothing NEW may be written after confirmed settlement; staging gone.
    settled = sorted(p.name for p in (tmp_path / "out").iterdir())
    await asyncio.sleep(0.2)
    after = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert settled == after, (settled, after)
    assert not any(name.startswith(".hashi-copy-") for name in after), after
    # Ordinary completed file operations must keep working after cancellation.
    (tmp_path / "small.txt").write_text("still works", encoding="utf-8")
    ok = await _run(registry, "file_manage", {
        "operation": "copy", "source": "small.txt", "destination": "done.txt"
    })
    assert ok["ok"] and (tmp_path / "done.txt").read_text() == "still works"


@pytest.mark.asyncio
async def test_tool_find_accumulates_discoveries_within_turn(tmp_path):
    """P2: later tool_find calls keep previously revealed, still-authorised
    schemas (bounded, request-local)."""
    # office_* tools are filtered by tool_availability when no Office runtime
    # is configured, so accumulation is exercised with always-available tools.
    registry = _registry(tmp_path, allowed=[
        "tool_find", "file_search", "workspace_inspect", "file_read"
    ])
    view = OnDemandToolView(registry)
    first = await view.execute("tool_find", {"names": ["workspace_inspect"]}, "one")
    assert not first.is_error
    second = await view.execute("tool_find", {"names": ["file_search"]}, "two")
    assert not second.is_error
    visible = {item["function"]["name"] for item in view.get_tool_definitions(tiers=["core"])}
    assert {"workspace_inspect", "file_search"} <= visible, visible


@pytest.mark.asyncio
async def test_text_replace_detects_concurrent_change(tmp_path, monkeypatch):
    """P2: an external edit between read and replace must abort instead of
    silently losing the concurrent change."""
    import tools.developer_toolkit as toolkit
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    registry = _registry(tmp_path)
    real_copymode = toolkit.shutil.copymode

    def racing_copymode(source_target, staged_file, **kwargs):
        # Simulate an external writer landing while the edit is staged,
        # before the read-back check runs.
        source_target.write_text("external edit\n", encoding="utf-8")
        return real_copymode(source_target, staged_file, **kwargs)

    monkeypatch.setattr(toolkit.shutil, "copymode", racing_copymode)
    result = await registry.execute("text_replace", {
        "path": "note.txt", "find": "alpha", "replace": "gamma", "expected_count": 1
    }, "race")
    payload = json.loads(result.output)
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "concurrent_change", payload
    assert target.read_text(encoding="utf-8") == "external edit\n"
# HASHI-Fingerprint: hfp_c7cbfa1a3f474171ae633577f172a78d
