"""Portable markers must describe the exact delivered bytes, never just logs."""
from __future__ import annotations
import ast
import asyncio
import hashlib
import json

import pytest

from tests.test_fingerprint_execution import registry_at, events
from orchestrator.fingerprint.execution import reconcile
from orchestrator.fingerprint.service import FingerprintAudit
from orchestrator.fingerprint.manifest import read_manifest, write_sidecar


@pytest.mark.asyncio
async def test_python_header_semantics_crlf_and_stable_single_marker(tmp_path):
    registry, store, work = registry_at(tmp_path)
    source = "#!/usr/bin/env python\n# coding: utf-8\n# Copyright Example\nvalue = 1\n"
    result = await registry.execute("file_write", {"path": "code.py", "content": source}, tool_call_id="first")
    target = work / "code.py"
    first = target.read_bytes()
    assert b"# HASHI-Fingerprint: hfp_" in first
    assert first.startswith(source[:source.index("value")].replace("\n", "\r\n").encode()) if __import__("os").name == "nt" else first.startswith(b"#!/usr/bin/env python")
    assert ast.dump(ast.parse(first.decode())) == ast.dump(ast.parse(source))
    manifest = read_manifest(work)
    record = manifest["receipts"][manifest["artifacts"]["code.py"]["latest_receipt"]]
    assert record["after"]["sha256"] == hashlib.sha256(first).hexdigest()
    assert record["marker_changed_bytes"] is True
    again = await registry.execute("file_write", {"path": "code.py", "content": source}, tool_call_id="again")
    final = target.read_bytes()
    assert final == first
    assert final.count(b"HASHI-Fingerprint:") == 1
    assert again.details["fingerprint"]["artifact_id"] == result.details["fingerprint"]["artifact_id"]
    assert again.details["fingerprint"]["action"] == "no_change"


@pytest.mark.asyncio
async def test_json_signed_generated_and_binary_use_manifest_without_injection(tmp_path):
    registry, store, work = registry_at(tmp_path)
    for name, content in [("data.json", '{"x":1}'), ("signed.py", "# signed file\nx=1\n"),
                           ("generated.py", "# DO NOT EDIT\nx=1\n")]:
        result = await registry.execute("file_write", {"path": name, "content": content}, tool_call_id=name)
        assert not result.is_error
        assert (work / name).read_text() == content
        assert result.details["fingerprint"]["marker_status"] == "manifest"
    manifest = read_manifest(work)
    record = manifest["receipts"][manifest["artifacts"]["data.json"]["latest_receipt"]]
    sidecar = write_sidecar(work / "data.json", record)
    assert json.loads(sidecar.read_bytes())["receipt"]["after"]["sha256"] == hashlib.sha256((work / "data.json").read_bytes()).hexdigest()
    assert set(manifest["artifacts"]) == {"data.json", "signed.py", "generated.py"}


@pytest.mark.asyncio
async def test_unknown_namespace_and_sidecar_collision_preserve_existing_work(tmp_path):
    registry, store, work = registry_at(tmp_path)
    folder = work / ".hashi-fingerprint"
    folder.mkdir()
    (folder / "manifest.json").write_text("human data")
    result = await registry.execute("file_write", {"path": "saved.json", "content": "{}"}, tool_call_id="call")
    assert not result.is_error and (work / "saved.json").read_bytes() == b"{}"
    assert result.details["fingerprint"]["marker_status"] == "pending"
    assert (folder / "manifest.json").read_text() == "human data"
    destination = work / "saved.json.hashi.json"
    destination.write_text("keep")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        write_sidecar(work / "saved.json", result.details["fingerprint"])
    assert destination.read_text() == "keep"


@pytest.mark.asyncio
async def test_manifest_failure_and_recovery_never_replay_the_write(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    from orchestrator.fingerprint import manifest
    real_project = manifest.project
    monkeypatch.setattr(manifest, "project", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    result = await registry.execute("file_write", {"path": "data.json", "content": "{}"}, tool_call_id="once")
    assert not result.is_error and result.details["fingerprint"]["marker_status"] == "pending"
    monkeypatch.setattr(manifest, "project", real_project)
    from orchestrator.fingerprint.recovery import repair
    repaired = repair(FingerprintAudit(store), work,
        authorization={"actor": "test", "purpose": "repair", "allow_raw_audit": True})
    assert repaired[0]["status"] == "projected"
    assert len(events(store, "fingerprint_prepared")) == 1
    assert (work / "data.json").read_text() == "{}"
    # A later external edit is never repaired using a stale receipt.
    (work / "data.json").write_text("external")
    again = repair(FingerprintAudit(store), work,
        authorization={"actor": "test", "purpose": "repair", "allow_raw_audit": True})
    assert any(item["status"] == "version_mismatch" for item in again)


@pytest.mark.asyncio
async def test_distinct_files_and_agents_do_not_infer_identity_from_equal_hashes(tmp_path):
    first, store, work = registry_at(tmp_path)
    second, other_store, _ = registry_at(tmp_path, agent="other")
    await asyncio.gather(
        first.execute("file_write", {"path": "one.json", "content": "{}"}, tool_call_id="one"),
        second.execute("file_write", {"path": "two.json", "content": "{}"}, tool_call_id="two"))
    manifest = read_manifest(work)
    one, two = (manifest["artifacts"][key] for key in ("one.json", "two.json"))
    assert one["sha256"] == two["sha256"] and one["artifact_id"] != two["artifact_id"]
    assert manifest["receipts"][one["latest_receipt"]]["identity"]["agent_id"] == "agent"
    assert manifest["receipts"][two["latest_receipt"]]["identity"]["agent_id"] == "other"
