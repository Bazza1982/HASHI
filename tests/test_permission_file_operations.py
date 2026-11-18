"""Fixed helper behavior with exact patches and native held directory objects.

Native checks use pytest-owned temporary files in the current account; they do
not qualify an installed L2 host or start another service/process.
"""
import base64
import json
import os

import pytest

from orchestrator.permissions.file_operations import strict_patch


def test_exact_patch_is_precomputed_and_conflicts_never_modify_the_source():
    original = "one\ntwo\n"
    patch = "--- old\n+++ new\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n"
    assert strict_patch(original, patch) == b"one\nthree\n"
    assert strict_patch("one\r\ntwo\r\n", patch) == b"one\r\nthree\r\n"
    with pytest.raises(ValueError, match="does not match"):
        strict_patch("different\ntwo\n", patch)
    with pytest.raises(ValueError):
        strict_patch(original, patch + "--- another\n+++ file\n")
    assert strict_patch("", "--- /dev/null\n+++ new\n@@ -0,0 +1 @@\n+created\n") == b"created\n"


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_native_listing_search_and_continuation_skip_protected_descendants(tmp_path):
    from orchestrator.permissions.file_operations import list_held, search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    (tmp_path / "report.txt").write_text("needle one\nneedle two\nneedle three\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("needle secret", encoding="utf-8")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        listing = json.loads(list_held(bound, {"path": str(tmp_path)}, [str(private)]))
    assert [entry["name"] for entry in listing["entries"]] == ["report.txt"]
    assert listing["coverage_complete"] is True
    arguments = {"query": "needle", "mode": "content", "max_results": 2}
    with open_bound_directory(scope) as bound:
        first = json.loads(search_held([bound], [str(tmp_path)], arguments, [str(private)],
                                       cursor_key=b"k" * 32, scope_binding="run-one"))
    assert [row["line"] for row in first["matches"]] == [1, 2]
    assert first["coverage_complete"] is False and first["cursor"]
    with open_bound_directory(scope) as bound:
        second = json.loads(search_held([bound], [str(tmp_path)],
                                       {**arguments, "cursor": first["cursor"]}, [str(private)],
                                       cursor_key=b"k" * 32, scope_binding="run-one"))
    assert [row["line"] for row in second["matches"]] == [3]
    assert second["coverage_complete"] is True
    with open_bound_directory(scope) as bound:
        changed = json.loads(search_held([bound], [str(tmp_path)],
            {**arguments, "query": "secret", "cursor": first["cursor"]}, [],
            cursor_key=b"k" * 32, scope_binding="run-one"))
        assert changed["restart_required"] and not changed["matches"]


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_native_managed_listing_pages_every_allowed_entry_and_detects_changed_scope(tmp_path):
    from orchestrator.permissions.file_operations import list_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    for index in range(501):
        (tmp_path / f"entry-{index:04d}.txt").touch()
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").touch()
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    key = b"k" * 32
    arguments = {"path": str(tmp_path), "max_results": 137, "pattern": "*.txt"}
    def read_page(cursor=None, scope_binding="run-one", page_size=137):
        args = {**arguments, "max_results": page_size, **({"cursor": cursor} if cursor else {})}
        with open_bound_directory(scope) as bound:
            return json.loads(list_held(bound, args, [str(private)], cursor_key=key, scope_binding=scope_binding))

    first = read_page()
    assert not first["coverage_complete"] and first["next_cursor"]
    wrong_run = read_page(first["next_cursor"], scope_binding="run-two")
    assert wrong_run["restart_required"] and not wrong_run["entries"]
    cursor, names = first["next_cursor"], [entry["name"] for entry in first["entries"]]
    while cursor:
        page = read_page(cursor, page_size=99)
        names.extend(entry["name"] for entry in page["entries"])
        cursor = page["next_cursor"]
    assert names == [f"entry-{index:04d}.txt" for index in range(501)]
    assert len(names) == len(set(names))

    first = read_page()
    (tmp_path / "later.txt").touch()
    changed = read_page(first["next_cursor"])
    assert changed["restart_required"] and not changed["entries"]


def test_managed_listing_does_not_offer_reparse_targets_or_protected_children():
    from orchestrator.permissions.file_operations import list_held
    class Bound:
        def identity(self):
            return {"volume": 1, "file_id": 1, "write_time": 1}
        def entries(self):
            yield {"name": "public.txt", "directory": False, "reparse": False, "hidden": False, "size": 1}
            yield {"name": "link", "directory": True, "reparse": True, "hidden": False, "size": 0}
            yield {"name": "private", "directory": True, "reparse": False, "hidden": False, "size": 0}
    result = json.loads(list_held(Bound(), {"path": "C:\\root"}, ["C:\\root\\private"],
                                  cursor_key=b"k" * 32, scope_binding="run-one"))
    assert [entry["name"] for entry in result["entries"]] == ["public.txt"]
    assert result["coverage_complete"]

@pytest.mark.skipif(os.name != "nt", reason="native inherited held directory")
def test_fixed_helper_returns_run_bound_managed_listing_continuation(tmp_path):
    from dataclasses import replace
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.file_process_supervisor import prepare_managed_listing
    from orchestrator.permissions.file_worker import run
    from orchestrator.permissions.models import ExecutionContext, normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    for index in range(501):
        (tmp_path / f"item-{index:04d}.txt").touch()
    secret = tmp_path / "private.txt"
    secret.touch()
    scope = normalize_scope("file", str(tmp_path), ["list"], cwd=tmp_path)
    context = ExecutionContext(instance_id="HASHI", owner_id="owner", agent_id="agent", task_id="task",
                               run_id="run-a", session_id="session", turn_id="turn", worker_generation="worker",
                               context_generation="1", execution_epoch="epoch", backend_id="backend",
                               executor_id="executor", policy_revision=1)
    def helper(cursor=None, run_scope="run-a", pattern="*.txt"):
        # The actual execution policy resolves a relative argument to the held absolute root.
        args = {"path": ".", "max_results": 200, "pattern": pattern}
        if cursor:
            args["cursor"] = cursor
        with open_bound_directory(scope) as bound:
            _, transfer = inheritable_handle_copy(bound._handles[-1])
            descriptor = describe_tool("file_list", args, cwd=tmp_path, read_roots=(tmp_path,))
            data = prepare_managed_listing(replace(context, run_id=run_scope), descriptor, args,
                                           protected_paths=[str(secret)], cursor_key=b"k" * 32)
            plan = {"format": "hashi-file-helper-v1", "action": "list", "handles": [transfer],
                    "directory": "", "name": "", "data": base64.b64encode(json.dumps(data).encode()).decode(),
                    "result": ""}
            result = run(plan)
        assert result["outcome"] == "succeeded"
        output = base64.b64decode(result["data"])
        assert (b"k" * 32).hex().encode() not in output
        return json.loads(output)

    first = helper()
    cursor = first["next_cursor"]
    assert cursor and not first["coverage_complete"]
    assert helper(cursor, run_scope="run-b")["restart_required"]
    assert helper(cursor, pattern="item-000*.txt")["restart_required"]
    assert helper(cursor[:-1] + ("0" if cursor[-1] != "0" else "1"))["restart_required"]
    names = [entry["name"] for entry in first["entries"]]
    while cursor:
        page = helper(cursor)
        assert not page["restart_required"]
        names.extend(entry["name"] for entry in page["entries"])
        cursor = page["next_cursor"]
    assert names == [f"item-{index:04d}.txt" for index in range(501)]
    assert "private.txt" not in names


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_cursor_restarts_if_directory_is_replaced_at_the_same_path(tmp_path):
    from orchestrator.permissions.file_operations import list_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    directory = tmp_path / "work"
    directory.mkdir()
    for index in range(3):
        (directory / f"item-{index}.txt").touch()
    scope = normalize_scope("file", str(directory), ["list"], cwd=tmp_path)
    args = {"path": str(directory), "max_results": 1}
    def read_page(cursor=None):
        with open_bound_directory(scope) as bound:
            return json.loads(list_held(bound, {**args, **({"cursor": cursor} if cursor else {})}, [],
                                        cursor_key=b"k" * 32, scope_binding="run-one"))
    first = read_page()
    assert first["next_cursor"]
    directory.rename(tmp_path / "old-work")
    directory.mkdir()
    for index in range(3):
        (directory / f"item-{index}.txt").touch()
    replaced = read_page(first["next_cursor"])
    assert replaced["restart_required"] and not replaced["entries"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows file attributes")
def test_managed_listing_hidden_filter_uses_native_attributes(tmp_path):
    import ctypes
    from ctypes import wintypes
    from orchestrator.permissions.file_operations import list_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    hidden = tmp_path / "hidden.txt"
    hidden.touch()
    (tmp_path / "public.txt").touch()
    attributes = os.stat(hidden).st_file_attributes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.SetFileAttributesW.restype = wintypes.BOOL
    assert kernel.SetFileAttributesW(str(hidden), attributes | 2)
    try:
        scope = normalize_scope("file", str(tmp_path), ["list"], cwd=tmp_path)
        def names(include_hidden):
            with open_bound_directory(scope) as bound:
                result = json.loads(list_held(bound, {"path": str(tmp_path), "include_hidden": include_hidden}, []))
            return [item["name"] for item in result["entries"]]
        assert names(True) == ["hidden.txt", "public.txt"]
        assert names(False) == ["public.txt"]
    finally:
        assert kernel.SetFileAttributesW(str(hidden), attributes)


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_listing_excludes_a_native_directory_link(tmp_path):
    from orchestrator.permissions.file_operations import list_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    directory, outside = tmp_path / "listed", tmp_path / "outside"
    directory.mkdir()
    outside.mkdir()
    (outside / "secret.txt").touch()
    try:
        os.symlink(outside, directory / "outside-link", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating native symbolic links unavailable: {exc}")
    (directory / "public.txt").touch()
    scope = normalize_scope("file", str(directory), ["list"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(list_held(bound, {"path": str(directory)}, []))
    assert [entry["name"] for entry in result["entries"]] == ["public.txt"]


@pytest.mark.skipif(os.name != "nt", reason="native inherited held file")
def test_fixed_helper_patches_the_held_file_once(tmp_path):
    from orchestrator.permissions.file_worker import run
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_write
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    target = tmp_path / "report.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    scope = normalize_scope("file", str(target), ["create", "read", "write"], cwd=tmp_path)
    patch = "--- old\n+++ new\n@@ -1,2 +1,2 @@\n one\n-two\n+saved\n"
    with open_bound_write(scope) as bound:
        _, transfer = inheritable_handle_copy(bound._handles[-1])
        plan = {"format": "hashi-file-helper-v1", "action": "patch_write", "handles": [transfer],
                "directory": "", "name": "", "data": base64.b64encode(patch.encode()).decode(), "result": ""}
        assert run(plan)["outcome"] == "succeeded"
    assert target.read_text(encoding="utf-8") == "one\nsaved\n"


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_content_search_pages_large_files_without_losing_matches(tmp_path):
    import inspect
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    (tmp_path / "first.txt").write_text("needle-first\n", encoding="utf-8")
    large = tmp_path / "large.txt"
    with large.open("wb") as stream:
        stream.write(b"a" * (65536 - 3))
        stream.write(b"needle-split\n")
        for _ in range(17):
            stream.write(b"b" * (1024 * 1024))
        stream.write(b"needle-tail\n")
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("needle-secret", encoding="utf-8")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    arguments = {"query": "needle", "mode": "content", "max_results": 100}
    kwargs = ({"cursor_key": b"k" * 32, "scope_binding": "run-one"}
              if "cursor_key" in inspect.signature(search_held).parameters else {})

    def page(cursor=None, **kwargs_override):
        with open_bound_directory(scope) as bound:
            return json.loads(search_held([bound], [str(tmp_path)],
                {**arguments, **({"cursor": cursor} if cursor else {})},
                [str(private)], **{**kwargs, **kwargs_override}))

    found, cursor, pages = [], None, 0
    while True:
        result = page(cursor)
        found.extend(row["text"] for row in result["matches"])
        pages += 1
        cursor = result.get("cursor")
        if not cursor:
            break
        assert pages < 10, "continuation must make progress"
    assert pages >= 2, "cumulative read budget must continue rather than skip"
    assert result["coverage_complete"], result
    assert any("needle-first" in row for row in found)
    assert any("needle-split" in row for row in found)
    assert any("needle-tail" in row for row in found)
    assert not any("needle-secret" in row for row in found)


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_content_search_restarts_when_source_or_cursor_changes(tmp_path):
    import inspect
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    large = tmp_path / "large.txt"
    with large.open("wb") as stream:
        for _ in range(17):
            stream.write(b"x" * (1024 * 1024))
        stream.write(b"needle-tail\n")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    arguments = {"query": "needle", "mode": "content"}
    kwargs = ({"cursor_key": b"k" * 32, "scope_binding": "run-one"}
              if "cursor_key" in inspect.signature(search_held).parameters else {})

    def page(cursor=None, arguments_override=None, **kwargs_override):
        with open_bound_directory(scope) as bound:
            return json.loads(search_held([bound], [str(tmp_path)],
                {**arguments, **(arguments_override or {}), **({"cursor": cursor} if cursor else {})},
                [], **{**kwargs, **kwargs_override}))

    first = page()
    assert first["cursor"], first
    cursor = first["cursor"]
    if kwargs:
        assert page(cursor, scope_binding="other-run")["restart_required"]
        assert page(cursor, arguments_override={"query": "different"})["restart_required"]
        altered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        assert page(altered)["restart_required"]
    with large.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"changed")
    os.utime(large, None)
    changed = page(cursor)
    assert changed["restart_required"]
    assert not changed["matches"]


@pytest.mark.skipif(os.name != "nt", reason="native inherited held directory")
def test_fixed_helper_managed_search_continuation_is_run_and_root_bound(tmp_path):
    from dataclasses import replace
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.file_process_supervisor import prepare_managed_search
    from orchestrator.permissions.file_worker import run
    from orchestrator.permissions.models import ExecutionContext, normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    (tmp_path / "report.txt").write_text("needle one\nneedle two\nneedle three\n", encoding="utf-8")
    private = tmp_path / "private.txt"
    private.write_text("needle private\n", encoding="utf-8")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    context = ExecutionContext(instance_id="HASHI", owner_id="owner", agent_id="agent", task_id="task",
                               run_id="run-a", session_id="session", turn_id="turn", worker_generation="worker",
                               context_generation="1", execution_epoch="epoch", backend_id="backend",
                               executor_id="executor", policy_revision=1)

    def helper(cursor=None, run_scope="run-a", query="needle"):
        args = {"query": query, "mode": "content", "roots": [str(tmp_path)], "max_results": 1}
        if cursor:
            args["cursor"] = cursor
        with open_bound_directory(scope) as bound:
            _, transfer = inheritable_handle_copy(bound._handles[-1])
            descriptor = describe_tool("file_search", args, cwd=tmp_path, read_roots=(tmp_path,))
            data = prepare_managed_search(replace(context, run_id=run_scope), descriptor, args,
                                          protected_paths=[str(private)], cursor_key=b"k" * 32)
            plan = {"format": "hashi-file-helper-v1", "action": "search", "handles": [transfer],
                    "directory": "", "name": "", "data": base64.b64encode(json.dumps(data).encode()).decode(),
                    "result": ""}
            result = run(plan)
        assert result["outcome"] == "succeeded"
        output = base64.b64decode(result["data"])
        assert (b"k" * 32).hex().encode() not in output
        return json.loads(output)

    first = helper()
    assert [row["line"] for row in first["matches"]] == [1]
    cursor = first["cursor"]
    assert cursor and not first["coverage_complete"]
    assert helper(cursor, run_scope="run-b")["restart_required"]
    assert helper(cursor, query="other")["restart_required"]
    altered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
    assert helper(altered)["restart_required"]
    rows = list(first["matches"])
    for _ in range(8):
        if not cursor:
            break
        result = helper(cursor)
        assert not result["restart_required"]
        rows.extend(result["matches"])
        cursor = result["cursor"]
    assert not cursor
    assert result["coverage_complete"]
    assert [row["line"] for row in rows] == [1, 2, 3]
    assert not any("private" in row["text"] for row in rows)


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_finds_utf8_and_literal_across_read_and_page_boundaries(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    large = tmp_path / "report.txt"
    with large.open("wb") as stream:
        stream.write(b"a" * (65536 - 5))
        stream.write("Straße\n".encode("utf-8"))
        bytes_written = stream.tell()
        remaining = 16 * 1024 * 1024 - bytes_written - 3
        while remaining:
            chunk = min(1024 * 1024, remaining)
            stream.write(b"b" * chunk)
            remaining -= chunk
        stream.write(b"needle-after-page\n")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)

    def page(query, cursor=None):
        with open_bound_directory(scope) as bound:
            return json.loads(search_held([bound], [str(tmp_path)],
                {"query": query, "mode": "content", **({"cursor": cursor} if cursor else {})},
                [], cursor_key=b"k" * 32, scope_binding="run-a"))

    unicode_page = page("STRASSE")
    assert any("Straße" in row["text"] for row in unicode_page["matches"])
    first = page("needle-after-page")
    assert first["cursor"] and not first["matches"]
    second = page("needle-after-page", first["cursor"])
    assert any("needle-after-page" in row["text"] for row in second["matches"])
    assert second["coverage_complete"]


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_pages_match_utf8_line_oracle(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    content = ("first NEEDLE\r\n" + "z" * 65532 + "Straße needle\r"
               + "independent needle\n" + "x" * 70000 + "needle at end\u2028"
               + "last Needle")
    (tmp_path / "report.txt").write_bytes(content.encode("utf-8"))
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)

    for query in ("needle", "STRASSE"):
        expected = [number for number, line in enumerate(content.splitlines(), 1)
                    if query.casefold() in line.casefold()]
        cursor, actual = None, []
        for _ in range(20):
            with open_bound_directory(scope) as bound:
                payload = json.loads(search_held([bound], [str(tmp_path)],
                    {"query": query, "mode": "content", "max_results": 1,
                     **({"cursor": cursor} if cursor else {})},
                    [], cursor_key=b"k" * 32, scope_binding="run-a"))
            assert not payload["restart_required"]
            actual.extend(row["line"] for row in payload["matches"])
            cursor = payload["cursor"]
            if not cursor:
                break
        assert payload["coverage_complete"]
        assert actual == expected


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_reports_unreadable_binary_and_invalid_utf8(tmp_path):
    from pathlib import Path
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    (tmp_path / "good.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"content\x00needle")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    source = tmp_path / "linked.txt"
    source.write_text("needle\n", encoding="utf-8")
    os.link(source, tmp_path / "linked-again.txt")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(search_held([bound], [str(tmp_path)],
            {"query": "needle", "mode": "content"}, [],
            cursor_key=b"k" * 32, scope_binding="run-a"))
    assert not result["coverage_complete"]
    assert not result["can_continue"] and not result["cursor"]
    reasons = {Path(row["path"]).name: row["reason"] for row in result["skipped_details"]}
    assert reasons["binary.bin"] == "binary_content"
    assert reasons["invalid.txt"] == "invalid_utf8"
    assert reasons["linked.txt"] == "file_unavailable"
    assert reasons["linked-again.txt"] == "file_unavailable"
    assert any("needle" in row["text"] for row in result["matches"])


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_path_search_continues_past_entry_budget(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    for index in range(5001):
        (tmp_path / f"entry-{index:04d}.txt").touch()
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    absent = {"query": "not-present", "mode": "path", "max_results": 200}
    with open_bound_directory(scope) as bound:
        first = json.loads(search_held([bound], [str(tmp_path)], absent, [],
            cursor_key=b"k" * 32, scope_binding="run-a"))
    assert first["stop_reason"] == "entry_budget" and first["cursor"] and not first["matches"]
    with open_bound_directory(scope) as bound:
        second = json.loads(search_held([bound], [str(tmp_path)], {**absent, "cursor": first["cursor"]}, [],
            cursor_key=b"k" * 32, scope_binding="run-a"))
    assert second["coverage_complete"] and not second["matches"]

    args = {"query": ".txt", "mode": "path", "max_results": 200}
    cursor, names = None, []
    for _ in range(40):
        with open_bound_directory(scope) as bound:
            result = json.loads(search_held([bound], [str(tmp_path)],
                {**args, **({"cursor": cursor} if cursor else {})}, [],
                cursor_key=b"k" * 32, scope_binding="run-a"))
        assert not result["restart_required"]
        names.extend(row["path"] for row in result["matches"])
        cursor = result["cursor"]
        if not cursor:
            break
    assert not cursor and result["coverage_complete"]
    assert len(names) == len(set(names)) == 5001


def test_managed_search_does_not_hold_all_sibling_directories(monkeypatch):
    from orchestrator.permissions import file_operations

    class Api:
        active = 0
        peak = 0

        def open_child(self, parent, name, *, directory, **_kwargs):
            assert directory and parent == "root"
            self.active += 1
            self.peak = max(self.peak, self.active)
            return name

        def check(self, handle, target, *, directory):
            assert directory and target.endswith(handle)

    class Directory:
        def __init__(self, api, handles):
            self._api, self._handles = api, handles

        def identity(self):
            name = self._handles[-1]
            return {"volume": 1, "file_id": 1 if name == "root" else int(name[1:]) + 2,
                    "created": 1, "write_time": 1}

        def entries(self, *, include_metadata=False):
            assert include_metadata
            name = self._handles[-1]
            names = ((f"d{index}", True, index + 2) for index in range(3000)) if name == "root" else [
                ("file.txt", False, int(name[1:]) + 10000)]
            for entry_name, directory, file_id in names:
                yield {"name": entry_name, "directory": directory, "reparse": False,
                       "hidden": False, "size": 0, "file_id": file_id,
                       "write_time": 1, "change_time": 1, "created": 1}

        def close(self):
            assert self._handles[-1] != "root"
            self._handles.clear()
            self._api.active -= 1

    monkeypatch.setattr(file_operations, "BoundWindowsDirectory", Directory)
    api = Api()
    root = Directory(api, ["root"])
    args = {"query": "not-present", "mode": "path"}
    cursor = None
    for _ in range(4):
        result = json.loads(file_operations.search_held([root], ["C:\\root"],
            {**args, **({"cursor": cursor} if cursor else {})}, [],
            cursor_key=b"k" * 32, scope_binding="run-a"))
        assert not result["restart_required"] and api.active == 0
        cursor = result["cursor"]
        if not cursor:
            break
    assert result["coverage_complete"] and not cursor
    assert api.peak == 1



def _make_directory_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating native symbolic links unavailable: {exc}")


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_follows_only_authorized_links_and_reports_skips(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    source, outside = tmp_path / "source", tmp_path / "outside"
    inside, private = source / "inside", source / "private"
    inside.mkdir(parents=True)
    private.mkdir()
    outside.mkdir()
    (inside / "data.txt").write_text("needle-public\n", encoding="utf-8")
    (private / "secret.txt").write_text("needle-private\n", encoding="utf-8")
    (outside / "secret.txt").write_text("needle-outside\n", encoding="utf-8")
    _make_directory_link(source / "inside-link", inside)
    _make_directory_link(source / "relative-link", "inside")
    _make_directory_link(source / "blocked-link", outside)
    _make_directory_link(source / "private-link", private)
    _make_directory_link(source / "loop-link", source)
    try:
        os.symlink(inside / "data.txt", source / "file-link.txt")
    except OSError as exc:
        pytest.skip(f"creating native symbolic links unavailable: {exc}")
    scope = normalize_scope("file", str(source), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(search_held([bound], [str(source)],
            {"query": "needle", "mode": "content", "options": {"follow_links": True}},
            [str(private)], cursor_key=b"k" * 32, scope_binding="run-a"))
    paths = {row["path"].casefold() for row in result["matches"]}
    assert str(source / "inside" / "data.txt").casefold() in paths
    assert str(source / "inside-link" / "data.txt").casefold() in paths
    assert str(source / "relative-link" / "data.txt").casefold() in paths
    assert str(source / "file-link.txt").casefold() in paths
    assert not any("secret" in row["text"] for row in result["matches"])
    reasons = {os.path.basename(row["path"]): row["reason"] for row in result["skipped_details"]}
    assert reasons["blocked-link"] == "link_target_outside_scope"
    assert reasons["private-link"] == "link_target_protected"
    assert reasons["loop-link"] == "link_cycle"
    assert not result["coverage_complete"] and not result["cursor"]


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_link_to_another_approved_root_and_changed_cursor(tmp_path):
    from contextlib import ExitStack
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    source, target, replacement = (tmp_path / name for name in ("source", "target", "replacement"))
    for directory in (source, target, replacement):
        directory.mkdir()
    (target / "data.txt").write_text("needle-a\nneedle-b\n", encoding="utf-8")
    (replacement / "data.txt").write_text("needle-replacement\n", encoding="utf-8")
    link = source / "alias"
    _make_directory_link(link, target)
    roots = [source, target, replacement]
    scopes = [normalize_scope("file", str(root), ["list", "read"], cwd=tmp_path) for root in roots]
    args = {"query": "needle", "mode": "content", "max_results": 1,
            "options": {"follow_links": True}}

    def page(cursor=None):
        with ExitStack() as stack:
            bounds = [stack.enter_context(open_bound_directory(scope)) for scope in scopes]
            return json.loads(search_held(bounds, [str(root) for root in roots],
                {**args, **({"cursor": cursor} if cursor else {})}, [],
                cursor_key=b"k" * 32, scope_binding="run-a"))

    first = page()
    assert first["cursor"] and first["matches"][0]["path"].casefold() == str(link / "data.txt").casefold()
    unchanged = page(first["cursor"])
    assert not unchanged["restart_required"] and unchanged["matches"][0]["line"] == 2
    assert unchanged["matches"][0]["path"].casefold() == str(link / "data.txt").casefold()
    os.rmdir(link)
    _make_directory_link(link, replacement)
    second = page(first["cursor"])
    assert second["restart_required"] and not second["matches"]


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_link_flags_must_be_boolean_and_default_is_not_follow(tmp_path):
    from orchestrator.permissions.file_operations import search_held, search_options
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("needle", encoding="utf-8")
    _make_directory_link(tmp_path / "alias", target)
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(search_held([bound], [str(tmp_path)],
            {"query": "needle", "mode": "content"}, [], cursor_key=b"k" * 32, scope_binding="run-a"))
    assert [row["path"].casefold() for row in result["matches"]] == [str(target / "file.txt").casefold()]
    for bad in (None, 1, "yes"):
        with pytest.raises(ValueError):
            search_options({"query": "needle", "options": {"follow_links": bad}})



@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_does_not_follow_an_unapproved_link_chain(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    source, outside = tmp_path / "source", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("needle-secret\n", encoding="utf-8")
    _make_directory_link(source / "redirect", outside)
    _make_directory_link(source / "chain", source / "redirect")
    scope = normalize_scope("file", str(source), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(search_held([bound], [str(source)],
            {"query": "needle", "mode": "content", "options": {"follow_links": True}},
            [], cursor_key=b"k" * 32, scope_binding="run-a"))
    reasons = {os.path.basename(row["path"]): row["reason"] for row in result["skipped_details"]}
    assert not result["matches"] and not result["coverage_complete"]
    assert reasons["redirect"] == "link_target_outside_scope"
    assert reasons["chain"] == "link_target_outside_scope"


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_follows_an_authorized_link_chain_and_detects_link_cycles(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    target = tmp_path / "target"
    target.mkdir()
    (target / "data.txt").write_text("needle-chain\\n", encoding="utf-8")
    _make_directory_link(tmp_path / "second", target)
    _make_directory_link(tmp_path / "first", tmp_path / "second")
    _make_directory_link(tmp_path / "cycle-a", tmp_path / "cycle-b")
    _make_directory_link(tmp_path / "cycle-b", tmp_path / "cycle-a")
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    with open_bound_directory(scope) as bound:
        result = json.loads(search_held([bound], [str(tmp_path)],
            {"query": "needle", "mode": "content", "options": {"follow_links": True}},
            [], cursor_key=b"k" * 32, scope_binding="run-a"))
    assert str(tmp_path / "first" / "data.txt").casefold() in {
        row["path"].casefold() for row in result["matches"]}
    reasons = {os.path.basename(row["path"]): row["reason"] for row in result["skipped_details"]}
    assert reasons["cycle-a"] == reasons["cycle-b"] == "link_cycle"
    assert not result["coverage_complete"]


@pytest.mark.skipif(os.name != "nt", reason="native held directory handles")
def test_managed_search_restarts_if_middle_link_changes_during_paging(tmp_path):
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    first, replacement = tmp_path / "first-target", tmp_path / "replacement"
    first.mkdir()
    replacement.mkdir()
    (first / "data.txt").write_text("needle-first\nneedle-second\n", encoding="utf-8")
    (replacement / "data.txt").write_text("needle-replacement\n", encoding="utf-8")
    middle, alias = tmp_path / "middle", tmp_path / "alias"
    _make_directory_link(middle, first)
    _make_directory_link(alias, middle)
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    args = {"query": "needle", "mode": "content", "max_results": 1,
            "options": {"follow_links": True}}

    def page(cursor=None):
        with open_bound_directory(scope) as bound:
            return json.loads(search_held([bound], [str(tmp_path)],
                {**args, **({"cursor": cursor} if cursor else {})}, [],
                cursor_key=b"k" * 32, scope_binding="run-a"))

    result = page()
    assert result["cursor"] and result["matches"][0]["path"].casefold() == str(alias / "data.txt").casefold()
    os.rmdir(middle)
    _make_directory_link(middle, replacement)
    continued = page(result["cursor"])
    assert continued["restart_required"] and not continued["matches"]


@pytest.mark.skipif(os.name != "nt", reason="native inherited held directory")
def test_fixed_helper_search_follows_an_authorized_link(tmp_path):
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.file_process_supervisor import prepare_managed_search
    from orchestrator.permissions.file_worker import run
    from orchestrator.permissions.models import ExecutionContext, normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("needle\n", encoding="utf-8")
    _make_directory_link(tmp_path / "alias", target)
    scope = normalize_scope("file", str(tmp_path), ["list", "read"], cwd=tmp_path)
    args = {"query": "needle", "mode": "content", "roots": [str(tmp_path)],
            "options": {"follow_links": True}}
    context = ExecutionContext(instance_id="HASHI", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run-a", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="1", execution_epoch="epoch", backend_id="backend",
        executor_id="executor", policy_revision=1)
    with open_bound_directory(scope) as bound:
        _, transfer = inheritable_handle_copy(bound._handles[-1])
        descriptor = describe_tool("file_search", args, cwd=tmp_path, read_roots=(tmp_path,))
        data = prepare_managed_search(context, descriptor, args, protected_paths=[], cursor_key=b"k" * 32)
        plan = {"format": "hashi-file-helper-v1", "action": "search", "handles": [transfer],
                "directory": "", "name": "", "data": base64.b64encode(json.dumps(data).encode()).decode(),
                "result": ""}
        result = run(plan)
    assert result["outcome"] == "succeeded"
    output = json.loads(base64.b64decode(result["data"]))
    assert output["coverage_complete"]
    assert str(tmp_path / "alias" / "file.txt").casefold() in {
        row["path"].casefold() for row in output["matches"]}


@pytest.mark.skipif(os.name != "nt", reason="native cross-volume test")
def test_managed_search_cross_volume_link_requires_explicit_option(tmp_path):
    import tempfile
    from contextlib import ExitStack
    from orchestrator.permissions.file_operations import search_held
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_directory

    other_volume = os.environ.get("HASHI_TEST_CROSS_VOLUME_ROOT")
    if not other_volume:
        pytest.skip("a disposable alternate-volume test root was not configured")
    source = tmp_path / "source"
    source.mkdir()
    with tempfile.TemporaryDirectory(prefix="hashi-crossvolume-", dir=other_volume) as temp:
        target = __import__("pathlib").Path(temp)
        (target / "file.txt").write_text("needle-cross-volume\n", encoding="utf-8")
        _make_directory_link(source / "alias", target)
        roots = [source, target]
        scopes = [normalize_scope("file", str(root), ["list", "read"], cwd=tmp_path) for root in roots]

        def search(cross):
            with ExitStack() as stack:
                bounds = [stack.enter_context(open_bound_directory(scope)) for scope in scopes]
                if bounds[0].identity()["volume"] == bounds[1].identity()["volume"]:
                    pytest.skip("alternate test root is on the same filesystem")
                return json.loads(search_held(bounds, [str(root) for root in roots],
                    {"query": "needle", "mode": "content",
                     "options": {"follow_links": True, "cross_filesystems": cross}}, [],
                    cursor_key=b"k" * 32, scope_binding="run-a"))

        denied = search(False)
        reasons = {os.path.basename(row["path"]): row["reason"] for row in denied["skipped_details"]}
        assert reasons["alias"] == "link_crosses_filesystem" and not denied["coverage_complete"]
        assert str(source / "alias" / "file.txt").casefold() not in {
            row["path"].casefold() for row in denied["matches"]}
        allowed = search(True)
        assert allowed["coverage_complete"]
        assert str(source / "alias" / "file.txt").casefold() in {
            row["path"].casefold() for row in allowed["matches"]}



def test_exact_patch_preserves_mixed_line_endings_and_unterminated_last_line():
    original = "one\r\ntwo\nthree\r\nfour\nlast"
    patch = ("--- old\n+++ new\n@@ -1,5 +1,5 @@\n"
             " one\n-two\n+TWO\n three\n four\n last\n"
             "\\ No newline at end of file\n")
    assert strict_patch(original, patch) == b"one\r\nTWO\nthree\r\nfour\nlast"
    with pytest.raises(ValueError, match="does not match"):
        strict_patch(original.replace("three", "changed"), patch)


def test_exact_patch_uses_the_local_line_ending_for_insertions_and_replacements():
    original = "head\r\nbody\nend\r\n"
    patch = ("--- old\n+++ new\n@@ -1,3 +1,4 @@\n"
             " head\n body\n+extra\n-end\n+END\n")
    assert strict_patch(original, patch) == b"head\r\nbody\nextra\nEND\r\n"


def test_exact_patch_preserves_legacy_carriage_return_lines():
    original = "first\rsecond\nthird\r\n"
    patch = ("--- old\n+++ new\n@@ -1,3 +1,3 @@\n"
             " first\n-second\n+SECOND\n third\n")
    assert strict_patch(original, patch) == b"first\rSECOND\nthird\r\n"



def test_exact_patch_handles_newline_transitions_without_changing_other_lines():
    add_newline = ("--- old\n+++ new\n@@ -1,2 +1,2 @@\n"
                   " one\n-two\n\\ No newline at end of file\n+TWO\n")
    remove_newline = ("--- old\n+++ new\n@@ -1,2 +1,2 @@\n"
                      " one\n-two\n+TWO\n\\ No newline at end of file\n")
    assert strict_patch("one\r\ntwo", add_newline) == b"one\r\nTWO\r\n"
    assert strict_patch("one\r\ntwo\n", remove_newline) == b"one\r\nTWO"


def test_exact_patch_preserves_each_replacement_line_and_multiple_hunks():
    replacement = ("--- old\n+++ new\n@@ -1,3 +1,3 @@\n"
                   "-a\n-b\n+A\n+B\n c\n")
    assert strict_patch("a\r\nb\nc\r\n", replacement) == b"A\r\nB\nc\r\n"

    hunks = ("--- old\n+++ new\n@@ -1,2 +1,2 @@\n"
             " a\n-b\n+B\n@@ -4 +4 @@\n-d\n+D\n")
    assert strict_patch("a\r\nb\nc\r\nd\n", hunks) == b"a\r\nB\nc\r\nD\n"
    with pytest.raises(ValueError, match="does not match"):
        strict_patch("a\r\nb\nc\r\nchanged\n", hunks)



@pytest.mark.skipif(os.name != "nt", reason="native inherited held file")
def test_native_held_patch_preserves_mixed_endings_and_rejects_stale_context(tmp_path):
    from orchestrator.permissions.file_worker import FilePlanRejected, run
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_write
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    target = tmp_path / "mixed.txt"
    target.write_bytes(b"one\r\ntwo\nthree\r\n")
    scope = normalize_scope("file", str(target), ["create", "read", "write"], cwd=tmp_path)
    patch = "--- old\n+++ new\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"

    def apply():
        with open_bound_write(scope) as bound:
            _, transfer = inheritable_handle_copy(bound._handles[-1])
            plan = {"format": "hashi-file-helper-v1", "action": "patch_write", "handles": [transfer],
                    "directory": "", "name": "", "data": base64.b64encode(patch.encode()).decode(),
                    "result": ""}
            return run(plan)

    assert apply()["outcome"] == "succeeded"
    expected = b"one\r\nTWO\nthree\r\n"
    assert target.read_bytes() == expected
    with pytest.raises(FilePlanRejected):
        apply()
    assert target.read_bytes() == expected



def test_exact_patch_treats_unicode_line_separators_as_file_content():
    original = "one\u2028two\r\nthree\u0085four\n"
    patch = ("--- old\n+++ new\n@@ -1,2 +1,2 @@\n"
             "-one\u2028two\n+ONE\u2028TWO\n three\u0085four\n")
    assert strict_patch(original, patch) == "ONE\u2028TWO\r\nthree\u0085four\n".encode("utf-8")
    with pytest.raises(ValueError, match="does not match"):
        strict_patch("one\u2028changed\r\nthree\u0085four\n", patch)
