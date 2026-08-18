from __future__ import annotations

import json

import pytest

from adapters import her_dream
from adapters.her_dream import (
    DreamUndoConflict,
    DreamValidationError,
    HERDreamJournal,
    StaleDreamState,
    build_dream_correction_prompt,
    build_dream_prompt,
    catalog_fingerprint,
    commit_dream_proposal,
    latest_undoable_run,
    parse_dream_proposal,
    recover_interrupted_runs,
    undo_dream_run,
)
from adapters.her_habits import HERHabitStore


def _create_habit(
    store: HERHabitStore,
    title: str,
    *,
    metadata: str | None = None,
    body: str | None = None,
) -> str:
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": title,
                "metadata": metadata or f"Use when {title.casefold()} applies.",
                "body": body or f"Follow the current behaviour for {title}.",
            }
        ],
        max_actions=1,
    )
    assert outcome.startswith("created:")
    return outcome.split(":", 1)[1]


def _begin(journal: HERDreamJournal, store: HERHabitStore, run_id: str) -> str:
    fingerprint = catalog_fingerprint(store.load())
    journal.begin_run(
        run_id=run_id,
        origin="manual",
        before_fingerprint=fingerprint,
        habit_count=len(store.load()),
        transcript_cursor={"offset": 42},
    )
    return fingerprint


def test_dream_prompt_is_her_only_closed_and_redacts_secret_like_authority(tmp_path):
    store = HERHabitStore(tmp_path)
    _create_habit(store, "Verify exact output")

    prompt = build_dream_prompt(
        agent_name="zelda",
        habits=store.load(),
        agent_guidance="Use a warm persona. api_key=secret-value-123456",
        sys_guidance=["Never edit files without permission."],
        recent_user_requests=[
            {"ts": "2026-08-15", "source": "text", "text": "Keep reports concise."}
        ],
    )

    assert "HER HABIT DREAM" in prompt
    assert '"groups"' in prompt
    assert "bridge_memory.sqlite" not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert "secret-value-123456" not in prompt
    assert "justification <= 500 characters" in prompt
    assert "aim for <=\n400" in prompt


def test_dream_correction_prompt_returns_the_validation_error_to_the_model():
    prompt = build_dream_correction_prompt(
        rejected_output='{"groups":[{"reason":"too long"}]}',
        error=DreamValidationError("groups[0].reason exceeds 500 characters"),
    )

    assert "groups[0].reason exceeds 500 characters" in prompt
    assert '"reason":"too long"' in prompt
    assert "Change only what is needed" in prompt


def test_dream_parser_accepts_closed_operations_and_blocks_protected_mutation(tmp_path):
    store = HERHabitStore(tmp_path)
    rewrite_id = _create_habit(store, "Check current timeout")
    protected_id = _create_habit(store, "Preserve user approval")
    store.set_protected(protected_id, True)
    habits = store.load()

    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "rewrite",
                        "habit_id": rewrite_id,
                        "title": "Check timeout state",
                        "metadata": "Use when a task stops at a timeout boundary.",
                        "body": "Verify whether work continues before deciding to retry.",
                        "reason": "The current wording does not distinguish stopped and continuing work.",
                    },
                    {
                        "operation": "protected_conflict",
                        "habit_id": protected_id,
                        "reason": "Recent explicit guidance conflicts with this protected instruction.",
                    },
                ]
            }
        ),
        habits=habits,
    )
    assert [group["operation"] for group in groups] == [
        "rewrite",
        "protected_conflict",
    ]

    protected_rewrite = {
        "groups": [
            {
                "operation": "rewrite",
                "habit_id": protected_id,
                "title": "Change protected Habit",
                "metadata": "This must be rejected.",
                "body": "Do not permit this automatic mutation.",
                "reason": "Attempted automatic change.",
            }
        ]
    }
    with pytest.raises(DreamValidationError, match="protected"):
        parse_dream_proposal(json.dumps(protected_rewrite), habits=habits)

    with pytest.raises(DreamValidationError, match="closed schema"):
        parse_dream_proposal(
            json.dumps(
                {
                    "groups": [
                        {
                            **protected_rewrite["groups"][0],
                            "unexpected": True,
                        }
                    ]
                }
            ),
            habits=habits,
        )


def test_dream_commit_journals_combine_rewrite_archive_and_protected_conflict(
    tmp_path,
):
    store = HERHabitStore(tmp_path)
    first = _create_habit(store, "Retry stalled upload")
    second = _create_habit(store, "Resume stalled upload")
    rewrite = _create_habit(store, "Check old timeout wording")
    archive = _create_habit(store, "Use obsolete endpoint")
    protected = _create_habit(store, "Keep explicit approval")
    store.set_protected(protected, True)
    original = {habit.habit_id: habit.to_payload() for habit in store.load()}
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    fingerprint = _begin(journal, store, run_id)

    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "combine",
                        "habit_ids": [first, second],
                        "canonical_id": first,
                        "title": "Resume stalled uploads safely",
                        "metadata": "Use when an upload appears stalled but may still be active.",
                        "body": "Check live process and remote state before resuming or retrying.",
                        "reason": "Both Habits describe the same trigger, action, and exception.",
                    },
                    {
                        "operation": "rewrite",
                        "habit_id": rewrite,
                        "title": "Verify timeout outcome",
                        "metadata": "Use when a foreground or background task reaches a timeout.",
                        "body": "Check whether execution stopped or continues before choosing the next action.",
                        "reason": "The old wording treated every timeout as a stopped process.",
                    },
                    {
                        "operation": "archive",
                        "habit_id": archive,
                        "reason": "The endpoint was explicitly retired by current guidance.",
                    },
                    {
                        "operation": "protected_conflict",
                        "habit_id": protected,
                        "reason": "Current guidance conflicts, but the user-controlled lock must be preserved.",
                    },
                ]
            }
        ),
        habits=store.load(),
    )
    journal.record_attempt(
        run_id,
        attempt=1,
        input_fingerprint=fingerprint,
        raw_output=json.dumps({"groups": groups}),
        validation={"valid": True, "groups": groups},
    )

    manifest = commit_dream_proposal(
        store=store,
        journal=journal,
        run_id=run_id,
        expected_fingerprint=fingerprint,
        groups=groups,
    )

    assert manifest["status"] == "completed"
    assert manifest["changed_group_numbers"] == [1, 2, 3]
    assert len(manifest["report_facts"]) == 4
    assert store.get(second) is None
    assert store.get(archive) is None
    assert store.get(first).title == "Resume stalled uploads safely"
    assert store.get(rewrite).title == "Verify timeout outcome"
    assert store.get(protected).to_payload() == original[protected]
    assert journal._snapshot_path(run_id).is_file()
    assert list(journal.raw_root.glob(f"{run_id}-attempt-1.txt"))
    assert list(journal.validation_root.glob(f"{run_id}-attempt-1.json"))


def test_dream_stale_fingerprint_applies_nothing(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _create_habit(store, "Inspect live state")
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    fingerprint = _begin(journal, store, run_id)
    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "archive",
                        "habit_id": habit_id,
                        "reason": "The instruction is now obsolete.",
                    }
                ]
            }
        ),
        habits=store.load(),
    )
    store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": habit_id,
                "body": "This changed while Dream was analysing.",
            }
        ],
        max_actions=1,
    )

    with pytest.raises(StaleDreamState):
        commit_dream_proposal(
            store=store,
            journal=journal,
            run_id=run_id,
            expected_fingerprint=fingerprint,
            groups=groups,
        )
    assert store.get(habit_id) is not None
    assert journal.get_run(run_id)["status"] == "analyzing"


def test_dream_commit_failure_restores_entire_before_catalogue(tmp_path, monkeypatch):
    store = HERHabitStore(tmp_path)
    first = _create_habit(store, "First rewrite target")
    second = _create_habit(store, "Second rewrite target")
    before = catalog_fingerprint(store.load())
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    _begin(journal, store, run_id)
    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "rewrite",
                        "habit_id": first,
                        "title": "First canonical target",
                        "metadata": "Use for the first canonical target.",
                        "body": "Apply the first canonical behaviour.",
                        "reason": "Remove obsolete wording from the first Habit.",
                    },
                    {
                        "operation": "rewrite",
                        "habit_id": second,
                        "title": "Second canonical target",
                        "metadata": "Use for the second canonical target.",
                        "body": "Apply the second canonical behaviour.",
                        "reason": "Remove obsolete wording from the second Habit.",
                    },
                ]
            }
        ),
        habits=store.load(),
    )
    original_apply = store.apply_actions_with_changes
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(store, "apply_actions_with_changes", fail_second)

    with pytest.raises(OSError, match="simulated"):
        commit_dream_proposal(
            store=store,
            journal=journal,
            run_id=run_id,
            expected_fingerprint=before,
            groups=groups,
        )

    assert catalog_fingerprint(store.load()) == before
    assert journal.get_run(run_id)["status"] == "failed_rolled_back"


def test_partial_then_full_undo_restores_original_catalogue_and_refuses_stale(tmp_path):
    store = HERHabitStore(tmp_path)
    rewrite = _create_habit(store, "Rewrite undo target")
    archive = _create_habit(store, "Archive undo target")
    original_fingerprint = catalog_fingerprint(store.load())
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    before = _begin(journal, store, run_id)
    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "rewrite",
                        "habit_id": rewrite,
                        "title": "Canonical undo target",
                        "metadata": "Use when testing partial Dream undo.",
                        "body": "Restore this exact record when the change is undone.",
                        "reason": "The old wording was unnecessarily long.",
                    },
                    {
                        "operation": "archive",
                        "habit_id": archive,
                        "reason": "The instruction was obsolete for the current workflow.",
                    },
                ]
            }
        ),
        habits=store.load(),
    )
    commit_dream_proposal(
        store=store,
        journal=journal,
        run_id=run_id,
        expected_fingerprint=before,
        groups=groups,
    )

    partial = undo_dream_run(
        store=store,
        journal=journal,
        run_id=run_id,
        group_number=1,
    )
    assert partial["group_numbers"] == [1]
    assert store.get(rewrite).title == "Rewrite undo target"
    assert store.get(archive) is None
    assert latest_undoable_run(journal)["run_id"] == run_id

    remaining = undo_dream_run(store=store, journal=journal, run_id=run_id)
    assert remaining["group_numbers"] == [2]
    assert catalog_fingerprint(store.load()) == original_fingerprint
    assert latest_undoable_run(journal) is None

    with pytest.raises(DreamUndoConflict):
        undo_dream_run(store=store, journal=journal, run_id=run_id, group_number=1)


def test_undo_refuses_to_overwrite_post_dream_change(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _create_habit(store, "Stale undo target")
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    before = _begin(journal, store, run_id)
    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "rewrite",
                        "habit_id": habit_id,
                        "title": "Dream rewrite target",
                        "metadata": "Use after the Dream rewrite.",
                        "body": "This is the committed Dream behaviour.",
                        "reason": "The old rule was contradicted by current guidance.",
                    }
                ]
            }
        ),
        habits=store.load(),
    )
    commit_dream_proposal(
        store=store,
        journal=journal,
        run_id=run_id,
        expected_fingerprint=before,
        groups=groups,
    )
    store.apply_actions(
        [
            {
                "operation": "update",
                "habit_id": habit_id,
                "body": "A newer manual or Meditation change exists.",
            }
        ],
        max_actions=1,
    )

    with pytest.raises(DreamUndoConflict, match="changed after Dream"):
        undo_dream_run(store=store, journal=journal, run_id=run_id)
    assert store.get(habit_id).body == "A newer manual or Meditation change exists."


def test_restart_recovery_rolls_back_applying_manifest(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _create_habit(store, "Recover interrupted Dream")
    original = catalog_fingerprint(store.load())
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    _begin(journal, store, run_id)
    journal.write_snapshot(run_id=run_id, habits=store.load())
    manifest = journal.get_run(run_id)
    manifest["status"] = "applying"
    her_dream._atomic_write_json(journal._run_path(run_id), manifest)
    store.apply_actions(
        [{"operation": "delete", "habit_id": habit_id}],
        max_actions=1,
    )

    assert recover_interrupted_runs(store=store, journal=journal) == 1
    assert catalog_fingerprint(store.load()) == original
    assert journal.get_run(run_id)["status"] == "recovered_rolled_back"


def test_restart_recovery_rolls_back_interrupted_undo(tmp_path):
    store = HERHabitStore(tmp_path)
    habit_id = _create_habit(store, "Recover interrupted undo")
    journal = HERDreamJournal(tmp_path)
    run_id = journal.new_run_id()
    before = _begin(journal, store, run_id)
    groups = parse_dream_proposal(
        json.dumps(
            {
                "groups": [
                    {
                        "operation": "rewrite",
                        "habit_id": habit_id,
                        "title": "Committed Dream wording",
                        "metadata": "Use when recovering an interrupted Dream undo.",
                        "body": "Keep the committed Dream state after rollback recovery.",
                        "reason": "The previous wording was needlessly verbose.",
                    }
                ]
            }
        ),
        habits=store.load(),
    )
    manifest = commit_dream_proposal(
        store=store,
        journal=journal,
        run_id=run_id,
        expected_fingerprint=before,
        groups=groups,
    )
    post_dream = [habit.to_payload() for habit in store.load()]
    undo_id = "U-20260815-023000-ABC123"
    before_path = journal.undo_root / f"{undo_id}-before.json"
    her_dream._atomic_write_json(
        before_path,
        {
            "format": her_dream.DREAM_SNAPSHOT_FORMAT,
            "run_id": run_id,
            "undo_id": undo_id,
            "habits": post_dream,
        },
    )
    undo = {
        "format": her_dream.DREAM_UNDO_FORMAT,
        "undo_id": undo_id,
        "run_id": run_id,
        "status": "applying",
        "before_snapshot_path": str(before_path.relative_to(journal.root)),
        "run_manifest_before": manifest,
    }
    undo_path = journal.undo_root / f"{undo_id}.json"
    her_dream._atomic_write_json(undo_path, undo)

    # Simulate a process dying after catalog/run-manifest mutation but before
    # the durable undo transaction could be marked complete.
    original = journal.read_snapshot(run_id)
    her_dream._restore_catalog(store, original)
    interrupted_manifest = dict(manifest)
    interrupted_manifest["status"] = "undone"
    interrupted_manifest["undone_groups"] = [1]
    her_dream._atomic_write_json(journal._run_path(run_id), interrupted_manifest)

    assert recover_interrupted_runs(store=store, journal=journal) == 1
    assert store.get(habit_id).title == "Committed Dream wording"
    assert journal.get_run(run_id)["status"] == "completed"
    recovered = json.loads(undo_path.read_text(encoding="utf-8"))
    assert recovered["status"] == "recovered_rolled_back"
