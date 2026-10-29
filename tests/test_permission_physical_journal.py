"""Crash reconciliation uses a stopped fixed helper and original private result.

The injected process observer tests the recovery contract; these are not native
Windows isolation evidence and never launch a process.
"""
from dataclasses import replace
from hashlib import sha256
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.permissions.authority import OperationConflict, PermissionAuthority
from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.models import normalize_scope
from orchestrator.permissions.physical_journal import PhysicalFileJournal
from orchestrator.permissions.receipt_issuer import SupervisorReceiptIssuer
from orchestrator.permissions.result_store import EncryptedResultStore
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from orchestrator.permissions.windows_process import ProcessObservation
from tests.test_permission_pao_fence import setup_run


def interrupted(tmp_path, *, raw_result=True, prepare=True):
    _, _, _, _, context = setup_run(tmp_path)
    key = Ed25519PrivateKey.generate()
    issuer = SupervisorReceiptIssuer(key_id="fixedhelper", signing_key=key)
    context = replace(context, executor_id=issuer.key_id)
    verifier = SignedReceiptVerifier(
        {"approval": Ed25519PrivateKey.generate().public_key().public_bytes_raw()},
        trusted_supervisor_keys={issuer.key_id: issuer.public_key},
    )
    scope = normalize_scope("file", str(tmp_path), ("create", "write"), cwd=tmp_path)
    old = PermissionAuthority(tmp_path / "authority.sqlite", verifier=verifier, baseline_resolver=lambda ctx: (scope,))
    old.open_task(context)
    results = EncryptedResultStore(tmp_path / "results.sqlite", key=bytes(range(32)))
    root = tmp_path / "private"
    folder = root / "file-owned"
    folder.mkdir(parents=True)
    plan_path, result_path = folder / "plan.json", folder / "result.json"
    plan = {"format": "hashi-file-helper-v1", "action": "create", "handles": [123],
            "directory": str(tmp_path), "name": "report.txt", "data": "c2F2ZWQ=", "result": str(result_path)}
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    arguments = {"path": str(tmp_path / "report.txt"), "content": "saved"}
    descriptor = describe_tool("file_write", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    observed = ProcessObservation(12345, "S-1-5-21-1", None, 0, False, False, True, "e" * 64, 1234567)
    journal = PhysicalFileJournal(
        tmp_path / "physical.sqlite", artifact_root=root, authority=old, results=results,
        issuer=issuer, observe_stopped=lambda observation: True,
    )
    claim_id = "a" * 32
    journal.record(
        context=context, operation_id="original-operation", descriptor=descriptor, arguments=arguments,
        cwd=tmp_path, claim_id=claim_id, observation=observed, plan_path=plan_path,
        plan_digest=sha256(plan_path.read_bytes()).hexdigest(), max_processes=1,
    )
    if prepare:
        old.prepare(context, "original-operation", descriptor.arguments_digest, descriptor.scopes,
                    prepared_claim_id=claim_id)
        old.start(context, claim_id)
    if raw_result:
        result_path.write_text(json.dumps({"outcome": "succeeded", "data": ""}), encoding="utf-8")
    current = PermissionAuthority(tmp_path / "authority.sqlite", verifier=verifier,
                                  baseline_resolver=lambda ctx: (scope,))
    issuer._clock = current.observed_time
    recovered = PhysicalFileJournal(
        tmp_path / "physical.sqlite", artifact_root=root, authority=current, results=results,
        issuer=issuer, observe_stopped=lambda observation: True,
    )
    return current, context, claim_id, recovered, results, descriptor


def test_completed_private_result_survives_service_crash_without_reexecuting_a_write(tmp_path):
    authority, context, claim, journal, results, descriptor = interrupted(tmp_path)
    assert authority.operation_state("original-operation") == "unknown"
    assert not (tmp_path / "report.txt").exists()
    journal.reconcile()
    assert authority.operation_state("original-operation") == "succeeded"
    assert authority.resource_state(claim) == "stopped"
    cached = results.get(context, "original-operation", descriptor)
    assert "wrote 5 characters" in cached.payload
    assert not (tmp_path / "report.txt").exists()  # Recovery did not issue any business write.
    assert not journal.pending()
    journal.reconcile()
    assert results.get(context, "original-operation", descriptor) == cached


def test_missing_result_stops_resources_but_keeps_business_outcome_unknown(tmp_path):
    authority, context, claim, journal, _, _ = interrupted(tmp_path, raw_result=False)
    journal.reconcile()
    assert authority.resource_state(claim) == "stopped"
    assert authority.operation_state("original-operation") == "unknown"
    assert authority.execution_status(context).task_state == "recovering"
    assert not (tmp_path / "report.txt").exists()


def test_live_original_process_prevents_result_import_and_resource_release(tmp_path):
    authority, _, claim, journal, _, _ = interrupted(tmp_path)
    journal._observe_stopped = lambda observation: False
    journal.reconcile()
    assert authority.resource_state(claim) != "stopped"
    assert authority.operation_state("original-operation") == "unknown"
    assert len(journal.pending()) == 1
    journal._observe_stopped = lambda observation: True
    journal.reconcile()
    assert authority.operation_state("original-operation") == "succeeded"


def test_journal_before_prepare_does_not_invent_an_execution_claim(tmp_path):
    authority, _, claim, journal, _, _ = interrupted(tmp_path, prepare=False, raw_result=False)
    journal.reconcile()
    assert authority.resource_state(claim) is None
    assert not journal.pending()


def test_damaged_physical_evidence_cannot_release_a_claim(tmp_path):
    authority, _, claim, journal, _, _ = interrupted(tmp_path)
    with journal._connection() as connection:
        connection.execute("UPDATE physical_files SET digest=?", ("0" * 64,))
    with pytest.raises(ValueError, match="damaged"):
        journal.reconcile()
    assert authority.resource_state(claim) != "stopped"
    assert authority.operation_state("original-operation") == "unknown"


def test_a_preassigned_claim_cannot_be_rebound_to_another_operation(tmp_path):
    authority, context, claim, _, _, descriptor = interrupted(tmp_path)
    # A fresh live binding is required before any further ordinary operation.
    new_context = replace(context, task_id="another-task", execution_epoch="another-epoch")
    authority.open_task(new_context)
    with pytest.raises(OperationConflict, match="already bound"):
        authority.prepare(
            new_context, "different-operation", descriptor.arguments_digest, descriptor.scopes,
            prepared_claim_id=claim,
        )
