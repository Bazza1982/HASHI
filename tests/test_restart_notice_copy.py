"""FC notices distinguish successful local readiness from optional connection progress."""
import pytest
from orchestrator import ui_language
from orchestrator.reboot_ui import render_notice
from orchestrator.reboot_manager import RebootManager
from remote.restart_receipts import project_restart
from tests.test_cold_restart_observation import record as cold_record, complete


def hot_record(**changes):
    return {
        "id": "a" * 32, "source_agent": "agent1", "mode": "max",
        "targets": ["agent1", "agent2"], "display_names": {},
        "status": "succeeded", "lifecycle_state": "online",
        "outcome": "succeeded_with_warning", "observation_confidence": "confirmed",
        "online": {"agent1": True, "agent2": True},
        "warnings": ["connectors_unavailable"], "duration_seconds": 139.8,
        "locale": "zh-CN", **changes,
    }


@pytest.mark.parametrize(("locale", "expected"), [
    ("zh-CN", "✅ 完整热重启成功，耗时139.8秒，2个代理在线。\n其它连接形式进行中。"),
    ("en", "✅ Full hot reboot succeeded in 139.8s; 2 agents online.\nOther connection types are being established."),
])
def test_optional_connector_recovery_is_success_with_progress(locale, expected):
    record = hot_record(locale=locale)
    assert render_notice(record) == expected
    projection = RebootManager._operation_projection(record)
    assert projection["notice"] == expected
    # Rendering must not rewrite the durable diagnostic or lifecycle outcome.
    assert projection["warnings"] == ["connectors_unavailable"]
    assert record["outcome"] == "succeeded_with_warning"


def test_genuine_followup_failure_and_unconfirmed_results_keep_their_warnings():
    warning = render_notice(hot_record(warnings=["connectors_unavailable", "topology_publish_failed"]))
    assert warning.startswith("⚠️")
    assert "拓扑更新未能发布" in warning
    assert "外部连接暂不可用" not in warning
    uncertain = render_notice(hot_record(
        status="unconfirmed", outcome="failed", lifecycle_state="unconfirmed",
        observation_confidence="unconfirmed", online={},
    ))
    assert uncertain.startswith("⚠️") and "✅" not in uncertain


@pytest.mark.parametrize(("locale", "hot", "cold"), [
    ("en", "🔄 Starting hot reboot. This takes about 2 minutes.",
     "🔄 Starting a full system restart. This takes about 5 minutes."),
    ("zh-CN", "🔄 开始热重启。这大约需要2分钟时间。",
     "🔄 开始完整系统重启。这大约需要5分钟时间。"),
])
def test_estimates_come_from_fc_for_each_supported_runtime_locale(locale, hot, cold):
    assert locale in ui_language.SUPPORTED_LOCALES
    for mode in ("same", "number", "group", "max"):
        assert render_notice(hot_record(mode=mode, locale=locale), starting=True) == hot
    assert ui_language.tr("api.restart.requested", locale=locale, instance="HASHI3") == cold
    record = cold_record()
    record["notify_locale"] = locale
    projection = project_restart(record, instance_id="HASHI3", owner_id="user:7")
    assert projection["notice"] == cold
    completed = project_restart(complete(record), instance_id="HASHI3", owner_id="user:7")
    assert completed["notice"] == ui_language.tr("api.restart.completed", locale=locale, instance="HASHI3")
    record["evidence"]["old_pid_exited"] = False
    unconfirmed = project_restart(record, instance_id="HASHI3", owner_id="user:7")
    assert unconfirmed["notice"] == ui_language.tr("api.restart.ack_unknown", locale=locale)


def test_fc_plain_projection_preserves_safe_display_names():
    record = hot_record(mode="same", targets=["agent1"],
                        display_names={"agent1": "<A & B>"}, warnings=[],
                        outcome="succeeded", locale="en")
    assert "&lt;A &amp; B&gt;" in render_notice(record)
    assert "<A & B>" in RebootManager._operation_projection(record)["notice"]
# HASHI-Fingerprint: hfp_25969d2dce8b4dd7911e30fc798d7993
