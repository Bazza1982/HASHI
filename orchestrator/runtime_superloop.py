from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore, _json_dump
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_waits import SuperloopWaitsService


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
        "/superloop record intent <recording_id> <summary>\n"
        "/superloop record exit <recording_id> <kind> <details-json>\n"
        "/superloop record finish [recording_id]\n"
        "/superloop status <loop_id>\n"
        "/superloop pause <loop_id>\n"
        "/superloop resume <loop_id>\n"
        "/superloop next <loop_id>\n"
        "/superloop task add <loop_id> <title>\n"
        "/superloop issue add <loop_id> <title>\n"
        "/superloop wait add <loop_id> <kind> [deadline-iso]"
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
        recording_service.set_intent_summary(
            result["recording_id"],
            intent_summary=goal,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        recording_service.set_exit_condition(
            result["recording_id"],
            exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": []}},
            actor_agent=runtime.name,
            actor_instance=local_instance,
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

    if lowered[:2] == ["record", "intent"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop record intent <recording_id> <summary>")
            return
        recording_id = parts[2]
        summary = raw.split(None, 3)[3].strip()
        recording_service.set_intent_summary(
            recording_id,
            intent_summary=summary,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(update, f"✅ intent summary updated for `{recording_id}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["record", "exit"]:
        if len(parts) < 5:
            await runtime._reply_text(
                update,
                "Usage: /superloop record exit <recording_id> <kind> <details-json>",
            )
            return
        recording_id = parts[2]
        kind = parts[3]
        json_text = raw.split(None, 4)[4].strip()
        try:
            details = json.loads(json_text)
            if not isinstance(details, dict):
                raise ValueError("details must be a JSON object")
        except Exception as exc:
            await runtime._reply_text(update, f"Invalid details JSON: {exc}")
            return
        recording_service.set_exit_condition(
            recording_id,
            exit_condition={"kind": kind, "details": details},
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(update, f"✅ exit condition updated for `{recording_id}`", parse_mode="Markdown")
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

    if lowered[:1] == ["pause"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop pause <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        state["status"] = "paused"
        _json_dump(store.loop_dir(loop_id) / "state.json", state)
        store.append_loop_event(loop_id, event_type="loop.paused", data={"source": "command"})
        await runtime._reply_text(update, f"⏸ Paused `{loop_id}`", parse_mode="Markdown")
        return

    if lowered[:1] == ["resume"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop resume <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        state["status"] = "running"
        _json_dump(store.loop_dir(loop_id) / "state.json", state)
        store.append_loop_event(loop_id, event_type="loop.resumed", data={"source": "command"})
        await runtime._reply_text(update, f"▶ Resumed `{loop_id}`", parse_mode="Markdown")
        return

    if lowered[:1] == ["next"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop next <loop_id>")
            return
        loop_id = parts[1]
        runner = SuperloopRunner(store)
        try:
            result = runner.next_action(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(
            update,
            (
                "⏭ Next action evaluated\n"
                f"loop_id: `{loop_id}`\n"
                f"advanced: `{result.get('advanced')}`\n"
                f"reason: `{result.get('reason', '')}`\n"
                f"task_id: `{result.get('task_id', '')}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["task", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop task add <loop_id> <title>")
            return
        loop_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        service = SuperloopTaskboardService(store)
        try:
            task = service.add_task(
                loop_id,
                title=title,
                owner_agent=runtime.name,
                owner_instance=local_instance,
            )
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ task added: `{task['task_id']}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["issue", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop issue add <loop_id> <title>")
            return
        loop_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        service = SuperloopIssuesService(store)
        try:
            issue = service.open_issue(
                loop_id,
                title=title,
                severity="medium",
                opened_by_agent=runtime.name,
                opened_by_instance=local_instance,
            )
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ issue opened: `{issue['issue_id']}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["wait", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop wait add <loop_id> <kind> [deadline-iso]")
            return
        loop_id = parts[2]
        kind = parts[3]
        deadline = parts[4] if len(parts) >= 5 else None
        details = {"until": deadline} if kind == "sleep_until" and deadline else None
        service = SuperloopWaitsService(store)
        try:
            wait = service.add_wait(loop_id, kind=kind, details=details, deadline=deadline)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ wait added: `{wait['wait_id']}`", parse_mode="Markdown")
        return

    await runtime._reply_text(update, _help_text())
