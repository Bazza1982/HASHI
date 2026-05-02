from __future__ import annotations
import os
import sys
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
    emit_bridge_audit,
    setup_bridge_file_logging,
    setup_console_logging,
)
from orchestrator.config_admin import ConfigAdmin
from orchestrator.instance_lock import InstanceLock
from orchestrator.lifecycle_state import LifecycleState
from orchestrator.onboarding_gate import run_onboarding_gate
from orchestrator.reboot_manager import RebootManager
from orchestrator.service_manager import ServiceManager
from orchestrator.shutdown_manager import ShutdownManager
from orchestrator.startup_manager import StartupManager
from orchestrator.whatsapp_manager import WhatsAppManager

# --- Global Orchestrator Setup ---
CODE_ROOT = Path(__file__).resolve().parent

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")  # file-only orchestrator log

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
        self.shutdown_manager = ShutdownManager(self)
        self.startup_manager = StartupManager(self, _handler)
        self.whatsapp_manager = WhatsAppManager(self)
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
        return self.whatsapp_manager.load_config()

    async def start_whatsapp_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        return await self.whatsapp_manager.start_transport(persist_enabled)

    async def stop_whatsapp_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        return await self.whatsapp_manager.stop_transport(persist_enabled)

    async def send_whatsapp_text(self, phone_number: str, text: str) -> tuple[bool, str]:
        return await self.whatsapp_manager.send_text(phone_number, text)

    async def _send_whatsapp_startup_notification(self, runtime):
        await self.whatsapp_manager.send_startup_notification(runtime)

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

        setup_bridge_file_logging(global_cfg, bridge_logger, logging.getLogger("BridgeU.Scheduler"))
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

        startup_ok, wa_cfg = await self.startup_manager.start_initial_agents(global_cfg, agent_configs, secrets)
        if not startup_ok:
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

            await self.shutdown_manager.full_shutdown()
            break

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", nargs="*", help="Only start the specified agents")
    parser.add_argument("--api-gateway", action="store_true", help="Enable the local OpenAI-compatible API gateway")
    parser.add_argument("--bridge-home", help="Override the bridge home directory (defaults to BRIDGE_HOME or the code root).")
    args = parser.parse_args()
    selected_agents = set(args.agents) if args.agents else None
    paths = build_bridge_paths(CODE_ROOT, bridge_home=args.bridge_home)

    if run_onboarding_gate(paths, CODE_ROOT):
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
