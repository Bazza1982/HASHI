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
        first = json.loads(search_held([bound], [str(tmp_path)], arguments, [str(private)]))
    assert [row["line"] for row in first["matches"]] == [1, 2]
    assert first["coverage_complete"] is False and first["cursor"]
    with open_bound_directory(scope) as bound:
        second = json.loads(search_held([bound], [str(tmp_path)],
                                       {**arguments, "cursor": first["cursor"]}, [str(private)]))
    assert [row["line"] for row in second["matches"]] == [3]
    assert second["coverage_complete"] is True
    with open_bound_directory(scope) as bound:
        with pytest.raises(ValueError, match="another query"):
            search_held([bound], [str(tmp_path)], {**arguments, "query": "secret", "cursor": first["cursor"]}, [])


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
