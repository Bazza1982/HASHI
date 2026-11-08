from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import NetworkError, RetryAfter

from orchestrator import runtime_delivery, telegram_delivery_failover as failover
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_supervisor import AgentRuntimeHandle
from orchestrator.telegram_delivery_errors import (
    TelegramDeliveryError,
    TelegramRateLimitRecoveryError,
    classify_telegram_delivery_error,
)
from tests.test_telegram_delivery_failover import _runtime, _write_delivery_state


def _kernel(root, *runtimes):
    kernel = SimpleNamespace(runtimes=list(runtimes), global_cfg=SimpleNamespace(project_root=root))
    for runtime in runtimes:
        runtime.orchestrator = kernel
    return kernel


@pytest.mark.asyncio
async def test_warning_ack_loss_survives_process_restart_without_duplicate_or_lost_reply(
    tmp_path, monkeypatch
):
    source = _runtime(tmp_path, "source")
    alternate = _runtime(tmp_path, "lin_yueru")
    _kernel(tmp_path, source, alternate)

    async def lose_result(*args, **kwargs):
        raise OSError("synthetic result-store outage after Telegram ACK")

    monkeypatch.setattr(failover, "_record_warning_result", lose_result)
    await failover.handle_retry_after(
        source, exc=RetryAfter(300), chat_id=123,
        request_id="business-once", purpose="placeholder",
    )
    assert len(alternate.app.bot.messages) == 1

    # A fresh process has no in-memory lock, runtime, or notification cache.
    child = """
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[2])
from tests.test_telegram_delivery_failover import _runtime
from orchestrator import telegram_delivery_failover as f
root=Path(sys.argv[1]);source=_runtime(root,"source");alternate=_runtime(root,"lin_yueru")
kernel=SimpleNamespace(runtimes=[source,alternate],global_cfg=SimpleNamespace(project_root=root))
source.orchestrator=alternate.orchestrator=kernel
blocked=asyncio.run(f.handle_blocked_send(source,chat_id=123,request_id="business-once",purpose="response",text="retained answer"))
saved=[json.loads(p.read_text(encoding="utf-8")) for p in (source.workspace_dir/"undelivered").glob("*.json")]
print(json.dumps({"blocked":blocked,"warning_sends":len(alternate.app.bot.messages),"saved":saved,"texts":[Path(x["markdown_path"]).read_text(encoding="utf-8") for x in saved]}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", child, str(tmp_path), str(Path(__file__).parents[1])],
        cwd=Path(__file__).parents[1],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1"),
        capture_output=True, text=True, timeout=30, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["blocked"] is True
    assert observed["warning_sends"] == 0, "an unconfirmed prior warning must not be resent"
    assert len(observed["saved"]) == 1
    assert observed["saved"][0]["request_id"] == "business-once"
    assert observed["texts"] == ["retained answer"]


@pytest.mark.asyncio
async def test_late_warning_result_cannot_quarantine_a_replacement_incarnation(tmp_path):
    source = _runtime(tmp_path, "source")
    alternate = _runtime(tmp_path, "lin_yueru")
    _kernel(tmp_path, source, alternate)
    replacement = _runtime(tmp_path, "source")
    replacement.config.extra["agent_lifecycle_id"] = "e" * 32
    replacement_owner = failover.owner_for_runtime(replacement)

    async def replace_before_ack(*args, **kwargs):
        def rotate(state):
            current = state["agents"]["source"]
            state["agents"]["source"] = {
                **current, "owner": replacement_owner,
                "incident_id": "replacement-incident", "per_chat": {"123": {}},
            }
            return None, True
        failover.mutate_state(failover.delivery_state_path(source), rotate)
        return SimpleNamespace(message_id=1)

    alternate._send_text = replace_before_ack
    await failover.handle_retry_after(
        source, exc=RetryAfter(300), chat_id=123,
        request_id="old-incarnation-request", purpose="placeholder",
    )
    state = failover.load_health_state(replacement)
    record = state["agents"]["source"]
    assert record["owner"] == replacement_owner
    assert record["incident_id"] == "replacement-incident"
    assert record["per_chat"]["123"] == {}
    assert not state.get("quarantine"), "a stale ACK cannot retire the new owner"


@pytest.mark.asyncio
async def test_recovery_does_not_retry_a_send_with_unknown_remote_outcome(tmp_path, monkeypatch):
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(failover, "_now", lambda: now[0])
    source = _runtime(tmp_path, "zelda")
    kernel = _kernel(tmp_path, source)
    path = _write_delivery_state(tmp_path, "zelda", {
        "status": "recovery_due", "blocked_until": "2000-01-01T00:00:00+00:00",
        "incident_id": "uncertain-ack", "per_chat": {"123": {}},
    })
    sends = []

    async def accepted_then_lost_ack(*args, **kwargs):
        sends.append(kwargs)
        try:
            raise httpx.ReadTimeout("synthetic ACK loss")
        except httpx.ReadTimeout as cause:
            raise NetworkError("Telegram response unknown") from cause

    source._send_text = accepted_then_lost_ack
    await failover._tick_recovery(kernel)
    now[0] += timedelta(minutes=5)
    await failover._tick_recovery(kernel)
    record = json.loads(path.read_text(encoding="utf-8"))["agents"]["zelda"]
    assert len(sends) == 1
    assert record["status"] == "recovery_stopped"
    assert record["per_chat"]["123"]["recovery_stop_reason"] == "delivery_outcome_unknown"
    assert "recovery_notice_sent_at" not in record["per_chat"]["123"]


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
async def test_recovery_failure_keeps_wait_and_frontend_meaning_across_worker_rpc(
    monkeypatch, locale
):
    async def send_text(*args, **kwargs):
        raise TelegramRateLimitRecoveryError(retry_after_s=17)

    host = object.__new__(FunctionWorkerHost)
    host.runtime = SimpleNamespace(_send_text=send_text)
    host.phase, host.accepting, host.agent_name = "ACTIVE", True, "source"
    result = await host.handle_request(
        "runtime.send_text", {"chat_id": 123, "text": "notice", "kwargs": {}}
    )
    handle = object.__new__(AgentRuntimeHandle)
    handle._route = AsyncMock(return_value=json.loads(json.dumps(result)))
    with pytest.raises(TelegramDeliveryError) as caught:
        await handle._send_text(123, "notice")
    monkeypatch.setattr(runtime_delivery.ui_language, "preferred_locale", lambda *a, **k: locale)
    fields = runtime_delivery.runtime_exception_fields(
        SimpleNamespace(config=SimpleNamespace(active_backend="codex-cli")),
        SimpleNamespace(owner_id="user:123", chat_id=123), caught.value,
    )
    assert fields["retry_after_s"] == 17
    assert fields["error_retryable"] is False
    public = fields["public_failure"]["text"]
    assert "17" in public
    assert "TELEGRAM_RATE_LIMIT_RECOVERY_FAILED" in public
    assert "Backend error" not in public and "后端错误" not in public


@pytest.mark.parametrize("prewrite", [True, False])
def test_transport_retry_proof_survives_json_rpc(prewrite):
    cause = httpx.ConnectTimeout("connect failed") if prewrite else httpx.ReadTimeout("ACK lost")
    error = NetworkError("synthetic transport failure")
    error.__cause__ = cause
    mapped = classify_telegram_delivery_error(error).to_mapping()
    restored = TelegramDeliveryError.from_mapping(json.loads(json.dumps(mapped)))
    assert getattr(restored, "request_not_submitted", False) is prewrite
    assert restored.retryable is True
    assert restored.permanent is False
# HASHI-Fingerprint: hfp_5f5bc52a801344079009022161132b3d\n\n
@pytest.mark.asyncio
async def test_conflicted_recovery_claim_is_discarded_before_any_send(tmp_path, monkeypatch):
    from orchestrator import telegram_delivery_state as state_store
    from orchestrator.config_json import ConfigConflictError

    source = _runtime(tmp_path, "zelda")
    kernel = _kernel(tmp_path, source)
    path = _write_delivery_state(tmp_path, "zelda", {
        "status": "recovery_due", "incident_id": "conflict-on-claim",
        "per_chat": {"123": {}},
    })
    write = state_store.write_config_json
    conflicts = []

    def conflict_once(*args, **kwargs):
        if not conflicts:
            conflicts.append(True)
            raise ConfigConflictError("synthetic compare-and-swap conflict")
        return write(*args, **kwargs)

    monkeypatch.setattr(state_store, "write_config_json", conflict_once)
    await failover._tick_recovery(kernel)
    record = json.loads(path.read_text(encoding="utf-8"))["agents"]["zelda"]
    assert len(source.app.bot.messages) == 1, "only the committed claim may send"
    assert record["per_chat"]["123"]["recovery_notice_sent_at"]

# HASHI-Fingerprint: hfp_5f5bc52a801344079009022161132b3d
