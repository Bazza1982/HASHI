from pathlib import Path

from tools.her_debug.restart_guard import restart_receipt_ready, runtime_start_marker


def test_restart_receipt_rejects_pre_restart_online_idle_runtime() -> None:
    assert not restart_receipt_ready(
        previous_started_at="2026-08-12T14:32:00",
        current_started_at="2026-08-12T14:32:00",
        online=True,
        idle=True,
    )


def test_restart_receipt_waits_for_new_runtime_to_be_ready() -> None:
    assert not restart_receipt_ready(
        previous_started_at="2026-08-12T14:32:00",
        current_started_at="2026-08-12T14:33:00",
        online=True,
        idle=False,
    )
    assert restart_receipt_ready(
        previous_started_at="2026-08-12T14:32:00",
        current_started_at="2026-08-12T14:33:00",
        online=True,
        idle=True,
    )


def test_runtime_start_marker_is_fail_closed(tmp_path: Path) -> None:
    state = tmp_path / ".runtime_session.json"

    assert runtime_start_marker(state) is None
    state.write_text("not-json", encoding="utf-8")
    assert runtime_start_marker(state) is None
    state.write_text('{"last_started_at":"2026-08-12T14:33:00"}\n', encoding="utf-8")
    assert runtime_start_marker(state) == "2026-08-12T14:33:00"
