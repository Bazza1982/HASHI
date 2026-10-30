"""The managed-tool descriptor must describe real effects and fail closed."""
import os
import pytest

from orchestrator.permissions.descriptors import (
    UnknownTool,
    describe_tool,
)
from orchestrator.permissions.models import scope_covers


def test_file_write_binds_path_and_argument_digest_without_exposing_content(tmp_path):
    path = tmp_path / "report.txt"
    descriptor = describe_tool(
        "file_write", {"path": str(path), "content": "private content"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    assert descriptor.coverage == "partial"
    assert descriptor.scopes[0].kind == "file"
    assert descriptor.scopes[0].resource == os.path.normcase(str(path.resolve()))
    assert "write" in descriptor.scopes[0].actions
    assert "private content" not in repr(descriptor)
    changed = describe_tool(
        "file_write", {"path": str(path), "content": "changed"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    assert changed.arguments_digest != descriptor.arguments_digest


def test_search_describes_all_requested_roots_instead_of_common_parent(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    descriptor = describe_tool(
        "file_search", {"query": "x", "roots": [str(first), str(second)]},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    assert len(descriptor.scopes) == 2
    assert {scope.resource for scope in descriptor.scopes} == {
        os.path.normcase(str(first.resolve())), os.path.normcase(str(second.resolve()))
    }
    assert all("read" in scope.actions for scope in descriptor.scopes)


def test_shell_and_argv_verification_are_unrestricted_execution_not_a_file_fence(tmp_path):
    shell = describe_tool(
        "shell", {"command": "print('hello')"}, cwd=tmp_path, read_roots=(tmp_path,),
    )
    verification = describe_tool(
        "verification_run", {"operation": "run", "argv": ["python", "-c", "print(1)"]},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    for descriptor in (shell, verification):
        assert descriptor.coverage == "partial"
        assert any(scope.kind == "capability" and scope.resource == "process.unrestricted"
                   and "execute" in scope.actions for scope in descriptor.scopes)
        assert all(scope.kind != "file" for scope in descriptor.scopes)


def test_network_read_and_upload_are_separate_and_redirects_not_claimed_enforced(tmp_path):
    fetch = describe_tool(
        "http_request", {"url": "https://example.com/a", "method": "GET"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    upload = describe_tool(
        "http_request", {"url": "https://example.com/a", "method": "POST", "body": "secret"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    assert fetch.scopes[0].resource == "https://example.com:443"
    assert {"fetch", "send"} <= set(fetch.scopes[0].actions)
    # A path can encode read data even without headers, a body or a query.
    assert "upload" in upload.scopes[0].actions
    assert not scope_covers(fetch.scopes[0], upload.scopes[0])
    assert fetch.coverage == "partial"
    assert upload.coverage == "partial"
    assert "secret" not in repr(upload)


def test_question_control_remains_available_without_becoming_approval(tmp_path):
    descriptor = describe_tool(
        "ask_user", {"question": "Which name?", "idempotency_key": "q1"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    assert descriptor.coverage == "control"
    assert descriptor.scopes == ()


def test_unknown_tools_and_unserializable_arguments_fail_closed(tmp_path):
    with pytest.raises(UnknownTool):
        describe_tool("new_mutating_plugin", {}, cwd=tmp_path, read_roots=(tmp_path,))
    with pytest.raises(ValueError):
        describe_tool("file_read", {"path": str(tmp_path), "unexpected": object()},
                      cwd=tmp_path, read_roots=(tmp_path,))


def test_json_key_coercion_and_nonstring_search_roots_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        describe_tool("file_read", {"path": str(tmp_path), "nested": {1: "x"}},
                      cwd=tmp_path, read_roots=(tmp_path,))
    with pytest.raises(ValueError):
        describe_tool("file_search", {"query": "x", "roots": [123]},
                      cwd=tmp_path, read_roots=(tmp_path,))
