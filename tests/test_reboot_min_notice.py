"""Normal single-agent completion is a concise success, not a cleanup warning."""
import pytest
from orchestrator.reboot_ui import render_notice
from orchestrator.reboot_manager import RebootManager
from tests.test_restart_notice_copy import hot_record


@pytest.mark.parametrize(("locale", "expected"), [
    ("en", "✅ Hot reboot succeeded in 23.7s."),
    ("zh-CN", "✅ 热重启成功，耗时23.7秒。"),
])
@pytest.mark.parametrize("warnings", [
    [], ["old_worker_exit_pending"], ["connectors_unavailable"],
    ["connectors_unavailable", "old_worker_exit_pending"],
])
def test_min_success_is_direct_even_while_background_work_finishes(locale, expected, warnings):
    record = hot_record(
        mode="min", targets=["agent1"], display_names={"agent1": "Lumi (Chief of Staff)"},
        online={"agent1": True}, duration_seconds=23.7, locale=locale,
        outcome="succeeded_with_warning" if warnings else "succeeded", warnings=warnings,
    )
    assert render_notice(record) == expected
    assert RebootManager._operation_projection(record)["notice"] == expected
    assert record["warnings"] == warnings


@pytest.mark.parametrize(("locale", "expected"), [
    ("en", "🔄 Starting hot reboot. This takes about 30 seconds."),
    ("zh-CN", "🔄 开始热重启。这大约需要30秒时间。"),
])
def test_min_start_estimate_is_thirty_seconds(locale, expected):
    record = hot_record(mode="min", targets=["agent1"], locale=locale)
    assert render_notice(record, starting=True) == expected


def test_background_retirement_alone_is_not_an_outage_in_broad_success():
    record = hot_record(warnings=["old_worker_exit_pending"])
    assert render_notice(record).startswith("✅")
    assert "Worker" not in render_notice(record)


@pytest.mark.parametrize("failure", ["topology_publish_failed", "retirement_failed"])
def test_actionable_failures_are_not_hidden_by_background_progress(failure):
    record = hot_record(mode="min", targets=["agent1"],
                        warnings=["old_worker_exit_pending", failure])
    assert render_notice(record).startswith("⚠️")
    assert "✅" not in render_notice(record)


@pytest.mark.parametrize(("outcome", "status", "lifecycle"), [
    ("failed", "failed", "unconfirmed"),
    ("rolled_back", "failed", "rolled_back"),
    ("partial", "unconfirmed", "committed"),
    ("succeeded_with_warning", "succeeded", "online"),
])
def test_failed_or_uncertain_min_results_never_turn_into_success(outcome, status, lifecycle):
    record = hot_record(mode="min", targets=["agent1"], online={},
                        status=status, outcome=outcome, lifecycle_state=lifecycle,
                        observation_confidence="unconfirmed",
                        warnings=["old_worker_exit_pending", "connectors_unavailable"])
    assert not render_notice(record).startswith("✅")
# HASHI-Fingerprint: hfp_886ebac2c7094aa181bcfcfcf0e3a7eb
