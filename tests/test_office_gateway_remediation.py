"""Office gateway behavior using isolated files and a deterministic test sidecar."""

import asyncio
import errno
import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.office.gateway import GatewayError, OfficeGateway


class TestSidecar:
    __test__ = False

    def __init__(self, state_root, **options):
        self.state_root = Path(state_root).resolve()
        self.options = {"state_root": str(self.state_root), **options}
        self.timeout = 30

    def manifest(self, verify=False):
        return {}

    async def invoke(self, operation, args, job):
        if operation == "inspect":
            return {"status": "ok", "data": {}}
        if operation == "read":
            return {"status": "ok", "data": {"content": (job / args["input"]).read_text()}}
        if operation == "edit":
            content = (job / args["input"]).read_bytes() + args["content"].encode()
            output = job / args["output"]
            output.write_bytes(content)
            return {"status": "ok", "data": {}, "output": output.name,
                    "sha256": hashlib.sha256(content).hexdigest()}
        if operation == "finish":
            return {"status": "ok", "data": {}}
        raise AssertionError(operation)


def make_gateway(tmp_path, **options):
    runtime = TestSidecar(tmp_path / "state", **options)
    return OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
                         context={"agent_name": "office-test", "hashi_session_id": "test-session",
                                  "request_id": "test-request", "instance_id": "TEST"})


def call(gateway, tool, args, call_id):
    return asyncio.run(gateway.execute(tool, args, call_id))


def test_independent_copy_survives_source_change_and_reports_provenance(tmp_path, monkeypatch):
    original = tmp_path / "original.docx"
    original.write_bytes(b"old")
    gateway = make_gateway(tmp_path, minimum_free_bytes=2 * 1024**3)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1024**2))

    opened = call(gateway, "office_inspect", {"path": str(original)}, "inspect")
    assert opened["status"] == "ok", opened
    file_id = opened["file_id"]
    original.write_bytes(b"changed outside")
    read = call(gateway, "office_read", {"file_id": file_id}, "read")
    assert read["status"] == "ok"
    assert read["data"]["content"] == "old"
    assert read["data"]["source_provenance"] == "changed"

    blocked = call(gateway, "office_edit", {"file_id": file_id, "action": "append_section",
                   "content": " new"}, "reserved")
    assert blocked["code"] == "STORAGE_LIMIT"
    gateway.runtime.options.pop("minimum_free_bytes")
    edited = call(gateway, "office_edit", {"file_id": file_id, "action": "append_section",
                  "content": " new"}, "edit")
    assert edited["status"] == "ok", edited
    assert edited["data"]["source_provenance"] == "changed"
    saved = call(gateway, "office_finish", {"file_id": file_id, "output": "copy.docx"}, "save")
    assert saved["status"] == "ok", saved
    assert saved["data"]["source_provenance"] == "changed"
    assert (tmp_path / "copy.docx").read_bytes() == b"old new"
    refused = call(gateway, "office_finish", {"file_id": file_id, "output": str(original)}, "original")
    assert refused["code"] == "ORIGINAL_PROTECTED"
    assert original.read_bytes() == b"changed outside"


def test_disk_full_does_not_publish_partial_output_and_prepared_save_can_recover(tmp_path, monkeypatch):
    original = tmp_path / "source.docx"
    original.write_bytes(b"source")
    gateway = make_gateway(tmp_path)
    file_id = call(gateway, "office_inspect", {"path": str(original)}, "inspect")["file_id"]
    changed = call(gateway, "office_edit", {"file_id": file_id, "action": "append_section",
                   "content": " addition"}, "edit")
    assert changed["status"] == "ok", changed
    args = {"file_id": file_id, "output": "output.docx"}

    def fail_after_partial_write(reader, writer):
        writer.write(b"partial")
        raise OSError(errno.ENOSPC, "simulated full disk")

    with monkeypatch.context() as failure:
        failure.setattr(shutil, "copyfileobj", fail_after_partial_write)
        result = call(gateway, "office_finish", args, "save")
    assert result["code"] == "STORAGE_LIMIT"
    assert original.read_bytes() == b"source"
    assert not (tmp_path / "output.docx").exists()
    assert list(tmp_path.glob(".office-*.tmp")) == []

    recovered = call(gateway, "office_finish", args, "save")
    assert recovered["status"] == "ok", recovered
    assert (tmp_path / "output.docx").read_bytes() == b"source addition"
    assert call(gateway, "office_finish", args, "save") == recovered


def test_publish_without_hardlinks_and_preserve_racing_target(tmp_path, monkeypatch):
    source = tmp_path / "working.docx"
    source.write_bytes(b"verified")
    target = tmp_path / "new.docx"

    def unsupported_link(*_args):
        raise OSError(errno.EPERM, "hardlinks unavailable")

    monkeypatch.setattr(os, "link", unsupported_link)
    OfficeGateway._atomic_new_file(source, target)
    assert target.read_bytes() == b"verified"

    racing_target = tmp_path / "race.docx"
    atomic_rename = OfficeGateway._atomic_rename_no_replace

    def race(temp, output):
        output.write_bytes(b"concurrent writer")
        atomic_rename(temp, output)

    monkeypatch.setattr(OfficeGateway, "_atomic_rename_no_replace", staticmethod(race))
    with pytest.raises(GatewayError) as failure:
        OfficeGateway._atomic_new_file(source, racing_target)
    assert failure.value.code == "OUTPUT_EXISTS"
    assert racing_target.read_bytes() == b"concurrent writer"
    assert source.read_bytes() == b"verified"
    assert list(tmp_path.glob(".office-*.tmp")) == []
# HASHI-Fingerprint: hfp_78ec873d7caf4502aa11cd61f22a4afc



def _office_test_link_or_skip(target, link, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink creation unavailable: {type(error).__name__}")


def test_office_reads_authorized_file_and_directory_aliases_and_edits_a_copy(tmp_path):
    target_dir = tmp_path / "source"
    target_dir.mkdir()
    source = target_dir / "report.docx"
    source.write_bytes(b"original")
    file_link = tmp_path / "report-link.docx"
    dir_link = tmp_path / "source-link"
    _office_test_link_or_skip(source, file_link)
    _office_test_link_or_skip(target_dir, dir_link, directory=True)
    gateway = make_gateway(tmp_path)

    assert gateway.read_bytes(file_link) == b"original"
    opened = call(gateway, "office_inspect", {"path": str(dir_link / "report.docx")}, "alias-inspect")
    assert opened["status"] == "ok", opened
    edited = call(gateway, "office_edit", {"file_id": opened["file_id"], "action": "append_section",
                  "content": " edited"}, "alias-edit")
    assert edited["status"] == "ok", edited
    saved = call(gateway, "office_finish", {"file_id": opened["file_id"],
                 "output": str(tmp_path / "copy.docx")}, "alias-save")
    assert saved["status"] == "ok", saved
    assert (tmp_path / "copy.docx").read_bytes() == b"original edited"
    assert source.read_bytes() == b"original"


def test_office_read_alias_outside_authorized_root_is_rejected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("office-outside") / "secret.docx"
    outside.write_bytes(b"outside")
    link = tmp_path / "outside.docx"
    _office_test_link_or_skip(outside, link)
    gateway = make_gateway(tmp_path)
    with pytest.raises(GatewayError) as error:
        gateway.read_bytes(link)
    assert error.value.code == "PATH_DENIED"
    assert outside.read_bytes() == b"outside"


def test_office_alias_retarget_cannot_redirect_a_checked_read(tmp_path, tmp_path_factory, monkeypatch):
    import tools.builtins as builtins

    original = tmp_path / "safe.docx"
    original.write_bytes(b"safe")
    outside = tmp_path_factory.mktemp("office-retarget") / "secret.docx"
    outside.write_bytes(b"outside")
    link = tmp_path / "link.docx"
    _office_test_link_or_skip(original, link)
    gateway = make_gateway(tmp_path)
    resolve = builtins._resolve_path
    changed = False

    def retarget_after_resolution(raw, *args, **kwargs):
        nonlocal changed
        target = resolve(raw, *args, **kwargs)
        if Path(raw) == link and not changed:
            changed = True
            link.unlink()
            os.symlink(outside, link)
        return target

    monkeypatch.setattr(builtins, "_resolve_path", retarget_after_resolution)
    assert gateway.read_bytes(link) == b"safe"
    assert changed
    assert outside.read_bytes() == b"outside"


def test_office_canonical_parent_replacement_cannot_redirect_open(tmp_path, tmp_path_factory, monkeypatch):
    import orchestrator.session_attachment_authorization as attachment_authorization

    folder = tmp_path / "chosen"
    folder.mkdir()
    source = folder / "report.docx"
    source.write_bytes(b"safe")
    outside_dir = tmp_path_factory.mktemp("office-replaced-parent")
    (outside_dir / "report.docx").write_bytes(b"outside")
    gateway = make_gateway(tmp_path)
    secure_open = attachment_authorization._open_no_symlinks
    changed = False

    def replace_parent_before_open(path, flags):
        nonlocal changed
        if Path(path) == source and not changed:
            changed = True
            folder.rename(tmp_path / "original-folder")
            os.symlink(outside_dir, folder, target_is_directory=True)
        return secure_open(path, flags)

    monkeypatch.setattr(attachment_authorization, "_open_no_symlinks", replace_parent_before_open)
    with pytest.raises(GatewayError) as error:
        gateway.read_bytes(source)
    assert error.value.code in {"SOURCE_CHANGED", "PATH_DENIED"}
    assert changed
    assert (outside_dir / "report.docx").read_bytes() == b"outside"



def test_office_read_alias_retains_the_canonical_path_guard(tmp_path):
    source = tmp_path / "guarded.docx"
    source.write_bytes(b"guarded")
    link = tmp_path / "alias.docx"
    _office_test_link_or_skip(source, link)
    gateway = make_gateway(tmp_path)
    seen = []

    def guard(path, write):
        seen.append((path, write))
        raise GatewayError("PATH_DENIED", "synthetic protected path")

    gateway.path_guard = guard
    with pytest.raises(GatewayError) as error:
        gateway.read_bytes(link)
    assert error.value.code == "PATH_DENIED"
    assert seen == [(source.resolve(), False)]
    assert source.read_bytes() == b"guarded"


def _office_test_windows_junction(target, link):
    import ctypes
    import struct
    from ctypes import wintypes

    link.mkdir()
    substitute = ('\\??\\' + str(target)).encode("utf-16-le")
    display = str(target).encode("utf-16-le")
    names = substitute + bytes(2) + display + bytes(2)
    payload = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(names), 0,
                          0, len(substitute), len(substitute) + 2, len(display)) + names
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    ioctl.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(link), 0x40000000, 0x7, None, 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD()
        if not ioctl(handle, 0x000900A4, buffer, len(payload), None, 0, ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)
    assert link.is_junction()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_office_windows_junction_resolves_in_root_and_rejects_outside(tmp_path, tmp_path_factory):
    source_dir = tmp_path / "source-junction-target"
    source_dir.mkdir()
    (source_dir / "report.docx").write_bytes(b"inside")
    outside = tmp_path_factory.mktemp("office-junction-outside")
    (outside / "secret.docx").write_bytes(b"outside")
    inside_link, outside_link = tmp_path / "inside-junction", tmp_path / "outside-junction"
    try:
        _office_test_windows_junction(source_dir, inside_link)
        _office_test_windows_junction(outside, outside_link)
    except OSError as error:
        pytest.skip(f"junction creation unavailable: {type(error).__name__}")
    gateway = make_gateway(tmp_path)
    assert gateway.read_bytes(inside_link / "report.docx") == b"inside"
    with pytest.raises(GatewayError) as error:
        gateway.read_bytes(outside_link / "secret.docx")
    assert error.value.code == "PATH_DENIED"
    assert (outside / "secret.docx").read_bytes() == b"outside"



def test_office_session_can_open_more_than_512_working_files(tmp_path):
    source = tmp_path / "new.docx"
    source.write_text("new source", encoding="utf-8")
    gateway = make_gateway(tmp_path)

    # The existing session has 512 valid, retained working-copy records.
    seed = gateway.root / "existing.docx"
    seed.write_text("earlier work", encoding="utf-8")
    sha = hashlib.sha256(seed.read_bytes()).hexdigest()
    with gateway.db() as db:
        db.executemany(
            "INSERT INTO files(id,path,suffix,sha256,revision,origin,origin_sha256,output,saved,facts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(f"existing-{index}", str(seed), ".docx", sha, 1, None, None, None, None, "{}")
             for index in range(512)],
        )

    opened = call(gateway, "office_inspect", {"path": str(source)}, "open-513")
    assert opened["status"] == "ok", opened
    assert call(gateway, "office_read", {"file_id": opened["file_id"]}, "read-new")["data"]["content"] == "new source"
    assert call(gateway, "office_read", {"file_id": "existing-0"}, "read-existing")["data"]["content"] == "earlier work"

    other = OfficeGateway(
        gateway.runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
        context={**gateway.context, "hashi_session_id": "other-session"},
    )
    denied = call(other, "office_read", {"file_id": opened["file_id"]}, "foreign")
    assert denied["code"] == "FILE_NOT_FOUND"
    assert source.read_text(encoding="utf-8") == "new source"



def test_office_reopens_an_authorized_session_working_copy(tmp_path, tmp_path_factory, monkeypatch):
    import json

    from orchestrator.session_store import SessionStore
    from tools.office import integration

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:alice", "office-test"
    original_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    current_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    other_owner = store.create_session(owner_id="user:bob", agent_id=agent)["session_id"]
    other_agent = store.create_session(owner_id=owner, agent_id="another-agent")["session_id"]
    runtime = TestSidecar(tmp_path / "state")
    base_context = {
        "agent_name": agent, "owner_id": owner, "instance_id": "TEST",
        "request_id": "request", "_runtime": SimpleNamespace(session_store=store),
    }
    prior = OfficeGateway(
        runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
        context={**base_context, "hashi_session_id": original_session},
    )
    source = tmp_path / "original.docx"
    source.write_text("original", encoding="utf-8")
    opened = call(prior, "office_inspect", {"path": str(source)}, "original-open")
    prior_id = opened["file_id"]
    changed = call(
        prior, "office_edit",
        {"file_id": prior_id, "action": "append_section", "content": " unsaved"},
        "original-edit",
    )
    assert changed["status"] == "ok" and changed["data"]["revision"] == 2
    with prior.db() as db:
        row = db.execute("SELECT facts FROM files WHERE id=?", (prior_id,)).fetchone()
        facts = json.loads(row["facts"])
        facts.update(attachment_id="source-session-only", attachment_sha256="a" * 64)
        db.execute("UPDATE files SET facts=? WHERE id=?", (json.dumps(facts), prior_id))

    monkeypatch.setattr(integration, "OfficeRuntime", lambda _options: runtime)

    class Registry:
        access_roots = (tmp_path,)
        tool_options = {}

        def __init__(self, context):
            self.context = context

        def _effective_audit_context(self):
            return self.context

        def _read_scope(self):
            return (tmp_path,), None, tmp_path

        def _check_live_runtime_gate(self, *_args, **_kwargs):
            return None

        def _check_enterprise_path_gate(self, *_args, **_kwargs):
            return None

    context = {**base_context, "hashi_session_id": current_session}
    registry = Registry(context)

    def run(name, args, call_id, current=registry):
        response = asyncio.run(integration.execute_office(current, name, args, call_id))
        return json.loads(response.output)

    request = {
        "source": "working_copy", "source_session_id": original_session,
        "source_file_id": prior_id, "source_revision": 2,
    }
    restored = run("office_inspect", request, "reopen")
    assert restored["status"] == "ok", restored
    assert restored["file_id"] != prior_id
    assert restored["data"]["source_revision"] == 2
    assert "attachment_id" not in restored["data"]
    assert "attachment_sha256" not in restored["data"]
    assert run("office_inspect", request, "reopen") == restored
    descriptor_registry = Registry({
        **context, "_runtime": None,
        "session_store_descriptor": {
            "db_path": str(store.db_path), "instance_id": store.instance_id,
            "attachment_root": str(store.attachment_files_root),
        },
    })
    descriptor_copy = run("office_inspect", request, "descriptor-reopen", current=descriptor_registry)
    assert descriptor_copy["status"] == "ok", descriptor_copy

    from tools.office.contract import OFFICE_TOOL_NAMES
    from tools.registry import ToolRegistry

    actual_registry = ToolRegistry(
        allowed_tools=list(OFFICE_TOOL_NAMES), access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context=context,
    )
    monkeypatch.setattr(
        actual_registry, "_office_runtime",
        lambda: SimpleNamespace(availability=lambda: {"available": True}),
    )
    definitions = actual_registry.get_tool_definitions(tiers=["office_basic"])
    inspect_schema = next(d["function"]["parameters"] for d in definitions
                          if d["function"]["name"] == "office_inspect")
    assert "working_copy" in inspect_schema["properties"]["source"]["enum"]
    dispatched = asyncio.run(actual_registry.execute("office_inspect", request, "registry-reopen"))
    assert not dispatched.is_error, dispatched.output
    assert json.loads(dispatched.output)["status"] == "ok"

    selected_root = tmp_path / "selected-workzone"
    selected_root.mkdir()

    class RestrictedRegistry(Registry):
        access_roots = (selected_root,)

        def _read_scope(self):
            return (selected_root,), None, selected_root

    restricted_registry = RestrictedRegistry(context)
    restricted = run("office_inspect", request, "outside-workzone", current=restricted_registry)
    assert restricted["code"] == "PATH_DENIED"
    assert run("office_inspect", request, "reopen", current=restricted_registry)["code"] == "PATH_DENIED"

    outside_asset = tmp_path_factory.mktemp("office-external-asset") / "asset.png"
    outside_asset.write_bytes(b"external asset")
    with prior.db() as db:
        original_facts = db.execute("SELECT facts FROM files WHERE id=?", (prior_id,)).fetchone()["facts"]
        changed_facts = json.loads(original_facts)
        changed_facts["origins"].append({
            "path": str(outside_asset), "sha256": hashlib.sha256(outside_asset.read_bytes()).hexdigest(),
        })
        db.execute("UPDATE files SET facts=? WHERE id=?", (json.dumps(changed_facts), prior_id))
    try:
        assert run("office_inspect", request, "outside-asset")["code"] == "PATH_DENIED"
    finally:
        with prior.db() as db:
            db.execute("UPDATE files SET facts=? WHERE id=?", (original_facts, prior_id))

    # A removed original does not destroy the already-authorized snapshot.
    moved_source = source.with_name("original-moved.docx")
    source.rename(moved_source)
    try:
        missing_origin = run("office_inspect", request, "missing-original")
        assert missing_origin["status"] == "ok", missing_origin
        assert missing_origin["data"]["source_provenance"] == "unavailable"
    finally:
        moved_source.rename(source)

    assert run("office_read", {"file_id": restored["file_id"]}, "read-restored")["data"]["content"] == "original unsaved"
    assert run("office_read", {"file_id": prior_id}, "foreign-handle")["code"] == "FILE_NOT_FOUND"

    new_edit = run(
        "office_edit",
        {"file_id": restored["file_id"], "action": "append_section", "content": " in new session"},
        "edit-restored",
    )
    assert new_edit["status"] == "ok", new_edit
    assert call(prior, "office_read", {"file_id": prior_id}, "read-prior")["data"]["content"] == "original unsaved"
    assert source.read_text(encoding="utf-8") == "original"

    stale = run("office_inspect", {**request, "source_revision": 1}, "stale")
    assert stale["code"] == "SOURCE_CHANGED"
    malformed = run("office_inspect", {**request, "source_revision": True}, "malformed")
    assert malformed["code"] == "INVALID_FIELD"
    incomplete = run("office_inspect", {"source": "working_copy", "source_file_id": prior_id}, "incomplete")
    assert incomplete["code"] == "MISSING_FIELD"
    wrong_owner = Registry({**context, "owner_id": "user:bob"})
    assert run("office_inspect", request, "wrong-owner", current=wrong_owner)["code"] == "WORKING_COPY_UNAVAILABLE"
    assert run("office_inspect", request, "reopen", current=wrong_owner)["code"] == "WORKING_COPY_UNAVAILABLE"
    for denied_session in (other_owner, other_agent):
        denied = run("office_inspect", {**request, "source_session_id": denied_session}, "deny-" + denied_session)
        assert denied["code"] == "WORKING_COPY_UNAVAILABLE"
    wrong_instance = Registry({**context, "instance_id": "OTHER"})
    denied = run("office_inspect", request, "wrong-instance", current=wrong_instance)
    assert denied["code"] == "WORKING_COPY_UNAVAILABLE"

    prior_path = Path(prior.handle(prior_id)["path"])
    outside_state = prior.root.parent / "outside.docx"
    outside_state.write_text("original unsaved", encoding="utf-8")
    escaping_path = prior.root / ".." / "outside.docx"
    with prior.db() as db:
        db.execute("UPDATE files SET path=? WHERE id=?", (str(escaping_path), prior_id))
    escaped = run("office_inspect", request, "escaped-source")
    assert escaped["code"] == "WORKING_COPY_CHANGED"
    assert outside_state.read_text(encoding="utf-8") == "original unsaved"
    with prior.db() as db:
        db.execute("UPDATE files SET path=? WHERE id=?", (str(prior_path), prior_id))
    prior_path.write_text("tampered", encoding="utf-8")
    damaged = run("office_inspect", request, "damaged")
    assert damaged["code"] == "WORKING_COPY_CHANGED"

    # A completed copy belongs to the target Session even if the old Session
    # is later deleted; a new copy must still verify the source Session.
    with store._connection() as db:
        db.execute("UPDATE sessions SET status='deleted' WHERE session_id=?", (original_session,))
    assert run("office_inspect", request, "reopen") == restored
    assert run("office_inspect", request, "after-source-deleted")["code"] == "WORKING_COPY_UNAVAILABLE"



def test_office_reopen_keeps_exact_snapshot_when_source_changes_concurrently(tmp_path, monkeypatch):
    import json

    from orchestrator.session_store import SessionStore
    from tools.office import integration

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:alice", "office-test"
    source_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    target_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    runtime = TestSidecar(tmp_path / "state")
    context = {
        "agent_name": agent, "owner_id": owner, "instance_id": "TEST", "request_id": "request",
        "_runtime": SimpleNamespace(session_store=store), "hashi_session_id": source_session,
    }
    source = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                           cwd=tmp_path, context=context)
    original = tmp_path / "concurrent.docx"
    original.write_text("version one", encoding="utf-8")
    opened = call(source, "office_inspect", {"path": str(original)}, "source-open")
    file_id = opened["file_id"]
    monkeypatch.setattr(integration, "OfficeRuntime", lambda _options: runtime)

    class Registry:
        access_roots = (tmp_path,)
        tool_options = {}

        def _effective_audit_context(self):
            return {**context, "hashi_session_id": target_session}

        def _read_scope(self):
            return (tmp_path,), None, tmp_path

        def _check_live_runtime_gate(self, *_args, **_kwargs):
            return None

        def _check_enterprise_path_gate(self, *_args, **_kwargs):
            return None

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        invoke = runtime.invoke

        async def pause_inspect(operation, args, job):
            if operation == "inspect" and not entered.is_set():
                entered.set()
                await release.wait()
            return await invoke(operation, args, job)

        monkeypatch.setattr(runtime, "invoke", pause_inspect)
        request = {"source": "working_copy", "source_session_id": source_session,
                   "source_file_id": file_id, "source_revision": 1}
        registry = Registry()
        pending = asyncio.create_task(integration.execute_office(registry, "office_inspect", request, "reopen"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        try:
            changed = await source.execute(
                "office_edit", {"file_id": file_id, "action": "append_section", "content": " updated"},
                "source-edit",
            )
            assert changed["status"] == "ok" and changed["data"]["revision"] == 2
        finally:
            release.set()
        restored = json.loads((await pending).output)
        assert restored["status"] == "ok" and restored["data"]["source_revision"] == 1
        read = json.loads((await integration.execute_office(
            registry, "office_read", {"file_id": restored["file_id"]}, "read-snapshot",
        )).output)
        assert read["data"]["content"] == "version one"
        current = await source.execute("office_read", {"file_id": file_id}, "read-current")
        assert current["data"]["content"] == "version one updated"
        stale = json.loads((await integration.execute_office(
            registry, "office_inspect", request, "retry-old-version",
        )).output)
        assert stale["code"] == "SOURCE_CHANGED"

    asyncio.run(scenario())



def test_office_working_copy_discovery_is_paginated_and_scoped(tmp_path, tmp_path_factory, monkeypatch):
    import json

    from orchestrator.session_store import SessionStore
    from tools.office import integration

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:alice", "office-test"
    source_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    target_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    other_target = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    empty_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    foreign = store.create_session(owner_id="user:bob", agent_id=agent)["session_id"]
    runtime = TestSidecar(tmp_path / "state")
    base = {"agent_name": agent, "owner_id": owner, "instance_id": "TEST",
            "request_id": "list-request", "_runtime": SimpleNamespace(session_store=store)}
    source = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
                           context={**base, "hashi_session_id": source_session})
    original = tmp_path / "draft.docx"
    original.write_text("document", encoding="utf-8")
    first = call(source, "office_inspect", {"path": str(original)}, "source-open")
    item = source.handle(first["file_id"])
    expected = {first["file_id"]}
    with source.db() as db:
        for index in range(56):
            if index == 49:
                continue
            item_id = f"office_{index:032x}"
            expected.add(item_id)
            record = {**item, "id": item_id}
            db.execute("INSERT INTO files(id,path,suffix,sha256,revision,origin,origin_sha256,output,saved,facts) "
                       "VALUES(:id,:path,:suffix,:sha256,:revision,:origin,:origin_sha256,:output,:saved,:facts)", record)

    private = tmp_path_factory.mktemp("outside-office-discovery") / "private.docx"
    private.write_text("private", encoding="utf-8")
    private_id = f"office_{49:032x}"
    facts = json.loads(item["facts"])
    facts["origins"] = [{"path": str(private), "sha256": hashlib.sha256(private.read_bytes()).hexdigest()}]
    hidden = {**item, "id": private_id, "origin": str(private), "facts": json.dumps(facts)}
    generated_id = f"office_{56:032x}"
    expected.add(generated_id)
    generated = {**item, "id": generated_id, "origin": None, "origin_sha256": None,
                 "output": str(private.parent / "not-visible.docx"), "facts": json.dumps({"origins": []})}
    with source.db() as db:
        for record in (hidden, generated):
            db.execute("INSERT INTO files(id,path,suffix,sha256,revision,origin,origin_sha256,output,saved,facts) "
                       "VALUES(:id,:path,:suffix,:sha256,:revision,:origin,:origin_sha256,:output,:saved,:facts)", record)

    monkeypatch.setattr(integration, "OfficeRuntime", lambda _options: runtime)

    class Registry:
        access_roots = (tmp_path,)
        tool_options = {}

        def __init__(self, context):
            self.context = context

        def _effective_audit_context(self):
            return self.context

        def _read_scope(self):
            return (tmp_path,), None, tmp_path

        def _check_live_runtime_gate(self, *_args, **_kwargs):
            return None

        def _check_enterprise_path_gate(self, *_args, **_kwargs):
            return None

    current = Registry({**base, "hashi_session_id": target_session})

    def run(args, call_id, registry=current):
        return json.loads(asyncio.run(integration.execute_office(registry, "office_inspect", args, call_id)).output)

    request = {"source": "list_working_copies", "source_session_id": source_session}
    page = run(request, "page")
    assert page["status"] == "ok", page
    assert page["data"]["next_cursor"]
    cursor = page["data"]["next_cursor"]
    assert private_id not in cursor
    first_cursor = cursor
    seen = list(page["data"]["files"])
    while cursor:
        page = run({**request, "cursor": cursor}, "next-" + cursor)
        assert page["status"] == "ok", page
        seen.extend(page["data"]["files"])
        cursor = page["data"]["next_cursor"]
    assert {entry["file_id"] for entry in seen} == expected
    assert all(entry["revision"] == 1 for entry in seen)
    assert all(entry["name"] == "draft.docx" for entry in seen if entry["file_id"] != generated_id)
    assert next(entry["name"] for entry in seen if entry["file_id"] == generated_id) == "Office.docx"
    assert private_id not in {entry["file_id"] for entry in seen}

    first_page = run(request, "page-again")
    cursor = first_page["data"]["next_cursor"]
    assert cursor == first_cursor
    wrong_target = Registry({**base, "hashi_session_id": other_target})
    assert run({**request, "cursor": cursor}, "wrong-target", wrong_target)["code"] == "INVALID_CURSOR"

    restricted_root = tmp_path / "different-workzone"
    restricted_root.mkdir()

    class RestrictedRegistry(Registry):
        access_roots = (restricted_root,)

        def _read_scope(self):
            return (restricted_root,), None, restricted_root

    restricted = RestrictedRegistry({**base, "hashi_session_id": target_session})
    assert run({**request, "cursor": cursor}, "wrong-workzone", restricted)["code"] == "INVALID_CURSOR"
    assert run({**request, "source_session_id": empty_session, "cursor": cursor}, "wrong-source")["code"] == "INVALID_CURSOR"
    empty = run({**request, "source_session_id": empty_session}, "empty-session")
    assert empty["status"] == "ok" and empty["data"]["files"] == []
    assert run({**request, "source_session_id": foreign}, "wrong-owner")["code"] == "WORKING_COPY_UNAVAILABLE"
    assert run({**request, "cursor": "invalid"}, "invalid")["code"] == "INVALID_CURSOR"
    with store._connection() as db:
        db.execute("UPDATE sessions SET status='deleted' WHERE session_id=?", (source_session,))
    assert run(request, "page")["code"] == "WORKING_COPY_UNAVAILABLE"



@pytest.mark.parametrize("revoked", ["source", "target", "workzone", "owner", "agent", "context", "fence"])
def test_office_reopen_revalidates_authority_at_commit(tmp_path, monkeypatch, revoked):
    import json

    from orchestrator.session_store import SessionStore
    from tools.office import integration

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:alice", "office-test"
    source_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    target_session = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    runtime = TestSidecar(tmp_path / "state")
    context = {
        "agent_name": agent, "owner_id": owner, "instance_id": "TEST",
        "request_id": "request", "_runtime": SimpleNamespace(session_store=store),
    }
    source = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                           cwd=tmp_path, context={**context, "hashi_session_id": source_session})
    original = tmp_path / "source.docx"
    original.write_text("authorized contents", encoding="utf-8")
    file_id = call(source, "office_inspect", {"path": str(original)}, "open")["file_id"]
    monkeypatch.setattr(integration, "OfficeRuntime", lambda _options: runtime)
    allowed = {"value": True}
    current_context = {**context, "hashi_session_id": target_session}
    if revoked == "fence":
        from contextlib import contextmanager

        @contextmanager
        def effect_guard():
            current_context["owner_id"] = "user:mallory"
            yield

        current_context["_tool_dispatch_guard"] = effect_guard

    class Registry:
        access_roots = (tmp_path,)
        tool_options = {}

        def _effective_audit_context(self):
            return current_context

        def _read_scope(self):
            return (tmp_path,), None, tmp_path

        def _check_live_runtime_gate(self, *_args, **_kwargs):
            return None if allowed["value"] else SimpleNamespace(output="Workzone revoked")

        def _check_enterprise_path_gate(self, *_args, **_kwargs):
            return None

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        invoke = runtime.invoke

        async def paused(operation, args, job):
            if operation == "inspect":
                entered.set()
                await release.wait()
            return await invoke(operation, args, job)

        monkeypatch.setattr(runtime, "invoke", paused)
        request = {"source": "working_copy", "source_session_id": source_session,
                   "source_file_id": file_id, "source_revision": 1}
        pending = asyncio.create_task(
            integration.execute_office(Registry(), "office_inspect", request, "reopen-revoked")
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        if revoked == "workzone":
            allowed["value"] = False
        elif revoked == "context":
            current_context["owner_id"] = "user:mallory"
        elif revoked == "fence":
            pass  # The existing effect guard changes context during commit admission.
        else:
            session = source_session if revoked in {"source", "owner"} else target_session
            with store._connection() as db:
                if revoked == "owner":
                    db.execute("UPDATE sessions SET owner_id=? WHERE session_id=?", ("user:mallory", session))
                elif revoked == "agent":
                    db.execute("UPDATE sessions SET agent_id=? WHERE session_id=?", ("other-agent", session))
                else:
                    db.execute("UPDATE sessions SET status='deleted' WHERE session_id=?", (session,))
        release.set()
        response = json.loads((await pending).output)
        expected = "PATH_DENIED" if revoked == "workzone" else "WORKING_COPY_UNAVAILABLE"
        assert response["code"] == expected, response
        target = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,),
                               cwd=tmp_path, context={**context, "hashi_session_id": target_session})
        with target.db() as db:
            assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0

    asyncio.run(scenario())



def test_office_working_copy_commit_fences_other_session_store_writer(tmp_path):
    import subprocess
    import sys
    import threading

    from orchestrator.session_store import SessionStore
    from tools.office.integration import _working_copy_commit_guard

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:alice", "office-test"
    source = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    target = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    context = {"owner_id": owner, "agent_name": agent, "instance_id": "TEST",
               "hashi_session_id": target, "_runtime": SimpleNamespace(session_store=store)}
    script = (
        "import sqlite3,sys; "
        "db=sqlite3.connect(sys.argv[1], timeout=10); "
        "print('attempt', flush=True); "
        "db.execute(\"UPDATE sessions SET status='deleted' WHERE session_id=?\", (sys.argv[2],)); "
        "db.commit(); db.close(); print('committed', flush=True)"
    )
    ready, line = threading.Event(), []
    process = None
    try:
        with _working_copy_commit_guard(context, context, source):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(store.db_path), source],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

            def read_attempt():
                line.append(process.stdout.readline().strip())
                ready.set()

            reader = threading.Thread(target=read_attempt, daemon=True)
            reader.start()
            assert ready.wait(5) and line == ["attempt"]
            assert not threading.Event().wait(0.3) and process.poll() is None
        remaining, errors = process.communicate(timeout=5)
        assert process.returncode == 0 and remaining.strip() == "committed", errors
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
    assert store.get_session(source, owner_id=owner, agent_id=agent)["status"] == "deleted"
