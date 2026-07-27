from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_wol


@pytest.mark.asyncio
async def test_wol_command_uses_configured_instance_identity(tmp_path, monkeypatch):
    calls = []
    replies = []

    def available(project_root, instance_id):
        calls.append(("available", project_root, instance_id))
        return True

    def run(project_root, target, *, configured_instance_id=None):
        calls.append(("run", project_root, target, configured_instance_id))
        return {"ok": True, "label": "Workstation", "stdout": "sent"}

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    monkeypatch.setattr(runtime_wol, "private_wol_available", available)
    monkeypatch.setattr(runtime_wol, "run_private_wol", run)
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(
            project_root=tmp_path,
            instance_id="HASHI-LOCAL",
        ),
        _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await runtime_wol.cmd_wol(runtime, update, SimpleNamespace(args=["workstation"]))

    assert calls == [
        ("available", tmp_path, "HASHI-LOCAL"),
        ("run", tmp_path, "workstation", "HASHI-LOCAL"),
    ]
    assert replies[-1] == "✅ WoL completed for Workstation.\n\nsent"
