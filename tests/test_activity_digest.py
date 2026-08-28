from __future__ import annotations

from adapters.stream_events import (
    KIND_FILE_EDIT,
    KIND_PROGRESS,
    KIND_REVIEW,
    KIND_SHELL_EXEC,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamEvent,
)
from orchestrator.activity_digest import ActivityDigest


def test_digest_aggregates_codex_activity_without_exposing_raw_commands() -> None:
    digest = ActivityDigest(started_at=10.0)

    digest.record(
        StreamEvent(
            kind=KIND_SHELL_EXEC,
            summary='Running: /bin/bash -lc \'rg -n "pytest" orchestrator\'',
            tool_name="Bash",
            metadata={
                "command": '/bin/bash -lc \'rg -n "pytest" orchestrator\''
            },
        ),
        now=11.0,
    )
    digest.record(
        StreamEvent(
            kind=KIND_TOOL_END,
            summary="Command exited (0)",
            tool_name="Bash",
            metadata={"exit_code": 0},
        ),
        now=12.0,
    )
    digest.record(
        StreamEvent(
            kind=KIND_FILE_EDIT,
            summary="Edited 2 files",
            file_path="orchestrator/a.py",
            metadata={"file_paths": ["orchestrator/a.py", "tests/test_a.py"]},
        ),
        now=13.0,
    )
    digest.record(
        StreamEvent(
            kind=KIND_SHELL_EXEC,
            summary="Running: pytest -q tests/test_a.py",
            tool_name="Bash",
            metadata={"command": "pytest -q tests/test_a.py"},
        ),
        now=14.0,
    )
    digest.record(
        StreamEvent(
            kind=KIND_TOOL_END,
            summary="Command exited (0)",
            tool_name="Bash",
            metadata={"exit_code": 0},
        ),
        now=15.0,
    )

    assert digest.phase_label == "Execution"
    assert digest.render_lines() == [
        "🔎 Performed 1 inspection · 1 search",
        "📝 Changed 2 files",
        "🧪 Ran 1 check · completed ✅",
    ]
    assert all("pytest -q" not in line for line in digest.render_lines())


def test_digest_tracks_lifecycle_review_replan_and_completion() -> None:
    digest = ActivityDigest()

    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Planning started",
            phase="planning",
            event_id="stage:planning",
            metadata={
                "activity_type": "stage",
                "status": "started",
                "stage": "planning",
            },
        )
    )
    assert (digest.phase_icon, digest.phase_label) == ("🧭", "Planning")

    digest.record(
        StreamEvent(
            kind=KIND_REVIEW,
            summary="Review fail",
            phase="review",
            event_id="review:1",
            metadata={
                "activity_type": "review_result",
                "outcome": "FAIL",
                "finding_count": 2,
            },
        )
    )
    assert digest.phase_label == "Review"
    assert "❌ Review found 2 issues" in digest.render_lines()

    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Replanning started",
            phase="replanning",
            event_id="stage:replanning",
            metadata={
                "activity_type": "stage",
                "status": "started",
                "stage": "replanning",
            },
        )
    )
    assert (digest.phase_icon, digest.phase_label) == ("🔄", "Replanning")

    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Task completed",
            phase="COMPLETED",
            event_id="terminal:completed",
            metadata={
                "activity_type": "lifecycle",
                "status": "completed",
                "lifecycle_state": "COMPLETED",
                "terminal": True,
            },
        )
    )
    assert digest.finished is True
    assert (digest.phase_icon, digest.phase_label) == ("✅", "Completed")


def test_digest_ignores_fragmented_tool_json_and_deduplicates_event_ids() -> None:
    digest = ActivityDigest()
    fragment = StreamEvent(
        kind=KIND_TOOL_START,
        summary='... {"file_path":',
        event_id="fragment-1",
    )
    assert digest.record(fragment) is False
    assert digest.record(fragment) is False
    assert digest.render_lines() == ["⏳ Preparing the next step"]


def test_digest_reports_recovery_and_waiting_as_separate_facts() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="provider recovery retry scheduled",
            event_id="retry-1",
            phase="execution",
            metadata={"activity_type": "recovery"},
        )
    )
    digest.mark_waiting()

    assert digest.phase_label == "Execution"
    assert digest.render_lines() == [
        "🔁 Performed 1 recovery action",
        "⏳ Waiting for a long-running operation",
    ]


def test_digest_uses_typed_recovery_without_parsing_its_prose() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Internal structured response step",
            event_id="repair-1",
            metadata={
                "activity_type": "recovery",
                "status": "started",
            },
        )
    )

    assert digest.render_lines() == ["🔁 Performed 1 recovery action"]


def test_digest_maps_common_backend_tools_to_stable_categories() -> None:
    digest = ActivityDigest()
    events = [
        StreamEvent(kind=KIND_TOOL_START, summary="Read", tool_name="Read"),
        StreamEvent(kind=KIND_TOOL_START, summary="Edit", tool_name="Edit"),
        StreamEvent(
            kind=KIND_TOOL_START,
            summary="verification_run",
            tool_name="verification_run",
        ),
        StreamEvent(
            kind=KIND_TOOL_START,
            summary="GoogleSearch",
            tool_name="GoogleSearch",
        ),
        StreamEvent(kind=KIND_TOOL_START, summary="Agent", tool_name="Agent"),
    ]

    for event in events:
        digest.record(event)

    assert digest.render_lines() == [
        "🔎 Performed 1 inspection",
        "📝 Performed 1 file change",
        "⚙️ Ran 1 command",
        "🧪 Ran 1 check",
        "🌐 Used 1 external operation",
    ]


def test_digest_marks_nonzero_command_exit_as_failure() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(
            kind=KIND_SHELL_EXEC,
            summary="Running: python build.py",
            tool_name="Bash",
            metadata={"command": "python build.py"},
        )
    )
    digest.record(
        StreamEvent(
            kind=KIND_TOOL_END,
            summary="Command exited (2)",
            tool_name="Bash",
            metadata={"exit_code": 2},
        )
    )

    assert digest.render_lines() == ["⚙️ Ran 1 command · 1 failure ❌"]


def test_digest_discloses_completed_with_limitations_terminal_state() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(
            kind=KIND_PROGRESS,
            summary="Task completed with limitations",
            phase="COMPLETED_WITH_LIMITATIONS",
            metadata={
                "activity_type": "lifecycle",
                "status": "completed",
                "lifecycle_state": "COMPLETED_WITH_LIMITATIONS",
                "terminal": True,
            },
        )
    )

    assert digest.phase_label == "Completed"
    assert digest.render_lines() == ["⚠️ Completed with limitations"]


def test_digest_uses_backend_todo_as_planning_until_work_starts() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(kind=KIND_PROGRESS, summary="Updated task list")
    )
    assert digest.phase_label == "Planning"

    digest.record(
        StreamEvent(
            kind=KIND_SHELL_EXEC,
            summary="Running: python task.py",
            metadata={"command": "python task.py"},
        )
    )
    assert digest.phase_label == "Execution"
