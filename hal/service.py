"""HAL v0.1 service: optional runtime loop and Workbench API handlers.

Owned by the existing Workbench API server process (Layer-2 Function). HAL adds
no new process, no supervisor, no scheduler and no forced dependency:

- zero enabled rules  -> no loop task at all (or the loop exits);
- enabled rules       -> one bounded feed check per <=5s (same precedent as the
                          existing HEA long-poll fallback), event-driven wake;
- every execution goes through the SAME authenticated prompt/command adapters
  a connected user uses. HAL never implements a second executor.

All persistence is synchronous SQLite behind ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from aiohttp import web

from hal import compile_hal
from hal.dispatch import DispatchAdapter, DispatchResult, strip_agent_spec
from hal.event_source import (
    EVENT_SYSTEM_IDLE,
    HalTrigger,
    feed_event_hal_event,
)
from hal.runtime import (
    EXEC_ACTIVE,
    EXEC_FAILED,
    RECEIPT_DISPATCHED,
    RECEIPT_FAILED,
    program_from_json,
    plan_for_trigger,
    stage_after_dispatch,
    unavailable_wait_detail,
    validate_script,
)
from hal.store import HalStore, HalStoreError

RECEIPT_PENDING = "pending_retry"
RECEIPT_UNKNOWN = "unknown"

POLL_INTERVAL_SECONDS = 5.0
MAX_SCRIPT_BYTES = 64 * 1024
MAX_PAGE_EVENTS = 100

SCHEMA_ID = "hashi.hal/v1"


class HalService:
    def __init__(
        self,
        *,
        store: HalStore,
        session_store,
        dispatch: DispatchAdapter,
        logger: logging.Logger | None = None,
    ):
        self.store = store
        self.session_store = session_store
        self.dispatch = dispatch
        self.logger = logger or logging.getLogger("hal.runtime")
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._idle_latch: dict[str, str] = {}  # owner -> last idle evidence run_id

    # ------------------------------------------------------------------ API

    def status(self) -> dict[str, Any]:
        enabled = self.store.enabled_rule_count()
        return {
            "ok": True,
            "schema": SCHEMA_ID,
            "available": True,
            "instance_id": getattr(self.session_store, "instance_id", None),
            "enabled_rule_count": enabled,
            "runtime_loop_running": self._loop_task is not None and not self._loop_task.done(),
            "event_availability": {
                event: {
                    "available": available,
                    "source": reason,
                }
                for event, (available, reason) in _ALL_EVENT_AVAILABILITY.items()
            },
        }

    def validate(self, script: str) -> dict[str, Any]:
        return validate_script(script)

    def upsert_rule(
        self,
        *,
        owner_id: str,
        name: str,
        script: str,
        rule_id: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        result = validate_script(script)
        if not result["ok"]:
            raise HalStoreError(result["error"] or "invalid HAL script")
        return self.store.upsert_rule(
            owner_id=owner_id,
            name=name,
            script=script,
            rule_id=rule_id,
            expected_revision=expected_revision,
        )

    def set_enabled(
        self,
        *,
        owner_id: str,
        rule_id: str,
        enabled: bool,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        rule = self.store.get_rule(owner_id, rule_id)
        if rule is None:
            raise HalStoreError("rule not found")
        if enabled:
            result = validate_script(rule["script"])
            if not result["ok"]:
                raise HalStoreError(f"stored script no longer compiles: {result['error']}")
            workflow = result["workflows"][0]
            if not workflow["entry_available"]:
                raise HalStoreError(
                    "entry event has no real source yet; "
                    + "; ".join(d["message"] for d in result["diagnostics"] if d["kind"] == "entry_event_unavailable")
                )
            if self.store.get_cursor(owner_id) == -1:
                # Anchor the feed cursor at enable time: only events committed
                # after enabling may trigger this owner's rules.
                try:
                    page = _feed_page(self.session_store, owner_id, -1, 1)
                    self.store.set_cursor(owner_id, page["next_cursor"])
                except Exception:  # noqa: BLE001 — loop bootstraps on first cycle
                    pass
        updated = self.store.set_enabled(
            owner_id, rule_id, enabled, expected_revision=expected_revision
        )
        self._note_rules_changed()
        return updated

    def list_rules(self, owner_id: str) -> list[dict[str, Any]]:
        rows = []
        for rule in self.store.list_rules(owner_id):
            public = {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "script": rule["script"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
                "created_at": rule["created_at"],
                "updated_at": rule["updated_at"],
            }
            public["validation"] = {
                key: value for key, value in validate_script(rule["script"]).items()
            }
            rows.append(public)
        return rows

    def list_runs(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "exec_id": row["exec_id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "rule_revision": row["rule_revision"],
                "stage": row["stage"],
                "state": row["state"],
                "trigger_event_id": row["trigger_event_id"],
                "trigger_agent": row["trigger_agent"],
                "trigger_kind": row["trigger_kind"],
                "detail": row["detail"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in self.store.list_execs(owner_id, limit=limit)
        ]

    def run_detail(self, owner_id: str, exec_id: str) -> dict[str, Any] | None:
        row = self.store.get_exec(owner_id, exec_id)
        if row is None:
            return None
        detail = dict(row)
        detail["receipts"] = self.store.list_receipts(exec_id)
        return detail

    # ------------------------------------------------------------------ loop

    def start_loop(self) -> None:
        if self._stopping:
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.get_running_loop().create_task(self._run_loop())

    def stop_loop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._loop_task
        if task is not None and not task.done():
            task.cancel()

    def _note_rules_changed(self) -> None:
        if self._stopping:
            return
        try:
            self._wake.set()
        except RuntimeError:
            pass
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # synchronous caller (tests): no loop to start
        if self.store.enabled_rule_count() > 0:
            self.start_loop()

    async def _run_loop(self) -> None:
        self.logger.info("HAL runtime loop started")
        try:
            while not self._stopping:
                owners = await asyncio.to_thread(self.store.enabled_owner_ids)
                if not owners:
                    self.logger.info("HAL runtime loop exiting: no enabled rules")
                    return
                activity = False
                for owner in owners:
                    if await self._process_owner(owner):
                        activity = True
                try:
                    if activity:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.1)
                    else:
                        await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._wake.clear()
        except asyncio.CancelledError:
            raise
        finally:
            self._loop_task = None

    async def _process_owner(self, owner_id: str) -> bool:
        """One bounded feed check + deterministic planning + idempotent apply."""
        activity = False
        try:
            cursor = await asyncio.to_thread(self.store.get_cursor, owner_id)
            page = await asyncio.to_thread(
                _feed_page, self.session_store, owner_id, cursor, MAX_PAGE_EVENTS
            )
            events = page.get("events") or []
            new_cursor = page.get("next_cursor", cursor)
            has_more = bool(page.get("has_more"))
            reset = bool(page.get("reset_required"))
        except Exception as exc:  # noqa: BLE001 — feed outage must never kill the loop
            self.logger.warning("HAL feed read failed for %s: %s", owner_id, exc)
            return False

        if cursor < 0 or reset:
            # Bootstrap: adopt the current feed position. Enabling HAL never
            # replays pre-enable history.
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return False

        if not events:
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return has_more

        rules = await self._load_enabled_rules(owner_id)
        if not rules:
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return False

        execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        for row in execs:
            try:
                row["program"] = program_from_json(json.loads(row["program_blob"]))
            except (ValueError, KeyError, TypeError):
                row["program"] = None
        execs = [row for row in execs if row.get("program") is not None]
        execs_by_id = {str(e["exec_id"]): e for e in execs}
        receipts = await self._load_receipts(execs)
        consumed: dict[str, set[str]] = {}

        active_at_cycle_start = await asyncio.to_thread(_active_runs, self.session_store, owner_id)
        if active_at_cycle_start:
            self._idle_latch.pop(owner_id, None)

        idle_emitted_this_page = False
        for event in events:
            trigger = feed_event_hal_event(event)
            if trigger is not None and trigger.agent_identity:
                # Canonical HAL agent identity: @name@instance (spec 2.2).
                instance_id = getattr(self.session_store, "instance_id", "") or ""
                suffix = f"@{instance_id}" if instance_id else ""
                trigger = _with_agent_identity(trigger, f"@{trigger.agent_identity}{suffix}")
            if trigger is not None:
                if await self._apply_trigger(
                    owner_id, trigger, rules, execs_by_id, receipts, consumed
                ):
                    activity = True
            # Idle edge: real evidence only — a non-automation run became
            # terminal and no runs remain active.
            if (
                not idle_emitted_this_page
                and trigger is not None
                and not trigger.automation_origin
                and not await asyncio.to_thread(_active_runs, self.session_store, owner_id)
            ):
                run_id = _event_run_id(event)
                if run_id != self._idle_latch.get(owner_id):
                    idle = HalTrigger(
                        event=EVENT_SYSTEM_IDLE,
                        agent_identity=None,
                        feed_event_id=trigger.feed_event_id,
                        feed_kind="system_idle",
                        run_idempotency_key=None,
                        automation_origin=False,
                    )
                    if await self._apply_trigger(
                        owner_id, idle, rules, execs_by_id, receipts, consumed
                    ):
                        activity = True
                    self._idle_latch[owner_id] = run_id
                    idle_emitted_this_page = True

        await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)

        # Reload after applying this cycle's intents so freshly created
        # executions with pending dispatches are retried in the same cycle.
        fresh_execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        fresh_by_id = {str(e["exec_id"]): e for e in fresh_execs}
        fresh_receipts = await self._load_receipts(fresh_execs)
        await self._retry_pending(owner_id, rules, fresh_by_id, fresh_receipts)
        return activity

    async def _load_enabled_rules(self, owner_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self.store.list_rules, owner_id)
        loaded = []
        for row in rows:
            if not row["enabled"]:
                continue
            try:
                program = compile_hal(row["script"])
            except Exception as exc:  # noqa: BLE001 — stored scripts must not crash the loop
                self.logger.error("HAL rule %s failed to compile: %s", row["rule_id"], exc)
                continue
            row["program"] = program
            loaded.append(row)
        return loaded

    async def _load_receipts(self, execs: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
        receipts: dict[tuple[str, int], dict[str, Any]] = {}
        for exec_row in execs:
            for receipt in await asyncio.to_thread(self.store.list_receipts, exec_row["exec_id"]):
                receipts[(str(receipt["exec_id"]), int(receipt["stage"]))] = receipt
        return receipts

    async def _apply_trigger(
        self,
        owner_id: str,
        trigger: HalTrigger,
        rules: list[dict[str, Any]],
        execs_by_id: dict[str, dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
        consumed: dict[str, set[str]],
    ) -> bool:
        activity = False
        creates, advances, hints = await asyncio.to_thread(
            plan_for_trigger,
            trigger,
            rules,
            list(execs_by_id.values()),
            list(receipts.values()),
            _flatten_consumed(consumed),
        )
        for hint in hints:
            await asyncio.to_thread(
                self.store.update_exec, hint.exec_id, detail=hint.detail
            )
        for intent in creates:
            # Idempotent per (rule, event): a replay after crash reuses the row.
            created = await asyncio.to_thread(
                self.store.create_exec,
                owner_id=owner_id,
                rule_id=intent.rule_id,
                rule_revision=intent.rule_revision,
                rule_name=intent.rule_name,
                workflow_index=0,
                exec_id=intent.exec_id,
                trigger_event_id=intent.trigger_event_id,
                trigger_agent=intent.trigger_agent,
                trigger_kind=intent.trigger_kind,
                program_blob=_program_for_rule(rules, intent.rule_id),
                detail=None,
            )
            exec_row = await asyncio.to_thread(self.store.get_exec, owner_id, intent.exec_id)
            if exec_row is None:
                continue
            if await self._dispatch_stage(owner_id, exec_row, intent.directive, rules, receipts):
                activity = True
            if created is False:
                # Replayed create: reconcile stage/receipt divergence from a crash.
                await self._reconcile(owner_id, exec_row, rules, receipts)
        for intent in advances:
            if intent.exec_id not in execs_by_id:
                continue
            fresh = await asyncio.to_thread(self.store.mark_exec_event_consumed, intent.exec_id, intent.trigger_event_id)
            if not fresh:
                continue
            exec_row = execs_by_id[intent.exec_id]
            if int(exec_row["stage"]) != intent.stage:
                continue
            if await self._dispatch_stage(owner_id, exec_row, intent.directive, rules, receipts):
                activity = True
        return activity

    async def _dispatch_stage(
        self,
        owner_id: str,
        exec_row: Mapping[str, Any],
        directive,
        rules: list[dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> bool:
        """Dispatch one stage once; crash-safe and never double-executing."""
        exec_id = directive.exec_id
        stage = directive.stage
        existing = receipts.get((exec_id, stage))
        if existing is not None and existing["state"] in (RECEIPT_DISPATCHED, RECEIPT_FAILED):
            return False
        if existing is not None and existing["state"] == RECEIPT_UNKNOWN and directive.kind == "native_command":
            # Ambiguous after an interruption: never re-run a command.
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=existing["idempotency_key"],
                kind=directive.kind,
                target=existing["target"],
                payload=existing["payload"],
                state=RECEIPT_FAILED,
                detail="command outcome unknown after interruption; manual verification advised",
            )
            await asyncio.to_thread(
                self.store.update_exec, exec_id, state=EXEC_FAILED,
                detail="native command outcome unknown after interruption",
            )
            return False

        if directive.unbound_reason:
            result = DispatchResult(
                accepted=False, transient=False, detail=directive.unbound_reason
            )
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=directive.idempotency_key,
                kind=directive.kind,
                target=directive.target,
                payload=directive.payload,
                state=RECEIPT_FAILED,
                detail=result.detail,
            )
            await asyncio.to_thread(
                self.store.update_exec, exec_id, state=EXEC_FAILED, detail=result.detail
            )
            return False

        pre_state = RECEIPT_PENDING if directive.kind == "prompt" else RECEIPT_UNKNOWN
        await asyncio.to_thread(
            self.store.upsert_receipt,
            exec_id=exec_id,
            stage=stage,
            idempotency_key=directive.idempotency_key,
            kind=directive.kind,
            target=directive.target,
            payload=directive.payload,
            state=pre_state,
            detail=None,
        )

        if directive.kind == "prompt":
            target = strip_agent_spec(directive.target)
            if directive.target == "@agent_trigger" and not exec_row.get("trigger_agent"):
                result = DispatchResult(
                    accepted=False,
                    transient=False,
                    detail="prompt target @agent_trigger has no Agent for this system event",
                )
            elif target is None:
                result = DispatchResult(accepted=False, transient=False, detail="prompt target missing")
            else:
                result = await self.dispatch.dispatch_prompt(
                    target_agent=target,
                    text=directive.payload,
                    idempotency_key=directive.idempotency_key,
                    exec_id=exec_id,
                    stage=stage,
                )
        else:
            result = await self.dispatch.dispatch_command(
                command_text=directive.payload,
                trigger_agent=exec_row.get("trigger_agent"),
            )

        if result.accepted:
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=directive.idempotency_key,
                kind=directive.kind,
                target=directive.target,
                payload=directive.payload,
                state=RECEIPT_DISPATCHED,
                detail=result.detail,
            )
            program = program_from_json(json.loads(exec_row["program_blob"]))
            workflow = program.workflows[int(exec_row["workflow_index"])]
            next_stage, state = stage_after_dispatch(stage, len(workflow))
            detail = None
            if state == EXEC_ACTIVE and next_stage < len(workflow):
                detail = unavailable_wait_detail(workflow[next_stage].event)
            await asyncio.to_thread(
                self.store.update_exec,
                exec_id,
                stage=next_stage,
                state=state,
                detail=detail,
            )
            return True

        # Failure: transient (prompt only) keeps the same idempotency key for
        # retry; permanent failures stop the workflow honestly.
        final_state = RECEIPT_PENDING if result.transient else RECEIPT_FAILED
        await asyncio.to_thread(
            self.store.upsert_receipt,
            exec_id=exec_id,
            stage=stage,
            idempotency_key=directive.idempotency_key,
            kind=directive.kind,
            target=directive.target,
            payload=directive.payload,
            state=final_state,
            detail=result.detail,
        )
        if not result.transient:
            await asyncio.to_thread(
                self.store.update_exec, exec_id, state=EXEC_FAILED, detail=result.detail
            )
        return False

    async def _reconcile(
        self,
        owner_id: str,
        exec_row: Mapping[str, Any],
        rules: list[dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        """Repair stage/state divergence after a crash between writes."""
        stage = int(exec_row["stage"])
        program = program_from_json(json.loads(exec_row["program_blob"]))
        workflow = program.workflows[int(exec_row["workflow_index"])]
        if stage >= len(workflow):
            return
        receipt = await asyncio.to_thread(self.store.get_receipt, exec_row["exec_id"], stage)
        if receipt is None or receipt["state"] == RECEIPT_PENDING:
            return  # retry pass handles the dispatch
        if receipt["state"] == RECEIPT_DISPATCHED:
            next_stage, state = stage_after_dispatch(stage, len(workflow))
            detail = None
            if state == EXEC_ACTIVE and next_stage < len(workflow):
                detail = unavailable_wait_detail(workflow[next_stage].event)
            await asyncio.to_thread(
                self.store.update_exec,
                exec_row["exec_id"],
                stage=next_stage,
                state=state,
                detail=detail,
            )

    async def _retry_pending(
        self,
        owner_id: str,
        rules: list[dict[str, Any]],
        execs_by_id: dict[str, dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        """Retry prompt dispatches still pending; resolve ambiguous commands."""
        from hal.runtime import DispatchDirective

        for exec_row in execs_by_id.values():
            stage = int(exec_row["stage"])
            receipt = receipts.get((exec_row["exec_id"], stage))
            if receipt is None or receipt["state"] not in (RECEIPT_PENDING, RECEIPT_UNKNOWN):
                continue
            if receipt["state"] == RECEIPT_UNKNOWN and receipt["kind"] == "native_command":
                await asyncio.to_thread(
                    self.store.upsert_receipt,
                    exec_id=exec_row["exec_id"],
                    stage=stage,
                    idempotency_key=receipt["idempotency_key"],
                    kind=receipt["kind"],
                    target=receipt["target"],
                    payload=receipt["payload"],
                    state=RECEIPT_FAILED,
                    detail="command outcome unknown after interruption; manual verification advised",
                )
                await asyncio.to_thread(
                    self.store.update_exec, exec_row["exec_id"], state=EXEC_FAILED,
                    detail="native command outcome unknown after interruption",
                )
                continue
            directive = DispatchDirective(
                exec_id=exec_row["exec_id"],
                stage=stage,
                kind=receipt["kind"],
                target=receipt["target"],
                payload=receipt["payload"],
                idempotency_key=receipt["idempotency_key"],
            )
            await self._dispatch_stage(owner_id, exec_row, directive, rules, receipts)


# ----------------------------------------------------------------- module APIs


def _with_agent_identity(trigger: HalTrigger, identity: str) -> HalTrigger:
    import dataclasses

    return dataclasses.replace(trigger, agent_identity=identity)


def _feed_page(session_store, owner_id: str, after: int, limit: int) -> dict[str, Any]:
    from orchestrator.automation_events import terminal_events_page

    return terminal_events_page(session_store, owner_id=owner_id, after=after, limit=limit)


def _active_runs(session_store, owner_id: str) -> bool:
    try:
        rows = session_store.list_active_runs(owner_id=owner_id)
    except Exception:  # noqa: BLE001 — evidence unavailable means no idle edge
        return True
    return bool(rows)


def _event_run_id(event: Mapping[str, Any]) -> str:
    return str(event.get("run_id") or event.get("event_id") or "")


def _flatten_consumed(consumed: dict[str, set[str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for exec_id, event_ids in consumed.items():
        for event_id in event_ids:
            out.add((exec_id, event_id))
    return out


def _program_for_rule(rules: list[dict[str, Any]], rule_id: str):
    for rule in rules:
        if str(rule["rule_id"]) == str(rule_id):
            return rule["program"]
    return None


_ALL_EVENT_AVAILABILITY = {
    "#turn_finish": (True, "real durable run-terminal feed event (run.completed)"),
    "#turn_error": (True, "real durable run-terminal feed event (run.failed)"),
    "#system_idle": (True, "derived active-to-idle edge from committed Session evidence"),
    "#turn_commentary": (False, "no commentary event source exists in the durable event feed"),
    "#hashideploy_finish": (False, "no deployment-completion event source exists"),
    "#reboot_ok": (False, "no reboot-completion event source exists"),
}


def _default_store_path(server) -> Path:
    bridge_home = Path(
        getattr(getattr(server, "global_config", None), "bridge_home", None)
        or getattr(server, "config_path", Path(".")).parent
    )
    return bridge_home / "hal" / "hal_v1.sqlite"


def ensure_service(server) -> HalService | None:
    """Build the service once per Workbench API server process."""
    service = getattr(server, "_hal_service", None)
    if service is not None:
        return service
    try:
        from hal.store import HalStore
        from hal.dispatch import GatewayDispatchAdapter

        store = HalStore(_default_store_path(server))
        service = HalService(
            store=store,
            session_store=server.session_store,
            dispatch=GatewayDispatchAdapter(server),
            logger=getattr(server, "logger", None),
        )
        server._hal_service = service
    except Exception:  # noqa: BLE001 — HAL must never break the gateway
        server._hal_service = None
        return None
    return service


def _require_auth(server, request):
    owner_id = server._v1_owner_id(request)
    if owner_id is None or not server._check_admin_auth(request):
        return None, web.json_response(
            {"ok": False, "error_code": "auth_required"}, status=401
        )
    return owner_id, None


async def _read_bounded_body(request: web.Request, max_bytes: int) -> tuple[bytes | None, web.Response | None]:
    if request.content_length is not None and request.content_length > max_bytes:
        return None, web.json_response(
            {"ok": False, "error_code": "body_too_large"}, status=413
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await request.content.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None, web.json_response(
                {"ok": False, "error_code": "body_too_large"}, status=413
            )
        chunks.append(chunk)
    return b"".join(chunks), None


def _service_or_503(server):
    service = ensure_service(server)
    if service is None:
        return None, web.json_response(
            {"ok": False, "error_code": "hal_unavailable"}, status=503
        )
    return service, None


async def handle_hal_status(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    return web.json_response(service.status(), headers={"Cache-Control": "no-store"})


async def handle_hal_validate(server, request):
    _owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            return web.json_response(
                {"ok": False, "error_code": "script_required"}, status=400
            )
    except (ValueError, UnicodeDecodeError):
        return web.json_response(
            {"ok": False, "error_code": "invalid_json"}, status=400
        )
    result = service.validate(script)
    return web.json_response(
        {"ok": result["ok"], "schema": SCHEMA_ID, **result},
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_rules_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    return web.json_response(
        {"ok": True, "schema": SCHEMA_ID, "rules": service.list_rules(owner_id)},
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_rules_post(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
        name = payload.get("name")
        script = payload.get("script")
        rule_id = payload.get("rule_id")
        expected_revision = payload.get("expected_revision")
        enable = bool(payload.get("enabled"))
        if not isinstance(name, str) or not isinstance(script, str):
            return web.json_response(
                {"ok": False, "error_code": "name_and_script_required"}, status=400
            )
        if rule_id is not None and not isinstance(rule_id, str):
            return web.json_response(
                {"ok": False, "error_code": "invalid_rule_id"}, status=400
            )
    except (ValueError, UnicodeDecodeError):
        return web.json_response(
            {"ok": False, "error_code": "invalid_json"}, status=400
        )
    try:
        rule, created = service.upsert_rule(
            owner_id=owner_id,
            name=name,
            script=script,
            rule_id=rule_id,
            expected_revision=expected_revision,
        )
        if enable:
            service.set_enabled(owner_id=owner_id, rule_id=rule["rule_id"], enabled=True)
            rule = service.store.get_rule(owner_id, rule["rule_id"])
    except HalStoreError as exc:
        return web.json_response(
            {"ok": False, "error_code": "hal_store_error", "detail": str(exc)},
            status=409 if "revision" in str(exc) else 400,
        )
    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "rule": {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "script": rule["script"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
                "created_at": rule["created_at"],
                "updated_at": rule["updated_at"],
            },
            "created": created,
        },
        status=201 if created else 200,
    )


async def handle_hal_rule_enable(server, request):
    return await _handle_hal_rule_toggle(server, request, True)


async def handle_hal_rule_disable(server, request):
    return await _handle_hal_rule_toggle(server, request, False)


async def _handle_hal_rule_toggle(server, request, enabled: bool):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    rule_id = request.match_info.get("rule_id")
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    expected_revision = None
    if body:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            expected_revision = payload.get("expected_revision")
        except (ValueError, UnicodeDecodeError):
            return web.json_response(
                {"ok": False, "error_code": "invalid_json"}, status=400
            )
    try:
        rule = service.set_enabled(
            owner_id=owner_id,
            rule_id=rule_id,
            enabled=enabled,
            expected_revision=expected_revision,
        )
    except HalStoreError as exc:
        status = 404 if "not found" in str(exc) else (409 if "revision" in str(exc) else 409)
        return web.json_response(
            {"ok": False, "error_code": "hal_store_error", "detail": str(exc)},
            status=status,
        )
    if rule is None:
        return web.json_response(
            {"ok": False, "error_code": "rule_not_found"}, status=404
        )
    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "rule": {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
            },
        }
    )


async def handle_hal_runs_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    try:
        limit = int(request.query.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    return web.json_response(
        {"ok": True, "schema": SCHEMA_ID, "runs": service.list_runs(owner_id, limit)},
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_run_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    exec_id = request.match_info.get("exec_id")
    detail = service.run_detail(owner_id, exec_id)
    if detail is None:
        return web.json_response(
            {"ok": False, "error_code": "run_not_found"}, status=404
        )
    return web.json_response(
        {"ok": True, "schema": SCHEMA_ID, "run": detail},
        headers={"Cache-Control": "no-store"},
    )
# HASHI-Fingerprint: hfp_ae365f75babe440f8247fb1d9ba7c5a2
