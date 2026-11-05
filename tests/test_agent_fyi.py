from __future__ import annotations

import base64
import hashlib
import json
import os
import re

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.agent_documentation import (
    CATALOGUE_MARKER, MANDATORY_DOMAINS, catalogue_domains, headings,
    read_documentation, validate_catalogue, validate_knowledge_migration,
)
from orchestrator.agent_fyi import (
    BOOTSTRAP_CHAR_BUDGET, build_agent_fyi_primer, load_agent_fyi_text,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def reference_repo(tmp_path):
    root = tmp_path / "repository"
    guide = root / "docs/agent-guide"
    guide.mkdir(parents=True)
    (root / "docs/AGENT_FYI.md").write_text(
        (ROOT / "docs/AGENT_FYI.md").read_text(encoding="utf-8"), encoding="utf-8",
    )
    (root / "secrets.json").write_text('{"private":"fixture sentinel"}', encoding="utf-8")
    (root / "secrets.md").write_text("private Markdown fixture sentinel", encoding="utf-8")
    (root / "AGENTS.md").write_text("Engineering boundaries fixture.", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# Architecture\n\nCurrent architecture.", encoding="utf-8")
    (root / "docs/contract.md").write_text(
        "# Contract\n\n## Conditions\n\nA complete condition and its exception.\n", encoding="utf-8",
    )
    for domain in MANDATORY_DOMAINS:
        (guide / f"{domain}.md").write_text(
            "# Guide\n\n## Purpose and when to read\nUse for the matching task.\n\n"
            "## Ownership and layer\nPCM reference, Functions.\n\n"
            "## Distinctions\nDocumented design differs from running code.\n\n"
            "## Authoritative documents\n[Contract](../contract.md#conditions)\n\n"
            "## Source and verification\nInspect the owning source and focused test.\n\n"
            "## Adoption\nVerify the actual running owner separately.\n\n"
            "DETAILED_GUIDE_BODY_NOT_FOR_BOOTSTRAP\n",
            encoding="utf-8",
        )
    return root


def _runtime(root):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.agent_fyi_path = root / "docs/AGENT_FYI.md"
    runtime.global_config = SimpleNamespace(instance_id="TEST")
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.backend_manager = SimpleNamespace(agent_mode="flex")
    runtime._pending_session_primer = None
    runtime._pending_session_primer_session_id = None
    runtime._pending_auto_recall_context = None
    return runtime


def test_heading_anchors_preserve_code_letters_digits_and_duplicate_sections():
    source = "# `x60 PCM*`\n\n## Same\n\nA\n\n## Same\n\nB\n"
    assert [item["anchor"] for item in headings(source)] == ["x60-pcm", "same", "same-1"]


@pytest.mark.parametrize("defect", ["placeholder", "empty_section", "guide_as_contract"])
def test_catalogue_requires_useful_navigation_and_actual_contracts(reference_repo, defect):
    root = reference_repo
    path = root / "docs/AGENT_FYI.md"
    guide = root / "docs/agent-guide/voice.md"
    if defect == "placeholder":
        text = path.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| voice |"))
        cells = row.split("|")
        cells[2] = " TODO "
        path.write_text(text.replace(row, "|".join(cells)), encoding="utf-8")
    elif defect == "empty_section":
        guide.write_text(guide.read_text(encoding="utf-8").replace(
            "## Ownership and layer\nPCM reference, Functions.", "## Ownership and layer"), encoding="utf-8")
    else:
        guide.write_text(guide.read_text(encoding="utf-8").replace(
            "../contract.md#conditions", "pcm.md"), encoding="utf-8")
    assert validate_catalogue(path, full=True)


def test_shipped_catalogue_navigation_migration_and_actual_bootstrap_budget():
    path = ROOT / "docs/AGENT_FYI.md"
    assert validate_catalogue(path, full=True) == []
    assert validate_knowledge_migration(ROOT) == []
    assert {domain.id for domain in catalogue_domains(load_agent_fyi_text(path))} >= MANDATORY_DOMAINS
    primer = build_agent_fyi_primer(path)
    assert CATALOGUE_MARKER in primer
    assert len(primer) <= BOOTSTRAP_CHAR_BUDGET
    assert primer.count(CATALOGUE_MARKER) == 1
    assert "[fyi trimmed]" not in primer
    # Comprehensive documents can exceed every bootstrap budget.
    assert any(path.stat().st_size > BOOTSTRAP_CHAR_BUDGET for path in (ROOT / "docs").glob("*.md"))


def test_refresh_rereads_validated_catalogue_and_common_guidance(reference_repo):
    path = reference_repo / "docs/AGENT_FYI.md"
    original = build_agent_fyi_primer(path)
    path.write_text(path.read_text(encoding="utf-8") + "\nA new navigation clarification.\n", encoding="utf-8")
    refreshed = build_agent_fyi_primer(path)
    assert "A new navigation clarification." in refreshed
    assert original.splitlines()[1] != refreshed.splitlines()[1]
    (reference_repo / "AGENTS.md").write_text("Updated engineering boundaries.", encoding="utf-8")
    assert build_agent_fyi_primer(path).splitlines()[1] != refreshed.splitlines()[1]


def test_bootstrap_never_reads_or_expands_linked_bodies(reference_repo, monkeypatch):
    opened = []
    original = Path.open

    def observe(path, *args, **kwargs):
        opened.append(path.resolve())
        assert path.name in {"AGENT_FYI.md", "AGENTS.md"}, "Detailed documents must be read on demand."
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    primer = build_agent_fyi_primer(reference_repo / "docs/AGENT_FYI.md")
    assert "DETAILED_GUIDE_BODY_NOT_FOR_BOOTSTRAP" not in primer
    assert opened.count(reference_repo / "docs/AGENT_FYI.md") == 1


def test_linked_document_has_own_revision_not_catalogue_revision(reference_repo):
    path = reference_repo / "docs/AGENT_FYI.md"
    before = build_agent_fyi_primer(path)
    first = read_documentation({"path": "docs/contract.md"}, code_root=reference_repo)
    (reference_repo / "docs/contract.md").write_text("# Contract\n\nDifferent complete source.\n", encoding="utf-8")
    after = read_documentation({"path": "docs/contract.md"}, code_root=reference_repo)
    assert before == build_agent_fyi_primer(path)
    assert first["source"]["sha256"] != after["source"]["sha256"]


@pytest.mark.parametrize("kind", ["missing", "malformed", "oversized", "guidance_oversized"])
def test_non_destructive_fallback_preserves_current_request(reference_repo, kind):
    path = reference_repo / "docs/AGENT_FYI.md"
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text("Invalid catalogue paragraph that must not be presented as complete.", encoding="utf-8")
    elif kind == "oversized":
        path.write_text(path.read_text(encoding="utf-8") + "oversized-input " * 3000, encoding="utf-8")
    else:
        (reference_repo / "AGENTS.md").write_text("oversized-guidance " * 3000, encoding="utf-8")
    before = {path: path.read_bytes() for path in reference_repo.rglob("*") if path.is_file()}
    runtime = _runtime(reference_repo)
    prompt = runtime._build_fyi_request_prompt("Explain the pending change; do not restart.")
    assert "Reference status: incomplete" in prompt
    assert "no partial reference" in prompt.casefold()
    assert "[fyi trimmed]" not in prompt
    assert prompt.endswith("--- CURRENT USER REQUEST — AUTHORITATIVE ---\nExplain the pending change; do not restart.")
    assert before == {path: path.read_bytes() for path in reference_repo.rglob("*") if path.is_file()}


def test_explicit_refresh_preserves_runtime_values_and_no_settings_write(reference_repo):
    runtime = _runtime(reference_repo)
    for name in ("agents.json", "agent.md", "permissions.json"):
        (reference_repo / name).write_text("unchanged fixture", encoding="utf-8")
    original = {name: (reference_repo / name).read_bytes() for name in ("agents.json", "agent.md", "permissions.json")}
    prompt = runtime._build_fyi_request_prompt("Explain the pending change")
    assert "working mode: flex" in prompt and "backend: codex-cli" in prompt
    assert prompt.endswith("--- CURRENT USER REQUEST — AUTHORITATIVE ---\nExplain the pending change")
    assert original == {name: (reference_repo / name).read_bytes() for name in original}


def test_session_primer_is_scoped_once_and_general_chat_does_not_reload(reference_repo, monkeypatch):
    runtime = _runtime(reference_repo)
    runtime._arm_session_primer("Handoff context.", session_id="target")
    other = SimpleNamespace(source="telegram", silent=False, session_id="other", prompt="Other user request")
    assert runtime._consume_session_primer(other) == other.prompt
    item = SimpleNamespace(source="telegram", silent=False, session_id="target", prompt="Current user request")
    consumed = runtime._consume_session_primer(item)
    assert consumed.count(CATALOGUE_MARKER) == 1 and consumed.endswith(item.prompt)
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: pytest.fail("Ordinary later turns must not reload documents"))
    assert runtime._consume_session_primer(item) == item.prompt


@pytest.mark.parametrize("source,silent", [("scheduler:test", False), ("bridge:handoff", False), ("telegram", True)])
def test_non_foreground_paths_do_not_consume_user_primer(reference_repo, source, silent):
    runtime = _runtime(reference_repo)
    runtime._arm_session_primer("Continuation.", session_id="session")
    saved = runtime._pending_session_primer
    item = SimpleNamespace(source=source, silent=silent, session_id="session", prompt="Background work")
    assert runtime._consume_session_primer(item) == item.prompt
    assert runtime._pending_session_primer == saved


def test_explicit_refresh_does_not_duplicate_an_armed_session_catalogue(reference_repo):
    runtime = _runtime(reference_repo)
    runtime._arm_session_primer("Backend handoff.", session_id="session")
    prompt = runtime._build_fyi_request_prompt("Current request remains authoritative")
    item = SimpleNamespace(source="telegram", silent=False, session_id="session", prompt=prompt)
    result = runtime._consume_session_primer(item)
    assert result.count(CATALOGUE_MARKER) == 1
    assert runtime._pending_session_primer is None


@pytest.mark.parametrize("defect", [
    "broken_file", "broken_anchor", "missing_guide", "duplicate_domain",
    "missing_domain", "missing_trigger", "historical_as_current", "automatic_cycle", "path_escape",
])
def test_catalogue_validator_reports_actionable_navigation_failures(reference_repo, defect):
    root = reference_repo
    path = root / "docs/AGENT_FYI.md"
    guide = root / "docs/agent-guide/voice.md"
    if defect == "broken_file":
        (root / "docs/contract.md").unlink()
    elif defect == "broken_anchor":
        guide.write_text(guide.read_text(encoding="utf-8").replace("#conditions", "#does-not-exist"), encoding="utf-8")
    elif defect == "missing_guide":
        guide.unlink()
    elif defect in {"duplicate_domain", "missing_domain", "missing_trigger"}:
        text = path.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| voice |"))
        if defect == "duplicate_domain":
            text = text.replace(row, row + "\n" + row)
        elif defect == "missing_domain":
            text = text.replace(row, "")
        else:
            cells = row.split("|")
            cells[3] = " "
            text = text.replace(row, "|".join(cells))
        path.write_text(text, encoding="utf-8")
    elif defect == "historical_as_current":
        (root / "docs/contract.md").write_text("# Old contract\n\nStatus: historical\n\n## Conditions\n\nOld evidence.", encoding="utf-8")
    elif defect == "automatic_cycle":
        guide.write_text(guide.read_text(encoding="utf-8") + "\n!include voice.md\n", encoding="utf-8")
    else:
        guide.write_text(guide.read_text(encoding="utf-8").replace("../contract.md#conditions", "../../secrets.md"), encoding="utf-8")
    assert validate_catalogue(path, full=True), defect


def test_migration_checks_actual_content_and_every_original_topic(tmp_path):
    source = ROOT / "docs/agent-guide/fyi-v1-migration.json"
    inventory = json.loads(source.read_text(encoding="utf-8"))
    assert len(inventory["records"]) == len(inventory["baseline"]["blocks"])
    for record in inventory["records"]:
        assert record["original_topic"] and record["classification"] and record["verification"]
    root = tmp_path / "copy"
    destination = root / "docs/agent-guide"
    destination.mkdir(parents=True)
    (destination / source.name).write_text(json.dumps(inventory), encoding="utf-8")
    # Existing filenames with empty/replaced content cannot satisfy migration.
    for record in inventory["records"]:
        path = root / record["destination"].split("#")[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Empty replacement\n", encoding="utf-8")
    assert validate_knowledge_migration(root)
    inventory["records"].pop()
    (destination / source.name).write_text(json.dumps(inventory), encoding="utf-8")
    assert "missing or duplicated" in "; ".join(validate_knowledge_migration(root))


def test_document_reference_survives_existing_compaction_evidence_contract(reference_repo):
    from orchestrator.context_compaction import (
        CAPSULE_FORMAT, CompactionFailure, _normalise_capsule, _turn_segment,
    )

    document = read_documentation({"path": "docs/contract.md", "section": "Conditions"}, code_root=reference_repo)
    reference = document["reference"]
    segment = _turn_segment({"id": 1, "role": "assistant", "text": json.dumps(document)})
    assert reference in segment.evidence_refs
    capsule = {
        "format": CAPSULE_FORMAT, "source_segment_ids": [segment.segment_id], "source_digest": segment.source_hash,
        "summary": "Continue source review.", "active_historical_goals": [], "decisions_and_constraints": [],
        "completed_work_and_verification": [], "unresolved_work_questions_failures": [],
        "evidence_refs": [], "preferences_and_definitions": [], "omissions_and_uncertainty": [],
    }
    validation = dict(source_ids=[segment.segment_id], source_digest=segment.source_hash,
                      required_evidence=segment.evidence_refs, max_chars=4000)
    with pytest.raises(CompactionFailure) as failure:
        _normalise_capsule(capsule, **validation)
    assert failure.value.code == "COMPACTION_EVIDENCE_MISSING"
    capsule["evidence_refs"] = [reference]
    retained = _normalise_capsule(capsule, **validation)
    reread = read_documentation({"reference": retained["evidence_refs"][0]}, code_root=reference_repo)
    assert reread["ok"] and reread["complete"]
    assert reread["source"] == document["source"]
    assert reread["text"] == document["text"]


@pytest.mark.parametrize("fields", [None, {"path": "../secrets.json", "section": "", "revision": "forged"}, {"root": "outside"}])
def test_saved_document_references_cannot_grant_filesystem_authority(reference_repo, fields):
    reference = "hashi-doc:!" if fields is None else "hashi-doc:" + base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")
    result = read_documentation({"reference": reference}, code_root=reference_repo)
    assert not result["ok"] and "text" not in result
    assert "fixture sentinel" not in json.dumps(result)


def test_saved_reference_detects_changed_source(reference_repo):
    first = read_documentation({"path": "docs/contract.md"}, code_root=reference_repo)
    (reference_repo / "docs/contract.md").write_text("# Different source\n", encoding="utf-8")
    changed = read_documentation({"reference": first["reference"]}, code_root=reference_repo)
    assert not changed["ok"] and changed["error"] == "revision_changed"


def test_relative_links_sections_and_revision_bound_continuation(reference_repo):
    contract = reference_repo / "docs/contract.md"
    complete = "# Contract\n\n## Conditions\n\n" + "\n\n".join(f"Condition {i}: full rule and exception." for i in range(100)) + "\n\n## Other\n\nUnrelated material.\n"
    contract.write_text(complete, encoding="utf-8")
    args = {"path": "../contract.md#conditions", "from_path": "docs/agent-guide/voice.md", "max_chars": 512}
    first = read_documentation(args, code_root=reference_repo)
    assert first["ok"] and first["source"]["path"] == "docs/contract.md"
    assert not first["complete"] and first["continuation"]["revision"] == first["source"]["sha256"]
    all_text, current = first["text"], first
    while not current["complete"]:
        # This small reference is sufficient after compaction or a backend handoff.
        saved_reference = json.loads(json.dumps(current["continuation"]))
        current = read_documentation(saved_reference, code_root=reference_repo)
        assert current["ok"]
        all_text += current["text"]
    assert all_text == complete[complete.index("## Conditions"):complete.index("## Other")]
    assert "Condition 99: full rule and exception." in all_text
    assert "Unrelated material." not in all_text
    contract.write_text(complete + "\nUpdated source.", encoding="utf-8")
    changed = read_documentation(first["continuation"], code_root=reference_repo)
    assert not changed["ok"] and changed["error"] == "revision_changed"


@pytest.mark.parametrize("reference", ["AGENTS.md", "docs/contract.md"])
def test_explicit_link_source_is_respected_for_root_looking_relative_names(reference_repo, reference):
    local = reference_repo / "docs/agent-guide" / reference
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# Linked local document\n", encoding="utf-8")
    result = read_documentation({
        "path": reference, "from_path": "docs/agent-guide/voice.md",
    }, code_root=reference_repo)
    assert result["ok"] and result["source"]["path"] == local.relative_to(reference_repo).as_posix()


@pytest.mark.parametrize("path", ["../secrets.json", "../../secrets.md", "/etc/passwd", "C:/secrets.md", "https://example.invalid/guide.md"])
def test_reader_cannot_escape_approved_documentation(reference_repo, path):
    result = read_documentation({"path": path}, code_root=reference_repo)
    assert result["ok"] is False
    assert "text" not in result


def test_reader_detects_symlink_escape_and_missing_documents(reference_repo, tmp_path):
    outside = tmp_path / "private.md"
    outside.write_text("private sentinel", encoding="utf-8")
    link = reference_repo / "docs/escape.md"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows may not grant symlink creation; missing references still fail.
        pass
    result = read_documentation({"path": "docs/escape.md"}, code_root=reference_repo)
    assert not result["ok"] and "private sentinel" not in json.dumps(result)
    missing = read_documentation({"path": "docs/absent.md"}, code_root=reference_repo)
    assert missing["error"] == "document_missing"


def test_outline_and_unavailable_anchor_are_explicit(reference_repo):
    outline = read_documentation({"operation": "outline", "path": "docs/contract.md"}, code_root=reference_repo)
    assert outline["ok"] and any(item["anchor"] == "conditions" for item in outline["sections"])
    missing = read_documentation({"path": "docs/contract.md#absent"}, code_root=reference_repo)
    assert missing["error"] == "anchor_missing" and "text" not in missing


@pytest.mark.asyncio
async def test_shared_registry_and_serialized_gateway_read_scoped_docs(reference_repo, tmp_path, monkeypatch):
    from tools.gateway.context import load_gateway_context, write_gateway_context
    from tools.gateway.mcp_stdio import ToolGateway, _dispatch
    from tools.registry import ToolRegistry

    workspace = tmp_path / "unrelated-workzone"
    workspace.mkdir()
    checkout = tmp_path / "mutable-checkout"
    checkout.mkdir()
    monkeypatch.setenv("HASHI_SOURCE_ROOT", str(checkout))
    monkeypatch.setenv("HASHI_FUNCTION_GENERATION_ROOT", str(reference_repo))
    registry = ToolRegistry(
        allowed_tools=["documentation_read"], access_root=workspace, workspace_dir=workspace, secrets={},
        audit_context={"agent_name": "fixture", "global_config": SimpleNamespace(project_root=checkout, instance_id="FIXTURE")},
    )
    assert registry.is_read_only("documentation_read")
    context_path = tmp_path / "gateway-context.json"
    write_gateway_context(registry, context_path)
    # CLI continuation can reconstruct tools without inheriting launcher env.
    monkeypatch.delenv("HASHI_SOURCE_ROOT")
    monkeypatch.delenv("HASHI_FUNCTION_GENERATION_ROOT")
    gateway = ToolGateway(load_gateway_context(context_path))
    listing = await _dispatch(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "documentation_read" in {item["name"] for item in listing["result"]["tools"]}
    result = await _dispatch(gateway, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "documentation_read", "arguments": {"path": "../contract.md#conditions", "from_path": "docs/agent-guide/voice.md"}},
    })
    payload = json.loads(result["result"]["content"][0]["text"])
    assert payload["ok"] and "complete condition" in payload["text"]
    restored = await _dispatch(gateway, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "documentation_read", "arguments": {"reference": payload["reference"]}},
    })
    assert json.loads(restored["result"]["content"][0]["text"])["source"] == payload["source"]
    invalid = await _dispatch(gateway, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "documentation_read", "arguments": {"root": str(tmp_path), "path": "private.md"}},
    })
    assert invalid["result"]["isError"] is True
    missing = await _dispatch(gateway, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "documentation_read", "arguments": {"path": "docs/missing.md"}},
    })
    assert missing["result"]["isError"] is True
    assert "document_missing" in missing["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_backend_without_documentation_capability_reports_limitation(reference_repo, tmp_path):
    from tools.documentation_read import execute_documentation_read
    from tools.registry import ToolRegistry

    missing = json.loads(await execute_documentation_read({}, global_config=None))
    assert missing["error"] == "documentation_unavailable"
    registry = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    assert not registry.is_allowed("documentation_read")
    result = await registry.execute("documentation_read", {"path": "docs/AGENT_FYI.md"})
    assert result.is_error


def test_deployed_artifact_contains_readable_docs_and_excludes_untracked_files(reference_repo, tmp_path):
    from orchestrator.function_generation import (
        CandidateProbeReceipt, VerifiedFunctionGeneration, build_source_manifest, verify_manifest_source_commit,
    )
    from orchestrator.function_worker_supervisor import materialize_generation_artifact
    from orchestrator.runtime_contract import enforce_runtime_contract

    root = reference_repo
    package = root / "orchestrator"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools/__init__.py").write_text("", encoding="utf-8")
    for relative in ("orchestrator/agent_documentation.py", "orchestrator/agent_fyi.py", "tools/documentation_read.py"):
        (root / relative).write_bytes((ROOT / relative).read_bytes())
    def git(*arguments):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *arguments],
            cwd=root, stderr=subprocess.STDOUT,
        ).decode().strip()
    git("init", "-q")
    git("add", "docs", "AGENTS.md", "ARCHITECTURE.md", "orchestrator", "tools")
    git("commit", "-qm", "fixture")
    (root / "docs/untracked.md").write_text("unpublished", encoding="utf-8")
    manifest = build_source_manifest([
        "orchestrator.fixture", "orchestrator.agent_documentation", "orchestrator.agent_fyi", "tools.documentation_read",
    ], code_root=root)
    assets = {item.relative_path for item in manifest.assets}
    assert {"docs/AGENT_FYI.md", "AGENTS.md", "ARCHITECTURE.md", "docs/agent-guide/voice.md", "docs/contract.md"} <= assets
    assert "docs/untracked.md" not in assets
    receipt = CandidateProbeReceipt(
        manifest.generation_id, manifest.module_names, enforce_runtime_contract(ROOT), os.getpid(),
        source_commit=verify_manifest_source_commit(manifest, code_root=root),
    )
    artifact = materialize_generation_artifact(tmp_path / "isolated-instance", VerifiedFunctionGeneration(root, manifest, receipt))
    result = read_documentation(
        {"path": "../contract.md#conditions", "from_path": "docs/agent-guide/voice.md"}, code_root=artifact,
    )
    assert result["ok"] and result["complete"]
    assert result["source"]["sha256"] == hashlib.sha256((root / "docs/contract.md").read_bytes()).hexdigest()
    # A fresh Function process must use its installed documentation, even after
    # the mutable development checkout moves ahead. No worker or service starts.
    (root / "docs/contract.md").write_text("# Unpublished source\n", encoding="utf-8")
    child = (
        "import asyncio,json,sys;from types import SimpleNamespace;"
        f"sys.path.insert(0,{str(artifact)!r});"
        "from tools.documentation_read import execute_documentation_read;"
        f"print(asyncio.run(execute_documentation_read({{'path':'docs/contract.md'}}, "
        f"global_config=SimpleNamespace(project_root={str(root)!r}))))"
    )
    output = subprocess.check_output(
        [sys.executable, "-I", "-c", child], cwd=tmp_path,
        env={**os.environ, "HASHI_FUNCTION_GENERATION_ROOT": str(artifact), "HASHI_SOURCE_ROOT": str(root)},
    )
    installed = json.loads(output)
    assert installed["ok"] and "A complete condition and its exception." in installed["text"]
    assert installed["source"]["sha256"] == result["source"]["sha256"]


@pytest.mark.parametrize("domain,destination,section", [
    ("voice", "HASHI_PHONE_CONTEXT_HANDOFF.md", "Freeze before action admission"),
    ("lifecycle", "HASHI_AGENT_MOVE_V1.md", "Transaction"),
    ("pcm", "HASHI_PCM_SYSTEM_DESIGN.md", "7. PCM Authority and Conflict Resolution"),
])
def test_representative_tasks_follow_actual_navigation(domain, destination, section):
    catalogue = read_documentation({"path": "docs/AGENT_FYI.md", "max_chars": 8000}, code_root=ROOT)
    entry = next(item for item in catalogue_domains(catalogue["text"]) if item.id == domain)
    guide = read_documentation({"path": entry.guide}, code_root=ROOT)
    assert guide["ok"] and guide["complete"]
    assert destination in guide["text"]
    contract = read_documentation({
        "path": "../" + destination, "from_path": guide["source"]["path"], "section": section, "max_chars": 8000,
    }, code_root=ROOT)
    assert contract["ok"] and contract["source"]["path"] == "docs/" + destination
    assert contract["complete"]


def test_phone_translation_navigation_exposes_the_actual_compute_contract_boundary():
    phone = read_documentation({
        "path": "docs/HASHI_PHONE_CONTEXT_HANDOFF.md", "section": "Translation discovery",
    }, code_root=ROOT)
    assert phone["ok"] and phone["complete"]
    links = re.findall(r"\]\(([^)]+)\)", phone["text"])
    assert "HASHI_PHONE_TRANSLATION_BOUNDARY.md" in links
    boundary = read_documentation({
        "path": links[0], "from_path": phone["source"]["path"],
    }, code_root=ROOT)
    assert boundary["ok"] and boundary["complete"]
    compute_link = "HASHI_COMPUTE_SERVICE_DIRECTORY.md#compatibility-and-truthfulness"
    assert compute_link in re.findall(r"\]\(([^)]+)\)", boundary["text"])
    compute = read_documentation({"path": compute_link, "from_path": boundary["source"]["path"]}, code_root=ROOT)
    assert compute["ok"] and compute["complete"]
    assert compute["source"]["path"] == "docs/HASHI_COMPUTE_SERVICE_DIRECTORY.md"
# HASHI-Fingerprint: hfp_f5928d78428641188f1bddad9023bffc
