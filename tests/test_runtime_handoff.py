from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_handoff


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(exchange_count=3, supports_sessions=False, backend_busy=False):
    replies = []
    sends = []
    requests = []
    bootstraps = []
    primers = []
    handoff_calls = []
    backend_calls = []

    class _HandoffBuilder:
        def refresh_recent_context(self):
            handoff_calls.append("refresh")

        def build_handoff(self):
            handoff_calls.append("build")

        def build_session_restore_prompt(self, max_rounds, max_words):
            handoff_calls.append((max_rounds, max_words))
            return "PROMPT", exchange_count, 321

    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _backend_busy=lambda: backend_busy,
        handoff_builder=_HandoffBuilder(),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(
                capabilities=SimpleNamespace(supports_sessions=supports_sessions),
                handle_new_session=lambda: None,
            )
            if supports_sessions
            else None
        ),
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _send_text(chat_id, text):
        sends.append((chat_id, text))

    async def enqueue_request(chat_id, prompt, source, summary):
        requests.append((chat_id, prompt, source, summary))

    async def enqueue_startup_bootstrap(chat_id):
        bootstraps.append(chat_id)

    def _arm_session_primer(text):
        primers.append(text)

    async def _handle_new_session():
        backend_calls.append("new-session")

    if supports_sessions:
        runtime.backend_manager.current_backend.handle_new_session = _handle_new_session

    runtime._reply_text = _reply_text
    runtime._send_text = _send_text
    runtime.enqueue_request = enqueue_request
    runtime.enqueue_startup_bootstrap = enqueue_startup_bootstrap
    runtime._arm_session_primer = _arm_session_primer
    return runtime, replies, sends, requests, bootstraps, primers, handoff_calls, backend_calls


@pytest.mark.asyncio
async def test_cmd_handoff_blocks_when_backend_busy():
    runtime, replies, sends, requests, *_ = _runtime(backend_busy=True)

    await runtime_handoff.cmd_handoff(runtime, _update(), _context())

    assert replies[-1][0] == "Handoff is blocked while a request is running or queued."
    assert sends == []
    assert requests == []


@pytest.mark.asyncio
async def test_cmd_handoff_reports_missing_recent_history():
    runtime, replies, sends, requests, bootstraps, primers, handoff_calls, backend_calls = _runtime(exchange_count=0)

    await runtime_handoff.cmd_handoff(runtime, _update(), _context())

    assert replies[-1][0] == "Starting a fresh session with recent bridge history..."
    assert sends[-1] == (456, "No recent bridge transcript was available for handoff.")
    assert requests == []
    assert primers == []
    assert handoff_calls == ["refresh", "build", (10, 6000)]
    assert backend_calls == []


@pytest.mark.asyncio
async def test_cmd_handoff_enqueues_restore_without_session_support():
    runtime, replies, sends, requests, bootstraps, primers, handoff_calls, backend_calls = _runtime(exchange_count=2)

    await runtime_handoff.cmd_handoff(runtime, _update(), _context())

    assert replies[-1][0] == "Starting a fresh session with recent bridge history..."
    assert primers and "bridge-managed handoff restore" in primers[-1]
    assert sends[-1] == (456, "Handoff prepared from 2 recent exchanges (321 words). Restoring continuity now...")
    assert requests == [(456, "PROMPT", "handoff", "Handoff restore [2 exchanges]")]
    assert bootstraps == []
    assert backend_calls == []


@pytest.mark.asyncio
async def test_cmd_handoff_resets_backend_session_when_supported():
    runtime, replies, sends, requests, bootstraps, primers, handoff_calls, backend_calls = _runtime(
        exchange_count=4,
        supports_sessions=True,
    )

    await runtime_handoff.cmd_handoff(runtime, _update(), _context())

    assert backend_calls == ["new-session"]
    assert bootstraps == [456]
    assert requests == [(456, "PROMPT", "handoff", "Handoff restore [4 exchanges]")]
