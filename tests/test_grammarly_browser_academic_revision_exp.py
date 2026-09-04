from __future__ import annotations

import json
from pathlib import Path

from exp.loader import ExpStore

EXP_ID = "barry/grammarly_browser_academic_revision"


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_grammarly_revision_exp_is_discoverable_and_complete() -> None:
    store = ExpStore()

    assert EXP_ID in store.list_ids()
    entry = store.get(EXP_ID)
    manifest = entry.manifest

    assert manifest["type"] == "exp"
    assert manifest["status"] == "candidate"
    assert manifest["context"]["user"] == "Barry"
    assert manifest["playbooks"] == {
        "browser_local_revision": "playbooks/browser_local_revision.exp.md",
        "train_and_promote": "playbooks/train_and_promote.exp.md",
    }

    referenced_files = [
        manifest["configuration"],
        manifest["failure_memory"],
        manifest["knowledge_base"],
        *manifest["playbooks"].values(),
        *manifest["validators"],
        *manifest["templates"].values(),
        *manifest["training_runs"].values(),
    ]
    for relative_path in referenced_files:
        assert (entry.root / relative_path).is_file(), relative_path


def test_grammarly_revision_policy_keeps_safety_and_adjustability_explicit() -> None:
    entry = ExpStore().get(EXP_ID)
    policy = _read_json(entry.root / entry.manifest["configuration"])

    assert policy["rounds"]["default_correction_limit"] == 3
    assert policy["rounds"]["user_adjustable"] is True
    assert policy["batching"]["target_min_words"] == 800
    assert policy["batching"]["target_max_words"] == 1000
    assert policy["external_review"]["requires_explicit_user_approval"] is True
    assert policy["external_review"]["requires_positive_upload_allowlist"] is True
    assert policy["browser"]["whole_batch_replacement_requires_disposable_preflight"] is True
    assert policy["browser"]["paired_document_fallback_allowed_without_run_amendment"] is False
    assert policy["word"]["source_is_immutable"] is True
    assert policy["promotion_defaults"]["stable_requires_user_confirmation"] is True


def test_grammarly_revision_failure_memory_is_valid_and_unique() -> None:
    entry = ExpStore().get(EXP_ID)
    failure_path = entry.root / entry.manifest["failure_memory"]
    records = [
        json.loads(line)
        for line in failure_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(records) >= 8
    ids = [record["id"] for record in records]
    assert len(ids) == len(set(ids))
    assert all(record["status"] == "active" for record in records)
    assert all(record["symptom"] for record in records)
    assert all(record["recovery"] for record in records)
    assert all(record["evidence"] for record in records)


def test_first_training_run_cannot_be_misreported_as_stable() -> None:
    entry = ExpStore().get(EXP_ID)
    run_path = entry.root / entry.manifest["training_runs"]["paper4_batch001_poc_001"]
    report = _read_json(run_path.parent / "state" / "validation_report.json")
    checks = {check["id"]: check["status"] for check in report["checks"]}

    assert report["overall_status"] == "candidate_evidence_only"
    assert report["completed_batch"] is False
    assert report["rewrite_efficacy_claim_allowed"] is False
    assert checks["batch_001_r0_browser_integrity"] == "passed"
    assert checks["same_document_r1_reassessment"] == "not_run"
    assert checks["word_writeback_and_reopen"] == "not_run"


def test_playbook_requires_local_revision_and_exact_round_comparison() -> None:
    playbook = ExpStore().get_playbook(EXP_ID, "browser_local_revision")

    assert "The browser detects and displays evidence" in playbook
    assert "Revise locally" in playbook
    assert "same active Grammarly document" in playbook
    assert "A lower batch percentage alone does not establish" in playbook
    assert "Never overwrite the selected source DOCX" in playbook
