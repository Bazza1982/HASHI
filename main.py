from __future__ import annotations
import os
import sys
import json
import asyncio
import argparse
import logging
import signal
import traceback
from pathlib import Path
from datetime import datetime

from orchestrator.config import ConfigManager
from orchestrator.skill_manager import SkillManager
from orchestrator.pathing import BridgePaths, build_bridge_paths
from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.bootstrap_logging import (
    AnimMute as _AnimMute,
    emit_bridge_audit,
    setup_console_logging,
)
from orchestrator.config_admin import ConfigAdmin
from orchestrator.instance_lock import InstanceLock
from orchestrator.lifecycle_state import LifecycleState
from orchestrator.reboot_manager import RebootManager
from orchestrator.service_manager import ServiceManager

# --- Global Orchestrator Setup ---
CODE_ROOT = Path(__file__).resolve().parent

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")  # file-only orchestrator log

INITIAL_AGENT_STARTUP_CONCURRENCY = 2


_handler = setup_console_logging()


def _emit_bridge_audit(paths: BridgePaths | None, level: int, message: str):
    emit_bridge_audit(paths, level, message, bridge_logger)

class UniversalOrchestrator:
    def __init__(self, paths: BridgePaths, selected_agents: set[str] | None = None, enable_api_gateway: bool = False):
        self.paths = paths
        self.runtimes = []
        self.shutdown_event = asyncio.Event()
        self.selected_agents = selected_agents
        self.enable_api_gateway = enable_api_gateway
        self.global_cfg = None
        self.secrets = {}
        self.skill_manager = SkillManager(self.paths.code_root, self.paths.tasks_path)
        self.config_admin = ConfigAdmin(self.paths)
        self.backend_preflight = BackendPreflight()
        self.agent_lifecycle = AgentLifecycleManager(self)
        self.service_manager = ServiceManager(self)
        self.reboot_manager = RebootManager(self, _handler)
        self.workbench_api = None
        self.api_gateway = None
        self.scheduler = None
        self.agent_directory = None
        self.scheduler_task = None
        self.whatsapp = None
        self._lifecycle_lock = asyncio.Lock()
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._startup_tasks: dict[str, asyncio.Task] = {}
        self._restart_request: dict | None = None  # set by request_restart()
        self._shutdown_request = {
            "reason": "external",
            "source": "unknown",
            "detail": "",
            "requested_at": None,
        }
        self.lifecycle_state = LifecycleState()

    def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(
                    sig,
                    self.request_shutdown,
                    f"signal:{sig_name}",
                    "os-signal",
                    sig_name,
                )
                bridge_logger.info(f"Installed shutdown signal handler for {sig_name}")
            except (NotImplementedError, RuntimeError):
                continue

    def request_shutdown(self, reason: str = "external", source: str = "unknown", detail: str = ""):
        if self.shutdown_event.is_set():
            main_logger.info(f"Shutdown already requested; ignoring duplicate request ({reason}).")
            bridge_logger.warning(
                f"Duplicate shutdown request ignored ({self.lifecycle_state.shutdown_meta_text(self._shutdown_request)}) "
                f"new_reason={reason} new_source={source} new_detail={detail or '-'}"
            )
            return
        self._shutdown_request = {
            "reason": reason,
            "source": source,
            "detail": detail,
            "requested_at": datetime.now().isoformat(),
        }
        main_logger.info(f"Shutdown requested ({reason}).")
        bridge_logger.warning(f"Shutdown requested ({self.lifecycle_state.shutdown_meta_text(self._shutdown_request)})")
        self.lifecycle_state.record_shutdown_request(self._shutdown_request)
        self.shutdown_event.set()

    def request_restart(self, mode: str = "same", agent_name: str | None = None, agent_number: int | None = None):
        """Signal a hot restart. Modes: same, min, max, number."""
        self._restart_request = {"mode": mode, "agent_name": agent_name, "agent_number": agent_number}
        main_logger.info(f"Restart requested (mode={mode}, agent={agent_name}, number={agent_number}).")
        bridge_logger.warning(
            f"Restart requested (mode={mode}, agent={agent_name or '-'}, "
            f"number={agent_number if agent_number is not None else '-'}"
            ")"
        )
        self.shutdown_event.set()

    def _runtime_map(self):
        return {rt.name: rt for rt in self.runtimes}

    def _agent_lock(self, agent_name: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_name)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_name] = lock
        return lock

    def _load_raw_config(self) -> dict:
        return self.config_admin.load_raw_config()

    def _write_raw_config(self, raw_cfg: dict):
        self.config_admin.write_raw_config(raw_cfg)

    def _load_whatsapp_cfg(self) -> tuple[dict, dict]:
        raw_cfg = self._load_raw_config()
        wa_cfg = raw_cfg.get("global", {}).get("whatsapp", {}) or {}
        return raw_cfg, wa_cfg

    async def start_whatsapp_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        async with self._lifecycle_lock:
            if self.whatsapp is not None:
                return False, "WhatsApp transport is already running."

            try:
                raw_cfg, wa_cfg = self._load_whatsapp_cfg()
            except Exception as e:
                return False, f"Failed to load WhatsApp config: {e}"

            if persist_enabled and not wa_cfg.get("enabled"):
                raw_cfg.setdefault("global", {}).setdefault("whatsapp", {})
                raw_cfg["global"]["whatsapp"]["enabled"] = True
                try:
                    self._write_raw_config(raw_cfg)
                except Exception as e:
                    return False, f"Failed to persist WhatsApp enabled flag: {e}"
                wa_cfg = raw_cfg["global"]["whatsapp"]

            if self.global_cfg is None:
                try:
                    global_cfg, _, secrets = self._load_config_bundle()
                    self.global_cfg = global_cfg
                    self.secrets = secrets
                except Exception as e:
                    return False, f"Failed to load runtime configuration: {e}"
            else:
                global_cfg = self.global_cfg

            try:
                from transports.whatsapp import WhatsAppTransport

                self.whatsapp = WhatsAppTransport(self, global_cfg, wa_cfg)
                await self.whatsapp.start()
                main_logger.info(
                    "WhatsApp transport started. If this account is not paired yet, "
                    "scan the QR code in this bridge-u-f console window."
                )
                return True, (
                    "WhatsApp transport started. "
                    "If this is the first login, scan the QR code in the bridge-u-f console window."
                )
            except Exception as e:
                self.whatsapp = None
                main_logger.warning(f"WhatsApp transport failed to start: {e}")
                main_logger.debug(traceback.format_exc())
                return False, f"WhatsApp transport failed to start: {e}"

    async def stop_whatsapp_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        async with self._lifecycle_lock:
            config_note = ""
            if persist_enabled:
                try:
                    raw_cfg, wa_cfg = self._load_whatsapp_cfg()
                    if wa_cfg.get("enabled"):
                        raw_cfg.setdefault("global", {}).setdefault("whatsapp", {})
                        raw_cfg["global"]["whatsapp"]["enabled"] = False
                        self._write_raw_config(raw_cfg)
                    config_note = " Future startups will keep WhatsApp disabled."
                except Exception as e:
                    return False, f"Failed to persist WhatsApp disabled flag: {e}"

            if self.whatsapp is None:
                return True, f"WhatsApp transport is already stopped.{config_note}"

            try:
                await asyncio.wait_for(self.whatsapp.shutdown(), timeout=5.0)
            except Exception as e:
                self.whatsapp = None
                main_logger.warning(f"WhatsApp shutdown warning: {e}")
                return False, f"WhatsApp shutdown warning: {e}"

            self.whatsapp = None
            main_logger.info("WhatsApp transport stopped.")
            return True, f"WhatsApp transport stopped.{config_note}"

    async def send_whatsapp_text(self, phone_number: str, text: str) -> tuple[bool, str]:
        async with self._lifecycle_lock:
            if self.whatsapp is None:
                return False, "WhatsApp transport is not running."
            try:
                await self.whatsapp.send_text_to_number(phone_number, text)
                return True, f"Sent WhatsApp message to {phone_number}."
            except Exception as e:
                main_logger.warning(f"WhatsApp admin send failed for {phone_number}: {e}")
                return False, f"Failed to send WhatsApp message to {phone_number}: {e}"

    async def _send_whatsapp_startup_notification(self, runtime):
        """Send startup notification via WhatsApp when agent starts in local mode."""
        if self.whatsapp is None:
            return
        try:
            # Get the admin's phone number from WhatsApp config
            wa_cfg = self.global_cfg.__dict__.get("whatsapp", {}) if self.global_cfg else {}
            admin_numbers = wa_cfg.get("allowed_numbers", []) if isinstance(wa_cfg, dict) else []
            if not admin_numbers:
                main_logger.debug(f"No WhatsApp admin numbers configured for startup notification.")
                return
            
            display_name = getattr(runtime, "get_display_name", lambda: runtime.name)()
            emoji = getattr(runtime, "get_agent_emoji", lambda: "🤖")()
            message = (
                f"{emoji} {display_name} started in LOCAL MODE\n"
                f"⚠️ Telegram unavailable — using Workbench + WhatsApp\n"
                f"Use /agent to check status"
            )
            
            for phone in admin_numbers[:1]:  # Send to first admin only
                try:
                    await self.whatsapp.send_text_to_number(phone, message)
                    main_logger.info(f"Sent WhatsApp startup notification for '{runtime.name}' to {phone}")
                    break
                except Exception as e:
                    main_logger.warning(f"Failed to send WhatsApp startup notification: {e}")
        except Exception as e:
            main_logger.debug(f"WhatsApp startup notification skipped: {e}")

    def _load_config_bundle(self):
        from orchestrator.config import ConfigManager
        cfg_mgr = ConfigManager(self.paths.config_path, self.paths.secrets_path, bridge_home=self.paths.bridge_home)
        global_cfg, agent_configs, secrets = cfg_mgr.load()
        self.global_cfg = global_cfg
        self.secrets = secrets
        return global_cfg, agent_configs, secrets

    def get_all_agents_raw(self) -> list[dict]:
        return self.config_admin.get_all_agents_raw()

    def set_agent_active(self, agent_name: str, active: bool) -> bool:
        return self.config_admin.set_agent_active(agent_name, active)

    def delete_agent_from_config(self, agent_name: str) -> bool:
        return self.config_admin.delete_agent_from_config(agent_name)

    def add_agent_to_config(self, agent_name: str, agent_cfg: dict | str | None = None, token: str | None = None):
        return self.config_admin.add_agent_to_config(agent_name, agent_cfg, token)

    def configured_agent_names(self) -> list[str]:
        return self.config_admin.configured_agent_names()

    def get_startable_agent_names(self, exclude_name: str | None = None) -> list[str]:
        return self.config_admin.get_startable_agent_names(
            running=set(self._runtime_map()),
            starting=set(self._startup_tasks),
            exclude_name=exclude_name,
        )

    def _check_backend_availability(self, global_cfg, agent_configs, secrets) -> dict[str, tuple[bool, str]]:
        return self.backend_preflight.check_backend_availability(global_cfg, agent_configs, secrets)

    def _partition_agents_by_availability(
        self, agent_configs, engine_status: dict[str, tuple[bool, str]]
    ) -> tuple[list, list[tuple[str, str]]]:
        return self.backend_preflight.partition_agents_by_availability(agent_configs, engine_status)

    def _build_runtime(self, agent_cfg, global_cfg, secrets):
        return self.agent_lifecycle.build_runtime(agent_cfg, global_cfg, secrets)

    async def _cleanup_runtime_start_failure(self, rt):
        await self.agent_lifecycle.cleanup_runtime_start_failure(rt)

    async def telegram_preflight(self, token: str, agent_name: str, attempt: int = 0, max_attempts: int = 0) -> bool:
        return await self.agent_lifecycle.telegram_preflight(token, agent_name, attempt, max_attempts)

    async def _start_runtime(self, rt) -> tuple[bool, str]:
        return await self.agent_lifecycle.start_runtime(rt)

    async def _try_telegram_connect(self, rt) -> bool:
        return await self.agent_lifecycle.try_telegram_connect(rt)

    async def start_agent(self, agent_name: str) -> tuple[bool, str]:
        return await self.agent_lifecycle.start_agent(agent_name)

    async def stop_agent(self, agent_name: str, reason: str = "manual-stop") -> tuple[bool, str]:
        return await self.agent_lifecycle.stop_agent(agent_name, reason)

    async def _teardown_runtime(self, runtime, timeout: float = 10.0):
        await self.agent_lifecycle.teardown_runtime(runtime, timeout)

    async def _shutdown_all_agents(self, timeout: float = 30.0):
        await self.agent_lifecycle.shutdown_all_agents(timeout)

    def _rebuild_hot_managers(self):
        self.reboot_manager.rebuild_hot_managers()

    def _reload_project_modules(self):
        self.reboot_manager.reload_project_modules()

    async def _do_hot_restart(self, restart: dict):
        await self.reboot_manager.hot_restart(restart)

    async def run(self):
        try:
            global_cfg, agent_configs, secrets = self._load_config_bundle()
        except Exception as e:
            main_logger.critical(f"Failed to load configuration: {e}")
            return

        # ── Set up bridge.log (orchestrator-level, file-only) ───────────
        _bridge_log_path = global_cfg.base_logs_dir / "bridge.log"
        global_cfg.base_logs_dir.mkdir(parents=True, exist_ok=True)
        _bl_handler = logging.FileHandler(_bridge_log_path, encoding="utf-8")
        _bl_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        bridge_logger.handlers.clear()
        bridge_logger.setLevel(logging.DEBUG)
        bridge_logger.propagate = False
        bridge_logger.addHandler(_bl_handler)
        # Also route scheduler logs to bridge.log
        _sched_logger = logging.getLogger("BridgeU.Scheduler")
        _sched_logger.handlers.clear()
        _sched_logger.setLevel(logging.DEBUG)
        _sched_logger.propagate = False
        _sched_logger.addHandler(_bl_handler)
        self.global_cfg = global_cfg
        self.secrets = secrets
        self.lifecycle_state.state_path = global_cfg.base_logs_dir / "orchestrator_state.json"
        bridge_logger.info("=== Bridge starting ===")
        previous_state, unexpected_previous_exit = self.lifecycle_state.mark_started(os.getpid())
        if unexpected_previous_exit:
            bridge_logger.error(
                "Previous bridge session ended unexpectedly "
                f"(pid={previous_state.get('pid', '?')} "
                f"started_at={previous_state.get('last_started_at', '?')} "
                f"pending_reason={previous_state.get('pending_shutdown_reason') or '-'} "
                f"pending_source={previous_state.get('pending_shutdown_source') or '-'} "
                f"last_exit_phase={previous_state.get('last_exit_phase') or '-'})"
            )
        self._install_signal_handlers()
        bridge_logger.info(
            "Process bootstrap: "
            f"pid={os.getpid()} ppid={os.getppid()} exe={sys.executable} cwd={Path.cwd()} "
            f"code_root={self.paths.code_root} bridge_home={self.paths.bridge_home} "
            f"config={self.paths.config_path}"
        )

        # Filter to selected agents
        selected_configs = [
            cfg for cfg in agent_configs
            if (self.selected_agents is not None and cfg.name in self.selected_agents) or
               (self.selected_agents is None and cfg.is_active)
        ]
        
        # Collect inactive agents for UI display
        inactive_agent_names = [
            cfg.name for cfg in agent_configs
            if cfg.name not in [c.name for c in selected_configs]
        ]
        
        if not selected_configs:
            print("\n" + "!" * 64)
            print("  CRITICAL ERROR: No active agents found.")
            print("  Please ensure at least one agent is set to 'is_active: true'")
            print("  in your 'agents.json' file.")
            print("!" * 64 + "\n")
            main_logger.critical("Aborting launch: No active agents configured.")
            return

        # --- Backend preflight check ---
        engine_status = self._check_backend_availability(global_cfg, selected_configs, secrets)
        startable_configs, skipped = self._partition_agents_by_availability(selected_configs, engine_status)
        initial_agent_names = [cfg.name for cfg in startable_configs]
        bridge_logger.info(f"Agents to start: {initial_agent_names}")
        if skipped:
            for name, reason in skipped:
                bridge_logger.warning(f"Skipping agent '{name}': {reason}")

        try:
            _, wa_cfg = self._load_whatsapp_cfg()
        except Exception:
            wa_cfg = {}

        if not initial_agent_names:
            print("\n" + "=" * 64)
            print("  CRITICAL ERROR: No agents can start.")
            print("  Reason: All backend engines (Gemini, Claude, etc.) are unavailable.")
            print("  Please check your 'secrets.json' for API keys and ensure")
            print("  CLI tools are installed as per the README.")
            print("=" * 64 + "\n")
            main_logger.critical(
                "No agents can start — all backends are unavailable."
            )
            return

        # ── Concurrent startup: agents boot while animation plays ─────────────
        # boot_state is a plain dict written by agent tasks (CPython GIL makes
        # simple key writes safe from a thread) and read by the banner thread.
        boot_state    = {name: "pending" for name in initial_agent_names}
        boot_reason   = {}  # last failure reason per agent, shown in banner
        startup_limit = max(1, min(INITIAL_AGENT_STARTUP_CONCURRENCY, len(initial_agent_names) or 1))
        startup_sem   = asyncio.Semaphore(startup_limit)

        async def _start_initial_agent(agent_name: str):
            async with startup_sem:
                boot_state[agent_name] = "connecting"
                bridge_logger.info(f"{agent_name}: pending → connecting")
                try:
                    ok, msg = await self.start_agent(agent_name)
                except Exception as e:
                    main_logger.exception(f"Unexpected startup error for '{agent_name}': {e}")
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = f"{type(e).__name__}: {e}"
                    bridge_logger.error(f"{agent_name}: connecting → failed (exception: {e})")
                    return agent_name, (False, str(e))
                if ok:
                    # Distinguish online (Telegram connected) from local (Telegram unavailable)
                    new_state = "local" if "LOCAL MODE" in msg.upper() else "online"
                    boot_state[agent_name] = new_state
                    if new_state == "local":
                        boot_reason[agent_name] = "Telegram unavailable"
                    bridge_logger.info(f"{agent_name}: connecting → {new_state}")
                else:
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = msg
                    bridge_logger.error(f"{agent_name}: connecting → failed ({msg})")
                return agent_name, (ok, msg)

        # Create all tasks first — they are queued and begin running as soon as
        # the event loop gets control (i.e. the moment we await anything below).
        startup_tasks = [
            asyncio.create_task(_start_initial_agent(name), name=f"boot-{name}")
            for name in initial_agent_names
        ]

        # Suppress console log output during the animation so log lines don't
        # corrupt the terminal.  Log records are not lost — file handlers still
        # receive them; only the console StreamHandler is muted here.
        _mute = _AnimMute()
        _handler.addFilter(_mute)

        from orchestrator.banner import show_startup_banner

        def _run_banner():
            show_startup_banner(
                agent_names=initial_agent_names,
                boot_state=boot_state,
                workbench_port=global_cfg.workbench_port,
                wa_enabled=bool(wa_cfg.get("enabled")),
                api_gateway_enabled=self.enable_api_gateway,
                skipped_agents=skipped,
                inactive_agents=inactive_agent_names,
                boot_reason=boot_reason,
            )

        try:
            # run_in_executor puts the animation in a thread-pool thread so
            # time.sleep() inside it does NOT block the asyncio event loop.
            # asyncio.gather waits for BOTH the animation AND all agent tasks.
            await asyncio.gather(
                asyncio.get_running_loop().run_in_executor(None, _run_banner),
                *startup_tasks,
                return_exceptions=True,
            )
        finally:
            _handler.removeFilter(_mute)

        # Emit deferred log messages now that the console is restored
        if skipped:
            for name, reason in skipped:
                main_logger.warning(f"Skipping agent '{name}': {reason}")

        for task in startup_tasks:
            if task.cancelled():
                continue
            try:
                agent_name, (ok, message) = task.result()
                if not ok:
                    main_logger.error(message)
            except Exception:
                pass

        if not self.runtimes:
            print("\n" + "*" * 64)
            print("  CRITICAL ERROR: All agents failed to connect to Telegram.")
            print("  Please check that:")
            print("  1. Your Bot Tokens in 'secrets.json' are correct.")
            print("  2. Your internet connection is active.")
            print("  3. The Telegram API is not being blocked.")
            print("*" * 64 + "\n")
            main_logger.critical("All agents failed to start. Exiting.")
            return

        main_logger.info("Universal Orchestrator is online. Awaiting messages.")

        await self.service_manager.start_runtime_services(global_cfg, secrets)

        try:
            _, wa_cfg = self._load_whatsapp_cfg()
        except Exception:
            wa_cfg = {}
        if wa_cfg.get("enabled"):
            ok, message = await self.start_whatsapp_transport(persist_enabled=False)
            if not ok:
                main_logger.warning(message)

        # --- Main event loop: supports hot restart ---
        while True:
            try:
                await self.shutdown_event.wait()
            except asyncio.CancelledError:
                main_logger.info("Shutdown signal received.")
                bridge_logger.warning(
                    f"Shutdown wait interrupted ({self.lifecycle_state.shutdown_meta_text(self._shutdown_request)})"
                )

            restart = self._restart_request
            self._restart_request = None

            if restart is not None:
                # --- Hot restart: stop agents only, keep services alive ---
                await self._do_hot_restart(restart)
                self.shutdown_event.clear()
                continue

            # --- Full shutdown ---
            main_logger.info("Shutting down active agents...")
            bridge_logger.warning(
                f"Full shutdown begin ({self.lifecycle_state.shutdown_meta_text(self._shutdown_request)}) "
                f"active_agents={len(self.runtimes)} "
                f"workbench={'on' if self.workbench_api is not None else 'off'} "
                f"api_gateway={'on' if self.api_gateway is not None else 'off'} "
                f"whatsapp={'on' if self.whatsapp is not None else 'off'}"
            )
            await self.service_manager.stop_runtime_services()
            if self.whatsapp is not None:
                bridge_logger.info("Stopping WhatsApp transport")
                try:
                    await asyncio.wait_for(self.whatsapp.shutdown(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    main_logger.warning(f"WhatsApp shutdown warning: {e}")
                    bridge_logger.warning(f"WhatsApp shutdown warning: {type(e).__name__}: {e}")
                self.whatsapp = None
            await self._shutdown_all_agents()
            self.lifecycle_state.mark_shutdown(
                self._shutdown_request,
                clean=True,
                phase="python-cleanup-complete",
            )
            bridge_logger.warning(
                f"Full shutdown complete ({self.lifecycle_state.shutdown_meta_text(self._shutdown_request)})"
            )

            # All Python-side cleanup is done. Start watchdog for the
            # asyncio.run() executor-shutdown phase: neonize's Go DLL runs
            # in asyncio.to_thread() workers that cannot be cancelled, so
            # shutdown_default_executor() blocks indefinitely.  Give it 5s
            # then force exit — everything important is already torn down.
            import threading as _threading
            def _exit_watchdog():
                import time as _time
                _time.sleep(5)
                msg = "Shutdown watchdog: forcing exit (Go runtime threads did not stop)."
                main_logger.warning(msg)
                _emit_bridge_audit(self.paths, logging.WARNING, msg)
                os._exit(0)
            _threading.Thread(target=_exit_watchdog, daemon=True, name="exit-watchdog").start()
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", nargs="*", help="Only start the specified agents")
    parser.add_argument("--api-gateway", action="store_true", help="Enable the local OpenAI-compatible API gateway")
    parser.add_argument("--bridge-home", help="Override the bridge home directory (defaults to BRIDGE_HOME or the code root).")
    args = parser.parse_args()
    selected_agents = set(args.agents) if args.agents else None
    paths = build_bridge_paths(CODE_ROOT, bridge_home=args.bridge_home)

    # Onboarding gate: if onboarding not complete, redirect to onboarding
    _agents_path = paths.bridge_home / "agents.json"
    _onboarding_done = False
    try:
        with open(_agents_path, encoding="utf-8") as _f:
            _cfg = json.load(_f)
            if _cfg.get("agents"):
                _onboarding_done = True
    except Exception:
        pass
    if not _onboarding_done:
        print("\033[38;5;180mOnboarding required. Starting onboarding program...\033[0m")
        import subprocess as _sp
        _sp.run([sys.executable, str(CODE_ROOT / "onboarding" / "onboarding_main.py")])
        sys.exit(0)

    orchestrator = UniversalOrchestrator(paths=paths, selected_agents=selected_agents, enable_api_gateway=args.api_gateway)
    lock = InstanceLock(paths.lock_path)
    try:
        lock.acquire()
        bootstrap_msg = (
            "Process bootstrap: "
            f"pid={os.getpid()} ppid={os.getppid()} exe={sys.executable} cwd={Path.cwd()} "
            f"code_root={paths.code_root} bridge_home={paths.bridge_home} config={paths.config_path}"
        )
        main_logger.info(bootstrap_msg)
        _emit_bridge_audit(paths, logging.INFO, bootstrap_msg)
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        msg = "KeyboardInterrupt received. Exiting."
        main_logger.info(msg)
        _emit_bridge_audit(paths, logging.WARNING, msg)
    except Exception as e:
        crash_msg = f"Fatal crash: {type(e).__name__}: {e}"
        main_logger.critical(f"{crash_msg}\n{traceback.format_exc()}")
        _emit_bridge_audit(paths, logging.CRITICAL, crash_msg)
    finally:
        lock.release()
    # If asyncio.run() returned but Go runtime threads are still alive, kill them.
    os._exit(0)
