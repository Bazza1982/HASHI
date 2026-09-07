from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from typing import Any

from orchestrator.bootstrap_logging import AnimMute

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")

# Worker startup is dominated by isolated imports and backend I/O.  A limit of
# two serialized six configured Agents into three avoidable waves.  Eight is a
# bounded default for larger installations while allowing common 4-8 Agent
# instances to prepare in one wave.
INITIAL_AGENT_STARTUP_CONCURRENCY = 8
STARTUP_PROGRESS_HEARTBEAT_SECONDS = 5.0
STARTUP_STALL_NOTICE_SECONDS = 15.0


def _remote_issue_from_result(result: dict[str, Any], instance_id: str) -> dict[str, Any]:
    settings = result.get("settings")
    supervisor = result.get("supervisor")
    action = str(result.get("action") or "lifecycle_failed")
    service_name = str(
        result.get("service_name")
        or getattr(supervisor, "service_name", "")
        or "the configured Remote supervisor"
    )
    port = getattr(settings, "port", None)
    reason = str(result.get("reason") or "Remote did not become healthy")
    code = action if action.startswith("remote_") else f"remote_{action}"

    if action == "supervisor_unavailable":
        cause = (
            f"Hashi Remote is included with HASHI, but its OS supervisor "
            f"{service_name} is not registered or enabled"
        )
        if port is not None:
            cause += f", and no healthy HASHI Remote endpoint was found on port {port}"
        actions = [
            "Use /remote on to activate Hashi Remote.",
            "If automatic activation remains unavailable, register and enable the OS supervisor with bin/hashi-remote-ctl.sh enable (Linux/WSL) or .\\bin\\hashi_remote_ctl.ps1 enable (Windows).",
        ]
    else:
        cause = reason
        actions = [
            "Run bin/hashi-remote-ctl.sh status and inspect the Remote supervisor logs.",
            "Check remote/config.yaml and the configured instance-specific service before retrying.",
        ]

    return {
        "code": code,
        "component": "remote",
        "severity": "warning",
        "summary": f"{instance_id} Remote/HChat is unavailable; local startup will continue.",
        "cause": cause + ".",
        "impact": f"Remote/HChat cannot connect to {instance_id}.",
        "unaffected": ["local agents", "Telegram", "Workbench", "API"],
        "automatic_retry": False,
        "actions": actions,
        "details": {
            "lifecycle_action": action,
            "service_name": service_name,
            "port": port,
        },
    }


def _format_remote_issue(issue: dict[str, Any]) -> str:
    unaffected = ", ".join(str(item) for item in issue.get("unaffected") or ())
    actions = "\n".join(
        f"  {index}. {action}"
        for index, action in enumerate(issue.get("actions") or (), start=1)
    )
    retry = (
        "HASHI will retry automatically."
        if issue.get("automatic_retry")
        else "HASHI attempted the default Remote lifecycle and kept local services running."
    )
    return (
        f"{issue['summary']}\n"
        f"Cause: {issue['cause']}\n"
        f"Impact: {issue['impact']} Unaffected: {unaffected}.\n"
        f"System response: {retry}\n"
        f"Action:\n{actions}\n"
        f"Diagnostic code: {issue['code']}"
    )


class StartupManager:
    """Initial agent selection, backend preflight, and startup banner orchestration."""

    def __init__(self, kernel, console_handler):
        self.kernel = kernel
        self.console_handler = console_handler
        self._startup_header_shown = False
        self._startup_animation_completed = False

    def _presentation_identity(self, global_cfg=None) -> tuple[str, object]:
        paths = getattr(self.kernel, "paths", None)
        config = global_cfg or getattr(self.kernel, "global_cfg", None) or getattr(
            self.kernel,
            "global_config",
            None,
        )
        instance_name = (
            getattr(config, "instance_id", None)
            or getattr(paths, "instance_id", None)
            or "HASHI"
        )
        instance_path = (
            getattr(paths, "bridge_home", None)
            or getattr(config, "bridge_home", None)
            or getattr(config, "project_root", None)
            or "-"
        )
        return str(instance_name), instance_path

    def _show_startup_header(
        self,
        global_cfg=None,
        *,
        include_branding: bool = True,
        styled: bool = False,
    ) -> None:
        if self._startup_header_shown:
            return
        from orchestrator.banner import show_startup_header

        instance_name, instance_path = self._presentation_identity(global_cfg)
        show_startup_header(
            instance_name=instance_name,
            instance_path=instance_path,
            audit_root=instance_path if instance_path != "-" else None,
            include_branding=include_branding,
            styled=styled,
        )
        self._startup_header_shown = True

    async def start_initial_agents(self, global_cfg, agent_configs, secrets) -> tuple[bool, dict]:
        selected_configs = [
            cfg for cfg in agent_configs
            if (self.kernel.selected_agents is not None and cfg.name in self.kernel.selected_agents)
            or (self.kernel.selected_agents is None and cfg.is_active)
        ]

        inactive_agent_names = [
            cfg.name for cfg in agent_configs
            if cfg.name not in [selected.name for selected in selected_configs]
        ]

        if not selected_configs and getattr(self.kernel, "_allow_empty_start", False):
            return True, {}

        if not selected_configs:
            print("\n" + "!" * 64)
            print("  CRITICAL ERROR: No active agents found.")
            print("  Please ensure at least one agent is set to 'is_active: true'")
            print("  in your 'agents.json' file.")
            print("!" * 64 + "\n")
            main_logger.critical("Aborting launch: No active agents configured.")
            return False, {}

        engine_status = self.kernel._check_backend_availability(global_cfg, selected_configs, secrets)
        startable_configs, skipped = self.kernel._partition_agents_by_availability(selected_configs, engine_status)
        initial_agent_names = [cfg.name for cfg in startable_configs]
        bridge_logger.info("Agents to start: %s", initial_agent_names)
        if skipped:
            for name, reason in skipped:
                bridge_logger.warning("Skipping agent '%s': %s", name, reason)

        try:
            _, wa_cfg = self.kernel._load_whatsapp_cfg()
        except Exception:
            wa_cfg = {}

        await self._ensure_remote_lifecycle(global_cfg)

        if not initial_agent_names:
            self._show_startup_header(global_cfg)
            print("\n" + "=" * 64)
            print("  CRITICAL ERROR: No agents can start.")
            print("  Reason: All backend engines (Gemini, Claude, etc.) are unavailable.")
            print("  Please check your 'secrets.json' for API keys and ensure")
            print("  CLI tools are installed as per the README.")
            print("=" * 64 + "\n")
            main_logger.critical("No agents can start - all backends are unavailable.")
            return False, wa_cfg

        await self._run_startup_banner(
            initial_agent_names,
            global_cfg,
            wa_cfg,
            skipped,
            inactive_agent_names,
        )

        if not self.kernel.runtimes:
            print("\n" + "*" * 64)
            print("  CRITICAL ERROR: All agents failed to connect to Telegram.")
            print("  Please check that:")
            print("  1. Your Bot Tokens in 'secrets.json' are correct.")
            print("  2. Your internet connection is active.")
            print("  3. The Telegram API is not being blocked.")
            print("*" * 64 + "\n")
            main_logger.critical("All agents failed to start. Exiting.")
            return False, wa_cfg

        return True, wa_cfg

    async def _ensure_remote_lifecycle(self, global_config=None) -> None:
        if global_config is None:
            global_config = getattr(self.kernel, "global_cfg", None) or getattr(
                self.kernel,
                "global_config",
                None,
            )
        portable_root = str(os.environ.get("HASHI_REMOTE_ROOT") or "").strip()
        root = portable_root or getattr(global_config, "project_root", None)
        instance_id = str(
            getattr(global_config, "instance_id", None)
            or getattr(getattr(self.kernel, "paths", None), "instance_id", None)
            or "HASHI"
        ).upper()
        try:
            remote_lifecycle = importlib.import_module(
                "orchestrator.remote_lifecycle"
            )
            result = await remote_lifecycle.ensure_remote_started(root)
        except Exception as exc:
            result = {
                "ok": False,
                "action": "lifecycle_check_failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "settings": None,
            }
        action = result.get("action")
        settings = result.get("settings")
        remote_status = {
            "available": bool(result.get("ok")),
            "enabled": bool(getattr(settings, "enabled", action != "skipped")),
            "supervised": bool(
                getattr(settings, "supervised", False)
                and action not in {"started_child", "started_child_fallback"}
                and not isinstance(result.get("supervisor_fallback"), dict)
            ),
            "supervisor_requested": bool(getattr(settings, "supervised", False)),
            "action": str(action or "unknown"),
            "port": getattr(settings, "port", None),
            "service_name": str(result.get("service_name") or ""),
        }
        setattr(self.kernel, "remote_lifecycle_status", remote_status)
        status = dict(getattr(self.kernel, "startup_status", {}) or {})
        status["remote"] = remote_status
        self.kernel.startup_status = status
        if result.get("ok"):
            bridge_logger.info(
                "Hashi Remote lifecycle: %s on port %s",
                action,
                remote_status["port"],
            )
            supervisor_fallback = result.get("supervisor_fallback")
            if isinstance(supervisor_fallback, dict):
                bridge_logger.warning(
                    "Hashi Remote is active for %s through the bundled child lifecycle, "
                    "but the OS supervisor could not be activated (%s). "
                    "Use /remote on|off to control Remote. For availability independent "
                    "of HASHI Core, register and enable the platform Remote supervisor.",
                    instance_id,
                    supervisor_fallback.get("reason") or "no detail",
                )
            supervisor_refresh = result.get("supervisor_refresh")
            if isinstance(supervisor_refresh, dict) and not supervisor_refresh.get("ok"):
                bridge_logger.warning(
                    "Hashi Remote is active for %s, but its OS supervisor registration "
                    "could not be refreshed (%s). Use /remote on to retry activation; "
                    "if needed, register and enable the platform Remote supervisor.",
                    instance_id,
                    supervisor_refresh.get("reason") or "no detail",
                )
            process = result.get("process")
            if process is not None:
                setattr(self.kernel, "_remote_lifecycle_process", process)
            return
        if action == "skipped":
            bridge_logger.info(
                "Hashi Remote lifecycle: %s (%s)",
                action,
                result.get("reason") or "disabled by configuration",
            )
            return

        issue = _remote_issue_from_result(result, instance_id)
        issues = list(status.get("issues") or ())
        if not any(item.get("code") == issue["code"] for item in issues):
            issues.append(issue)
        status["issues"] = issues
        status["degraded"] = True
        self.kernel.startup_status = status
        message = _format_remote_issue(issue)
        bridge_logger.warning(message)

    def _publish_startup_issues(self, issues: list[dict[str, Any]]) -> None:
        for issue in issues:
            if isinstance(issue, dict) and issue.get("component") == "remote":
                main_logger.warning(_format_remote_issue(issue))

    def _command_registry_notices(self) -> list[dict[str, Any]]:
        notices: dict[tuple[str, str], dict[str, Any]] = {}
        for runtime in getattr(self.kernel, "runtimes", ()):
            metadata = getattr(runtime, "metadata", {})
            if not isinstance(metadata, dict):
                continue
            for raw_notice in metadata.get("command_registry_notices") or ():
                if not isinstance(raw_notice, dict):
                    continue
                command = str(raw_notice.get("command") or "").strip()
                module = str(raw_notice.get("module") or "").strip()
                if not command or not module:
                    continue
                key = (command, module)
                current = notices.setdefault(
                    key,
                    {
                        "code": "protected_private_command_override",
                        "command": command,
                        "module": module,
                        "callbacks_ignored": False,
                    },
                )
                current["callbacks_ignored"] = bool(
                    current["callbacks_ignored"]
                    or raw_notice.get("callbacks_ignored")
                )
        return [notices[key] for key in sorted(notices)]

    def _publish_command_registry_notice(self, notices: list[dict[str, Any]]) -> None:
        if not notices:
            return
        affected = ", ".join(
            f"/{notice['command']} ({notice['module']})" for notice in notices
        )
        callbacks_ignored = any(
            notice.get("callbacks_ignored") for notice in notices
        )
        callback_result = (
            " Callbacks supplied by those protected overrides were also ignored."
            if callbacks_ignored
            else ""
        )
        message = (
            "Local command extension compatibility: ignored "
            f"{len(notices)} protected override(s): {affected}.\n"
            "Cause: those command names are protected because HASHI Core already provides them.\n"
            "Impact: the HASHI Core commands remain active; startup can continue."
            f"{callback_result}\n"
            "System response: the local command extension overrides were not loaded.\n"
            "Action: none is required unless those local command extensions still contain behavior you need; update or remove them only after checking other HASHI instances."
        )
        main_logger.info(message)
        bridge_logger.info(message)

    async def _run_startup_banner(self, initial_agent_names, global_cfg, wa_cfg, skipped, inactive_agent_names):
        boot_state = {name: "pending" for name in initial_agent_names}
        boot_reason = {}
        previous_status = dict(getattr(self.kernel, "startup_status", {}) or {})
        startup_progress = {
            "phase": "qualifying_generation",
            "ready": False,
            "degraded": bool(previous_status.get("issues")),
            "services_ready": False,
            "generation_id": None,
            "issues": list(previous_status.get("issues") or ()),
            "notices": list(previous_status.get("notices") or ()),
            "remote": previous_status.get("remote"),
            "agent_order": list(initial_agent_names),
            "agent_states": dict(boot_state),
            "agent_reasons": {},
            "skipped_agents": list(skipped),
        }
        started = getattr(self.kernel, "_startup_started_monotonic", time.monotonic())
        total = len(initial_agent_names)
        last_progress_signature: tuple[Any, ...] | None = None
        last_progress_at = started
        reported_stall_signature: tuple[Any, ...] | None = None

        def _publish_progress(*, report_stall: bool = False) -> None:
            nonlocal last_progress_signature, last_progress_at, reported_stall_signature
            now = time.monotonic()
            ready_agents = sum(
                state in {"online", "local"} for state in boot_state.values()
            )
            failed_agents = sum(
                state == "failed" for state in boot_state.values()
            )
            completed = ready_agents + failed_agents
            agent_percent = round((completed / total) * 100) if total else 100
            generation_ready = bool(startup_progress.get("generation_id"))
            overall_percent = min(
                90,
                (15 if generation_ready else 5)
                + round(75 * (completed / total if total else 1.0)),
            )
            snapshot = {
                **startup_progress,
                "agent_states": dict(boot_state),
                "agent_reasons": dict(boot_reason),
                "completed": completed,
                "ready_agents": ready_agents,
                "local_agents": sum(
                    state == "local" for state in boot_state.values()
                ),
                "failed_agents": failed_agents,
                "connecting_agents": sum(
                    state == "connecting" for state in boot_state.values()
                ),
                "pending_agents": sum(
                    state == "pending" for state in boot_state.values()
                ),
                "waiting_for": sorted(
                    name
                    for name, state in boot_state.items()
                    if state in {"pending", "connecting"}
                ),
                "total": total,
                "agent_percent": agent_percent,
                "percent": overall_percent,
                "elapsed_seconds": round(now - started, 1),
            }
            startup_progress.update(snapshot)
            self.kernel.startup_status = dict(snapshot)
            signature = (
                snapshot["phase"],
                snapshot.get("generation_id"),
                tuple(sorted(boot_state.items())),
            )
            if signature != last_progress_signature:
                last_progress_signature = signature
                last_progress_at = now
                reported_stall_signature = None
            elif (
                report_stall
                and now - last_progress_at >= STARTUP_STALL_NOTICE_SECONDS
                and reported_stall_signature != signature
            ):
                reported_stall_signature = signature
                bridge_logger.info(
                    "Startup is still waiting: phase=%s agents=%s/%s ready; "
                    "waiting_for=%s no_state_change=%.1fs elapsed=%.1fs",
                    snapshot["phase"],
                    ready_agents,
                    total,
                    ",".join(snapshot["waiting_for"]) or "service startup",
                    now - last_progress_at,
                    snapshot["elapsed_seconds"],
                )

        async def _prepare_initial_generation():
            _publish_progress()
            prepare = getattr(self.kernel.function_workers, "prepare_generation", None)
            if not callable(prepare):
                startup_progress["phase"] = "starting_workers"
                _publish_progress()
                return None, None
            pinned = getattr(self.kernel, "_startup_artifact", None)
            if pinned is not None:
                from orchestrator.function_worker_supervisor import verify_generation_artifact
                generation, generation_root = pinned
                await asyncio.to_thread(verify_generation_artifact, generation_root, generation)
            else:
                generation, generation_root = await prepare()
            startup_progress.update(
                {
                    "phase": "starting_workers",
                    "generation_id": generation.manifest.generation_id,
                }
            )
            _publish_progress()
            return generation, generation_root

        prepared_generation_task = asyncio.create_task(
            _prepare_initial_generation(),
            name="boot-function-generation",
        )
        startup_limit = max(1, min(INITIAL_AGENT_STARTUP_CONCURRENCY, len(initial_agent_names) or 1))
        startup_sem = asyncio.Semaphore(startup_limit)

        async def _start_initial_agent(agent_name: str):
            try:
                generation, generation_root = await asyncio.shield(
                    prepared_generation_task
                )
            except Exception as e:
                boot_state[agent_name] = "failed"
                boot_reason[agent_name] = f"{type(e).__name__}: {e}"
                _publish_progress()
                bridge_logger.error(
                    "%s: pending -> failed (generation: %s)",
                    agent_name,
                    e,
                )
                return agent_name, (False, str(e))
            async with startup_sem:
                boot_state[agent_name] = "connecting"
                bridge_logger.info("%s: pending -> connecting", agent_name)
                _publish_progress()
                try:
                    selected_generation, selected_root = getattr(self.kernel, "_startup_agent_artifacts", {}).get(
                        agent_name, (generation, generation_root))
                    if selected_generation is None:
                        ok, msg = await self.kernel.start_agent(agent_name)
                    else:
                        ok, msg = await self.kernel.start_agent(
                            agent_name,
                            generation=selected_generation,
                            generation_root=selected_root,
                        )
                except Exception as e:
                    main_logger.exception("Unexpected startup error for '%s': %s", agent_name, e)
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = f"{type(e).__name__}: {e}"
                    _publish_progress()
                    bridge_logger.error("%s: connecting -> failed (exception: %s)", agent_name, e)
                    return agent_name, (False, str(e))
                if ok:
                    new_state = "local" if "LOCAL MODE" in msg.upper() else "online"
                    snapshot = getattr(self.kernel.function_workers, "telegram_ingress_snapshot", None)
                    if (getattr(self.kernel, "_handoff_draining", False)
                            and callable(snapshot) and snapshot(agent_name).get("configured")):
                        # Worker readiness precedes shared Connector activation.
                        new_state = "online"
                    boot_state[agent_name] = new_state
                    if new_state == "local":
                        boot_reason[agent_name] = "Telegram unavailable"
                    bridge_logger.info("%s: connecting -> %s", agent_name, new_state)
                else:
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = msg
                    bridge_logger.error("%s: connecting -> failed (%s)", agent_name, msg)
                _publish_progress()
                return agent_name, (ok, msg)

        async def _progress_heartbeat() -> None:
            while True:
                await asyncio.sleep(STARTUP_PROGRESS_HEARTBEAT_SECONDS)
                if all(
                    state in {"online", "local", "failed"}
                    for state in boot_state.values()
                ):
                    return
                _publish_progress(report_stall=True)

        startup_tasks = [
            asyncio.create_task(_start_initial_agent(name), name=f"boot-{name}")
            for name in initial_agent_names
        ]
        progress_heartbeat = asyncio.create_task(
            _progress_heartbeat(),
            name="boot-progress-heartbeat",
        )

        from orchestrator.banner import StartupAnimationResult, show_startup_banner

        _, instance_path = self._presentation_identity(global_cfg)

        def _run_banner() -> StartupAnimationResult:
            return show_startup_banner(
                agent_names=initial_agent_names,
                boot_state=boot_state,
                workbench_port=global_cfg.workbench_port,
                wa_enabled=bool(wa_cfg.get("enabled")),
                api_gateway_enabled=self.kernel.enable_api_gateway,
                skipped_agents=skipped,
                logo_only=True,
                inactive_agents=inactive_agent_names,
                boot_reason=boot_reason,
                startup_progress=startup_progress,
                audit_root=instance_path if instance_path != "-" else None,
                fallback_to_static=False,
            )

        mute = AnimMute()
        if self.console_handler is not None:
            self.console_handler.addFilter(mute)

        try:
            results = await asyncio.gather(
                asyncio.get_running_loop().run_in_executor(None, _run_banner),
                *startup_tasks,
                return_exceptions=True,
            )
        finally:
            progress_heartbeat.cancel()
            await asyncio.gather(progress_heartbeat, return_exceptions=True)
            if self.console_handler is not None:
                self.console_handler.removeFilter(mute)

        animation_result = results[0]
        self._startup_animation_completed = bool(
            isinstance(animation_result, StartupAnimationResult)
            and animation_result.completed
        )
        if isinstance(animation_result, BaseException):
            from orchestrator.terminal_console import record_output_exception

            record_output_exception(
                purpose="startup_animation",
                sink="animation_executor",
                error=animation_result,
            )
        self._show_startup_header(
            global_cfg,
            include_branding=not self._startup_animation_completed,
            styled=self._startup_animation_completed,
        )

        issues = list(startup_progress.get("issues") or ())

        def _add_issue(issue: dict[str, Any]) -> None:
            if not any(
                isinstance(existing, dict)
                and existing.get("code") == issue["code"]
                for existing in issues
            ):
                issues.append(issue)

        failed_names = sorted(
            name for name, state in boot_state.items() if state == "failed"
        )
        if failed_names:
            _add_issue(
                {
                    "code": "agent_startup_failed",
                    "component": "function_workers",
                    "severity": "warning",
                    "summary": f"{len(failed_names)} configured agent(s) failed to start.",
                    "cause": "One or more Function Workers did not complete backend or Telegram startup.",
                    "impact": "The affected agents are unavailable; other started agents remain usable.",
                    "automatic_retry": False,
                    "actions": ["Review the per-agent startup errors and correct the reported provider or Telegram configuration."],
                    "details": {
                        "agents": failed_names,
                        "reasons": {
                            name: str(boot_reason.get(name) or "unknown")
                            for name in failed_names
                        },
                    },
                }
            )
        local_names = sorted(
            name for name, state in boot_state.items() if state == "local"
        )
        if local_names:
            _add_issue(
                {
                    "code": "agent_telegram_unavailable",
                    "component": "telegram",
                    "severity": "warning",
                    "summary": f"{len(local_names)} agent(s) started in local mode without Telegram.",
                    "cause": "Telegram startup was unavailable for the affected agents.",
                    "impact": "Workbench and other local surfaces remain usable for those agents, but Telegram does not.",
                    "automatic_retry": False,
                    "actions": ["Check the affected bot tokens and Telegram network access, then start those agents again."],
                    "details": {"agents": local_names},
                }
            )
        if skipped:
            skipped_names = sorted(str(name) for name, _reason in skipped)
            _add_issue(
                {
                    "code": "agent_backend_unavailable",
                    "component": "providers",
                    "severity": "warning",
                    "summary": f"{len(skipped_names)} configured agent(s) were skipped because their backend was unavailable.",
                    "cause": "Backend preflight rejected the affected agent configurations.",
                    "impact": "The skipped agents are unavailable; other started agents remain usable.",
                    "automatic_retry": False,
                    "actions": ["Correct the provider credentials or CLI availability reported for each skipped agent, then start them again."],
                    "details": {
                        "agents": skipped_names,
                        "reasons": {
                            str(name): str(reason) for name, reason in skipped
                        },
                    },
                }
            )
        startup_progress["issues"] = issues
        startup_progress["degraded"] = bool(issues)
        startup_progress["phase"] = (
            "agents_ready"
            if all(state == "online" for state in boot_state.values()) and not skipped
            else "agents_degraded"
        )
        _publish_progress()

        command_notices = self._command_registry_notices()
        if command_notices:
            startup_progress["notices"] = command_notices
            self.kernel.startup_status = dict(startup_progress)
        self._publish_startup_issues(list(startup_progress.get("issues") or ()))
        if command_notices:
            self._publish_command_registry_notice(command_notices)

        if skipped:
            for name, reason in skipped:
                main_logger.warning("Skipping agent '%s': %s", name, reason)

        for task in startup_tasks:
            if task.cancelled():
                continue
            try:
                _agent_name, (ok, message) = task.result()
                if not ok:
                    main_logger.error(message)
            except Exception as e:
                main_logger.error("Unexpected error reading startup task result: %s", e)

    @staticmethod
    def _runtime_is_online(handle, startup_state: str) -> bool:
        if handle is None:
            return False
        metadata = dict(getattr(handle, "metadata", {}) or {})
        client = getattr(handle, "client", None)
        process = getattr(client, "process", None)
        alive = True
        is_alive = getattr(process, "is_alive", None)
        if callable(is_alive):
            try:
                alive = bool(is_alive())
            except Exception:
                alive = False
        if bool(getattr(client, "closed", False)):
            alive = False
        if getattr(handle, "_offline_error", None):
            alive = False

        phase = str(metadata.get("worker_phase") or "").upper()
        accepting = metadata.get("worker_accepting")
        backend_ready = bool(getattr(handle, "backend_ready", True))
        if phase or accepting is not None:
            return (
                alive
                and backend_ready
                and phase == "ACTIVE"
                and bool(accepting)
            )
        return alive and startup_state in {"online", "local"}

    def reconcile_connector_status(self, errors=None, *, pending=False, publish=True) -> None:
        """Derive startup health from current ingress; retain unrelated failures."""
        status = dict(getattr(self.kernel, "startup_status", {}) or {})
        previous_issues = list(status.get("issues") or [])
        states = dict(status.get("agent_states") or {})
        reasons = dict(status.get("agent_reasons") or {})
        workers = self.kernel.function_workers
        snapshot = getattr(workers, "telegram_ingress_snapshot", None)
        unavailable = []
        connecting = []
        for name, handle in self.kernel._runtime_map().items():
            ingress = dict(snapshot(name) or {}) if callable(snapshot) else {}
            configured = ingress.get("configured", name in getattr(workers, "_telegram_ingress", {}))
            connected = bool(ingress.get("running") and ingress.get("connected")
                             and getattr(handle, "telegram_connected", False))
            if configured:
                if connected:
                    states[name] = "online"
                    reasons.pop(name, None)
                elif pending:
                    connecting.append(name)
                else:
                    unavailable.append(name)
                    states[name] = "local"
                    reasons[name] = "Telegram unavailable"
            elif states.get(name) == "local":
                unavailable.append(name)

        errors = dict(status.get("connector_errors") or {}) if errors is None else dict(errors)
        issues = [issue for issue in previous_issues if issue.get("code") not in {
            "agent_telegram_unavailable", "connector_activation"}]
        if unavailable:
            issues.append({
                "code": "agent_telegram_unavailable", "component": "telegram",
                "severity": "warning", "summary": f"{len(unavailable)} agent(s) have no active Telegram connection.",
                "automatic_retry": bool(any(name in getattr(workers, "_telegram_ingress", {}) for name in unavailable)),
                "details": {"agents": sorted(unavailable)},
            })
        if errors:
            issues.append({"code": "connector_activation", "component": "connectors",
                "severity": "warning", "summary": "Connectors are retrying activation",
                "details": errors, "automatic_retry": True})
        degraded = bool(status.get("failed_agents") or any(
            issue.get("severity") in {"warning", "error", "critical"} for issue in issues))
        status.update(issues=issues, connector_errors=errors, degraded=degraded,
            ready=not pending and not degraded,
            phase="connecting" if pending else "degraded" if degraded else "ready",
            agent_states=states, agent_reasons=reasons,
            connecting_agents=len(connecting), local_agents=len(unavailable),
            waiting_for=sorted(connecting))
        self.kernel.startup_status = status
        if pending or not publish:
            return
        signature = (tuple(sorted(states.items())), tuple(sorted(unavailable)),
                     tuple(sorted(errors)), tuple(issue.get("code") for issue in issues))
        if signature == getattr(self, "_connector_status_signature", None):
            return
        self._connector_status_signature = signature
        previous_codes = {issue.get("code") for issue in previous_issues}
        current_codes = {issue.get("code") for issue in issues}
        if not (unavailable or errors) and (previous_codes - current_codes) & {
                "agent_telegram_unavailable", "connector_activation"}:
            main_logger.info("Connector startup recovered.", extra={"terminal_safe": True})
            bridge_logger.info("Connector startup recovered.")
        if unavailable or errors:
            main_logger.warning("Connector startup is degraded; inspect /api/health for affected agents.",
                                extra={"terminal_safe": True})
        report = getattr(self.kernel, "_report_startup", None)
        if callable(report):
            report()

    def show_startup_status(self) -> None:
        """Render verified Worker, Telegram ingress, and live service state."""

        from orchestrator.banner import (
            AgentBannerStatus,
            service_banner_statuses,
            show_startup_status,
        )

        startup = dict(getattr(self.kernel, "startup_status", {}) or {})
        states = dict(startup.get("agent_states") or {})
        agent_order = [str(name) for name in startup.get("agent_order") or ()]
        runtime_map = dict(self.kernel._runtime_map())
        for name in runtime_map:
            if name not in agent_order:
                agent_order.append(name)

        workers = getattr(self.kernel, "function_workers", None)
        ingress_snapshot = getattr(workers, "telegram_ingress_snapshot", None)
        agent_rows = []
        for name in agent_order:
            handle = runtime_map.get(name)
            startup_state = str(states.get(name) or "").casefold()
            online = self._runtime_is_online(handle, startup_state)
            telegram_connected: bool | None = None
            if handle is not None:
                telegram_connected = bool(
                    getattr(handle, "telegram_connected", False)
                )
                if callable(ingress_snapshot):
                    try:
                        ingress = dict(ingress_snapshot(name) or {})
                    except Exception:
                        ingress = {}
                    telegram_connected = bool(
                        telegram_connected
                        and ingress.get("running")
                        and ingress.get("connected")
                    )
            agent_rows.append(
                AgentBannerStatus(
                    name=name,
                    state="ONLINE" if online else "FAILED",
                    telegram_connected=telegram_connected,
                )
            )

        registry = getattr(self.kernel, "endpoint_registry", None)
        snapshot = getattr(registry, "snapshot", None)
        try:
            services = dict((snapshot() if callable(snapshot) else {}).get("services") or {})
        except Exception:
            services = {}
        service_rows = service_banner_statuses(services)

        instance_name, instance_path = self._presentation_identity()
        show_startup_status(
            agents=agent_rows,
            services=service_rows,
            instance_name=instance_name,
            audit_root=instance_path if instance_path != "-" else None,
            styled=self._startup_animation_completed,
        )
