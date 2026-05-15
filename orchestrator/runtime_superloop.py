from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_store import SuperloopStore


def _local_instance_id() -> str:
    try:
        from tools.hchat_send import _get_instance_id, _load_config

        return str(_get_instance_id(_load_config()) or "HASHI").upper()
    except Exception:
        return "HASHI"


def _build_services(runtime) -> tuple[SuperloopStore, SuperloopRecordingService, SuperloopCompiler]:
    root = Path(runtime.global_config.project_root) / "superloops"
    store = SuperloopStore(root)
    return store, SuperloopRecordingService(store), SuperloopCompiler(store)


def _latest_recording_id(store: SuperloopStore) -> str | None:
    candidates = [item for item in store.recordings_dir.iterdir() if item.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0].name


def _help_text() -> str:
    return (
        "🧭 Superloop\n\n"
        "/superloop record start <goal>\n"
        "/superloop record status [recording_id]\n"
        "/superloop record try <recording_id> <step title>\n"
        "/superloop record finish [recording_id]\n"
        "/superloop status <loop_id>"
    )


async def handle_superloop_command(runtime, update, args_text: str) -> None:
    raw = (args_text or "").strip()
    if not raw:
        await runtime._reply_text(update, _help_text())
        return

    store, recording_service, compiler = _build_services(runtime)
    parts = raw.split()
    lowered = [part.lower() for part in parts]
    local_instance = _local_instance_id()

    if lowered[:2] == ["record", "start"]:
        goal = raw[len("record start") :].strip()
        if not goal:
            await runtime._reply_text(update, "Usage: /superloop record start <goal>")
            return
        result = recording_service.start_recording(
            goal=goal,
            owner_agent=runtime.name,
            owner_instance=local_instance,
            source_mode="incremental",
        )
        await runtime._reply_text(
            update,
            (
                "✅ Superloop recording started\n"
                f"recording_id: `{result['recording_id']}`\n"
                f"status: `{result['status']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "status"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(update, "No recording sessions found.")
            return
        payload = recording_service.get_status(recording_id)
        state = payload["state"]
        await runtime._reply_text(
            update,
            (
                "🧾 Superloop recording status\n"
                f"recording_id: `{recording_id}`\n"
                f"status: `{state.get('status')}`\n"
                f"goal: {state.get('goal')}\n"
                f"finish_ready: `{state.get('finish_ready')}`\n"
                f"candidate_steps: `{len(state.get('candidate_steps') or [])}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "try"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop record try <recording_id> <step title>")
            return
        recording_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        result = recording_service.record_trial_step(
            recording_id,
            title=title,
            step_kind="human_or_agent_action",
            owner_agent=runtime.name,
            owner_instance=local_instance,
            execution_mode="simulated",
            success=True,
        )
        await runtime._reply_text(
            update,
            (
                "🧪 Recorded trial step\n"
                f"recording_id: `{recording_id}`\n"
                f"step_id: `{result['recorded_as_step_id']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "finish"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(update, "No recording sessions found.")
            return
        result = compiler.compile_recording(
            recording_id,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        if not result.get("ok"):
            await runtime._reply_text(
                update,
                (
                    "⚠️ compile_blocked\n"
                    f"recording_id: `{recording_id}`\n"
                    f"missing: `{', '.join(result.get('missing') or [])}`"
                ),
                parse_mode="Markdown",
            )
            return
        await runtime._reply_text(
            update,
            (
                "✅ Superloop compiled\n"
                f"recording_id: `{recording_id}`\n"
                f"loop_id: `{result['loop_id']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["status"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop status <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(
            update,
            (
                "📌 Superloop status\n"
                f"loop_id: `{loop_id}`\n"
                f"status: `{state.get('status')}`\n"
                f"current_step: `{state.get('current_step')}`\n"
                f"next_action: `{json.dumps(state.get('next_action'), ensure_ascii=False)}`"
            ),
            parse_mode="Markdown",
        )
        return

    await runtime._reply_text(update, _help_text())
