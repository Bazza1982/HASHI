from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_credit


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=123))


def _context(*args):
    return SimpleNamespace(args=list(args))


class _Backend:
    def __init__(self, key_info):
        self._key_info = key_info

    async def get_key_info(self):
        return self._key_info


def _runtime(backend):
    replies = []
    runtime = SimpleNamespace(
        backend_manager=SimpleNamespace(current_backend=backend),
        _is_authorized_user=lambda user_id: True,
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    runtime._reply_text = _reply_text
    return runtime, replies


@pytest.mark.asyncio
async def test_cmd_credit_reports_unavailable_without_openrouter_backend():
    runtime, replies = _runtime(backend=None)

    await runtime_credit.cmd_credit(runtime, _update(), _context())

    assert replies[-1][0] == "Credit info is only available for OpenRouter backends."


@pytest.mark.asyncio
async def test_cmd_credit_reports_fetch_failure():
    runtime, replies = _runtime(_Backend(None))

    await runtime_credit.cmd_credit(runtime, _update(), _context())

    assert replies[-1][0] == "Failed to fetch credit info."


@pytest.mark.asyncio
async def test_cmd_credit_reports_key_info():
    runtime, replies = _runtime(
        _Backend(
            {
                "data": {
                    "label": "primary",
                    "usage": 12,
                    "limit": 100,
                    "limit_remaining": 88,
                    "is_free_tier": False,
                }
            }
        )
    )

    await runtime_credit.cmd_credit(runtime, _update(), _context())

    assert replies[-1][0] == (
        "OpenRouter key: primary\n"
        "Usage: 12\n"
        "Limit: 100\n"
        "Remaining: 88\n"
        "Free tier: False"
    )
