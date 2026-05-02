from __future__ import annotations
import os
import sys
import json
import shutil
import asyncio
import argparse
import logging
import signal
import traceback
from pathlib import Path
from contextlib import suppress
from datetime import datetime

import httpx

from orchestrator.config import ConfigManager
from orchestrator.agent_runtime import BridgeAgentRuntime
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.scheduler import TaskScheduler
from orchestrator.skill_manager import SkillManager
from orchestrator.workbench_api import WorkbenchApiServer
from orchestrator.api_gateway import APIGatewayServer
from orchestrator.flexible_backend_registry import get_secret_lookup_order
from orchestrator.pathing import BridgePaths, build_bridge_paths
from orchestrator.bootstrap_logging import (
    C_RESET,
    C_STOP,
    AnimMute as _AnimMute,
    emit_bridge_audit,
    setup_console_logging,
)
from orchestrator.instance_lock import InstanceLock
from orchestrator.lifecycle_state import LifecycleState
from adapters.registry import get_backend_class

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
        return json.loads(self.paths.config_path.read_text(encoding="utf-8-sig"))

    def _write_raw_config(self, raw_cfg: dict):
        self.paths.config_path.write_text(
            json.dumps(raw_cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8-sig",
            newline="\r\n",
        )

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
        raw = json.loads(self.paths.config_path.read_text(encoding="utf-8-sig"))
        return raw.get("agents", [])

    def set_agent_active(self, agent_name: str, active: bool) -> bool:
        """Toggle is_active for an agent. Returns True if found and updated."""
        raw = self._load_raw_config()
        for ag in raw.get("agents", []):
            if ag.get("name") == agent_name:
                ag["is_active"] = active
                self._write_raw_config(raw)
                return True
        return False

    def delete_agent_from_config(self, agent_name: str) -> bool:
        """Remove an agent entry from config. Returns True if found and removed."""
        raw = self._load_raw_config()
        agents = raw.get("agents", [])
        orig_len = len(agents)
        raw["agents"] = [ag for ag in agents if ag.get("name") != agent_name]
        if len(raw["agents"]) < orig_len:
            self._write_raw_config(raw)
            return True
        return False

    def add_agent_to_config(self, agent_name: str, agent_cfg: dict | None = None) -> bool:
        """Add a new flex agent to config, create workspace dir + AGENT.md scaffold.
        Returns True on success, False if agent already exists."""
        raw = self._load_raw_config()
        existing_names = {ag.get("name") for ag in raw.get("agents", [])}
        if agent_name in existing_names:
            return False

        ws_dir = self.paths.workspaces_root / agent_name
        ws_dir.mkdir(parents=True, exist_ok=True)

        agent_md = ws_dir / "AGENT.md"
        if not agent_md.exists():
            agent_md.write_text(f"# {agent_name}\n\nNew HASHI agent.\n", encoding="utf-8")

        new_entry = agent_cfg or {}
        new_entry.setdefault("name", agent_name)
        new_entry.setdefault("workspace_dir", f"workspaces/{agent_name}")
        new_entry.setdefault("system_md", f"workspaces/{agent_name}/AGENT.md")
        new_entry.setdefault("is_active", True)
        new_entry.setdefault("type", "flex")

        raw.setdefault("agents", []).append(new_entry)
        self._write_raw_config(raw)
        return True

    def configured_agent_names(self) -> list[str]:
        raw = json.loads(self.paths.config_path.read_text(encoding="utf-8-sig"))
        return [agent["name"] for agent in raw.get("agents", []) if agent.get("is_active", True)]

    def get_startable_agent_names(self, exclude_name: str | None = None) -> list[str]:
        running = set(self._runtime_map())
        starting = set(self._startup_tasks)
        return [
            name for name in self.configured_agent_names()
            if name not in running and name not in starting and name != exclude_name
        ]

    def _has_openrouter_api_key(self, agent_configs, secrets) -> bool:
        for cfg in agent_configs:
            for secret_key in get_secret_lookup_order("openrouter-api", getattr(cfg, "name", "")):
                if secrets.get(secret_key):
                    return True
        return False

    def _check_backend_availability(self, global_cfg, agent_configs, secrets) -> dict[str, tuple[bool, str]]:
        """
        Check which backend engines are available. Returns {engine: (available, reason)}.

        CLI backends: check shutil.which().
        openrouter-api: check that the API key exists in secrets.
        """
        engines = set()
        for cfg in agent_configs:
            if hasattr(cfg, "allowed_backends"):  # flex agent
                for b in cfg.allowed_backends:
                    engines.add(b.get("engine", ""))
            engines.add(getattr(cfg, "engine", "") or getattr(cfg, "active_backend", ""))
        engines.discard("")

        result = {}
        cli_map = {
            "gemini-cli": global_cfg.gemini_cmd,
            "claude-cli": global_cfg.claude_cmd,
            "codex-cli": global_cfg.codex_cmd,
        }
        for engine in engines:
            if engine in cli_map:
                cmd = cli_map[engine]
                found = shutil.which(cmd)
                if found:
                    result[engine] = (True, found)
                else:
                    result[engine] = (False, f"'{cmd}' not found on PATH")
            elif engine == "openrouter-api":
                if self._has_openrouter_api_key(agent_configs, secrets):
                    result[engine] = (True, "API key present")
                else:
                    result[engine] = (False, "no API key in secrets.json")
            else:
                result[engine] = (True, "unknown engine, assuming available")
        return result

    def _partition_agents_by_availability(
        self, agent_configs, engine_status: dict[str, tuple[bool, str]]
    ) -> tuple[list, list[tuple[str, str]]]:
        """
        Split agents into (startable_configs, skipped_list).
        skipped_list is [(agent_name, reason), ...].
        For flex agents: startable if active_backend is available OR any allowed_backend is.
        """
        startable = []
        skipped = []
        for cfg in agent_configs:
            if hasattr(cfg, "allowed_backends"):  # flex
                active_ok, _ = engine_status.get(cfg.active_backend, (False, "unknown"))
                if active_ok:
                    startable.append(cfg)
                    continue
                # Try fallback to any available backend
                fallback = None
                for b in cfg.allowed_backends:
                    eng = b.get("engine", "")
                    ok, _ = engine_status.get(eng, (False, ""))
                    if ok:
                        fallback = eng
                        break
                if fallback:
                    main_logger.info(
                        f"Flex agent '{cfg.name}': active backend '{cfg.active_backend}' unavailable, "
                        f"will start with '{fallback}' instead."
                    )
                    cfg = type(cfg)(**{**cfg.__dict__, "active_backend": fallback})
                    startable.append(cfg)
                else:
                    reasons = [
                        f"{b.get('engine')}: {engine_status.get(b.get('engine', ''), (False, '?'))[1]}"
                        for b in cfg.allowed_backends
                    ]
                    skipped.append((cfg.name, f"no available backend ({', '.join(reasons)})"))
            else:  # fixed
                engine = cfg.engine
                ok, reason = engine_status.get(engine, (False, "unknown engine"))
                if ok:
                    startable.append(cfg)
                else:
                    skipped.append((cfg.name, f"{engine}: {reason}"))
        return startable, skipped

    def _build_runtime(self, agent_cfg, global_cfg, secrets):
        # Local imports so hot restart picks up reloaded module code.
        from orchestrator.agent_runtime import BridgeAgentRuntime as _BridgeRT
        from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime as _FlexRT
        from adapters.registry import get_backend_class as _get_backend

        if agent_cfg.type in {"flex", "limited"}:
            token = secrets.get(agent_cfg.telegram_token_key)
            if not token:
                main_logger.warning(
                    f"No Telegram token found for flex agent '{agent_cfg.name}' (key: {agent_cfg.telegram_token_key})."
                )
                token = "WORKBENCH_ONLY_NO_TOKEN"
            runtime = _FlexRT(agent_cfg, global_cfg, token, secrets, self.skill_manager)
        else:
            token = secrets.get(agent_cfg.name)
            api_key = secrets.get(f"{agent_cfg.engine}_key", None)
            if not token:
                main_logger.warning(f"No Telegram token found for agent '{agent_cfg.name}'.")
                token = "WORKBENCH_ONLY_NO_TOKEN"
            backend = _get_backend(agent_cfg.engine)(agent_cfg, global_cfg, api_key)
            runtime = _BridgeRT(agent_cfg.name, backend, token, self.skill_manager, secrets=secrets)
        runtime.orchestrator = self
        runtime.bind_handlers()
        return runtime

    async def _cleanup_runtime_start_failure(self, rt):
        with suppress(Exception):
            await rt.app.shutdown()
        if hasattr(rt, "backend"):
            with suppress(Exception):
                await rt.backend.shutdown()
        else:
            with suppress(Exception):
                await rt.shutdown()

    async def telegram_preflight(self, token: str, agent_name: str, attempt: int = 0, max_attempts: int = 0) -> bool:
        url = f"https://api.telegram.org/bot{token}/getMe"
        attempt_tag = f" (attempt {attempt}/{max_attempts})" if attempt else ""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    msg = f"Telegram preflight failed for '{agent_name}'{attempt_tag}: API returned not ok."
                    main_logger.error(msg)
                    bridge_logger.error(msg)
                    return False
                bridge_logger.info(f"Telegram preflight OK for '{agent_name}'{attempt_tag}")
                return True
        except Exception as e:
            err_text = str(e) or "<no error message>"
            msg = f"Telegram preflight failed for '{agent_name}'{attempt_tag}: {type(e).__name__}: {err_text}"
            main_logger.warning(msg)
            bridge_logger.warning(msg)
            return False

    async def _start_runtime(self, rt) -> tuple[bool, str]:
        """
        Start an agent runtime. Backend initialization failure is fatal.
        Telegram connection failure results in LOCAL MODE (Workbench + WhatsApp only).
        """
        # Stage 1: Backend initialization (required)
        bridge_logger.info(f"{rt.name}: starting backend initialization")
        for attempt in range(1, 4):
            try:
                main_logger.info(f"Initializing backend for '{rt.name}' (attempt {attempt}/3)...")
                if hasattr(rt, "backend"):
                    backend_ok = await rt.backend.initialize()
                    if not backend_ok:
                        await rt.backend.shutdown()
                        bridge_logger.error(f"{rt.name}: backend.initialize() returned False")
                        return False, f"Backend for '{rt.name}' failed to initialize."
                else:
                    backend_ok = await rt.initialize()
                    if not backend_ok:
                        await rt.shutdown()
                        bridge_logger.error(f"{rt.name}: flex initialize() returned False")
                        return False, f"Flex initialization for '{rt.name}' failed."
                rt.backend_ready = True
                bridge_logger.info(f"{rt.name}: backend ready (attempt {attempt}/3)")
                break
            except Exception as e:
                bridge_logger.warning(f"{rt.name}: backend init attempt {attempt}/3 failed: {type(e).__name__}: {e}")
                if attempt < 3:
                    main_logger.warning(
                        f"Backend init for '{rt.name}' attempt {attempt} failed: {e}. Retrying in 5s..."
                    )
                    await self._cleanup_runtime_start_failure(rt)
                    await asyncio.sleep(5)
                    continue
                if hasattr(rt, "error_logger"):
                    rt.error_logger.exception(f"Backend startup error for '{rt.name}' after 3 attempts: {e}")
                else:
                    main_logger.error(f"Backend startup error for '{rt.name}' after 3 attempts: {e}")
                bridge_logger.error(f"{rt.name}: backend init FAILED after 3 attempts")
                return False, f"Failed to start '{rt.name}': {e}"

        # Stage 2: Telegram connection (optional — local mode if fails)
        bridge_logger.info(f"{rt.name}: starting Telegram connection")
        telegram_ok = await self._try_telegram_connect(rt)

        # Stage 3: Finalize startup
        rt.startup_success = True
        if hasattr(rt, "prepare_post_start_state"):
            rt.prepare_post_start_state()

        if telegram_ok:
            bridge_logger.info(f"{rt.name}: ONLINE (backend + Telegram)")
            main_logger.info(f"Bot '{rt.name}' is online.")
            return True, f"Started agent '{rt.name}'."
        else:
            bridge_logger.warning(f"{rt.name}: LOCAL MODE (backend ok, Telegram failed)")
            main_logger.info(f"Bot '{rt.name}' started in LOCAL MODE (Telegram unavailable).")
            return True, f"Started '{rt.name}' in LOCAL MODE (Workbench + WhatsApp only)."

    async def _try_telegram_connect(self, rt) -> bool:
        """
        Attempt to connect Telegram. Returns True on success, False on failure.
        Does NOT block agent startup — local mode will be used if this fails.
        """
        last_failure_reason = ""
        for attempt in range(1, 4):
            preflight_ok = await self.telegram_preflight(rt.token, rt.name, attempt=attempt, max_attempts=3)
            if not preflight_ok:
                last_failure_reason = f"preflight {attempt}/3 failed"
                if attempt < 3:
                    bridge_logger.info(f"{rt.name}: {last_failure_reason}, retrying in 5s...")
                    await asyncio.sleep(5)
                    continue
                # All attempts failed — continue in local mode
                msg = (
                    f"⚠️ Telegram unavailable for '{rt.name}'. "
                    f"Agent will run in LOCAL MODE (Workbench + WhatsApp only)."
                )
                main_logger.warning(msg)
                bridge_logger.warning(f"{rt.name}: all 3 preflight attempts failed → LOCAL MODE")
                rt.telegram_connected = False
                return False

            try:
                main_logger.info(f"Connecting Telegram for '{rt.name}'...")
                await rt.app.initialize()
                await rt.app.start()
                error_callback = getattr(rt, "handle_polling_error", None)
                await rt.app.updater.start_polling(
                    drop_pending_updates=True,
                    error_callback=error_callback,
                    timeout=30,
                )
                rt.telegram_connected = True
                try:
                    await rt.app.bot.set_my_commands(rt.get_bot_commands())
                except Exception as e:
                    if hasattr(rt, "logger"):
                        rt.logger.warning(f"Could not register command menu: {e}")
                return True
            except Exception as e:
                last_failure_reason = f"connect attempt {attempt}/3: {type(e).__name__}: {e}"
                bridge_logger.warning(f"{rt.name}: {last_failure_reason}")
                if attempt < 3:
                    main_logger.warning(
                        f"Telegram connect for '{rt.name}' attempt {attempt} failed: {e}. Retrying in 5s..."
                    )
                    # Cleanup partial Telegram state
                    with suppress(Exception):
                        await rt.app.shutdown()
                    await asyncio.sleep(5)
                    continue
                main_logger.warning(
                    f"⚠️ Telegram connection failed for '{rt.name}' after 3 attempts: {e}. "
                    f"Agent will run in LOCAL MODE."
                )
                bridge_logger.warning(f"{rt.name}: all 3 connect attempts failed → LOCAL MODE")
                rt.telegram_connected = False
                return False

        rt.telegram_connected = False
        return False

    async def start_agent(self, agent_name: str) -> tuple[bool, str]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("start_agent() must run inside an asyncio task.")

        async with self._lifecycle_lock:
            if agent_name in self._runtime_map():
                return False, f"Agent '{agent_name}' is already running."
            if agent_name in self._startup_tasks:
                return False, f"Agent '{agent_name}' is already starting."
            self._startup_tasks[agent_name] = current_task

        agent_lock = self._agent_lock(agent_name)
        try:
            async with agent_lock:
                async with self._lifecycle_lock:
                    if agent_name in self._runtime_map():
                        return False, f"Agent '{agent_name}' is already running."
                    try:
                        global_cfg, agent_configs, secrets = self._load_config_bundle()
                    except Exception as e:
                        return False, f"Failed to load configuration: {e}"

                    agent_cfg = next((cfg for cfg in agent_configs if cfg.name == agent_name), None)
                    if agent_cfg is None:
                        return False, f"Agent '{agent_name}' is not configured."

                try:
                    runtime = self._build_runtime(agent_cfg, global_cfg, secrets)
                except Exception as e:
                    main_logger.error(f"Failed to initialize '{agent_name}': {e}")
                    return False, str(e)

                ok, message = await self._start_runtime(runtime)
                if not ok:
                    await self._cleanup_runtime_start_failure(runtime)
                    return False, message

                runtime.process_task = asyncio.create_task(runtime.process_queue(), name=f"queue-{runtime.name}")
                async with self._lifecycle_lock:
                    self.runtimes.append(runtime)
                # Startup notification: Telegram if connected, WhatsApp if in local mode
                if runtime.telegram_connected:
                    if hasattr(runtime, "enqueue_startup_bootstrap"):
                        await runtime.enqueue_startup_bootstrap(global_cfg.authorized_id)
                elif self.whatsapp is not None:
                    # Send startup notification via WhatsApp for local mode agents
                    await self._send_whatsapp_startup_notification(runtime)
                return True, message
        finally:
            try:
                async with self._lifecycle_lock:
                    if self._startup_tasks.get(agent_name) is current_task:
                        self._startup_tasks.pop(agent_name, None)
            except Exception:
                self._startup_tasks.pop(agent_name, None)

    async def stop_agent(self, agent_name: str, reason: str = "manual-stop") -> tuple[bool, str]:
        async with self._lifecycle_lock:
            if agent_name in self._startup_tasks:
                return False, f"Agent '{agent_name}' is still starting."
            runtime = self._runtime_map().get(agent_name)
            if runtime is None:
                return False, f"Agent '{agent_name}' is not running."

            bridge_logger.info(f"Stopping agent '{agent_name}' (reason={reason})")
            await self._teardown_runtime(runtime)

            self.runtimes = [rt for rt in self.runtimes if rt.name != agent_name]
            main_logger.info(f"Agent '{agent_name}' stopped.")
            bridge_logger.info(f"Agent '{agent_name}' stopped (reason={reason})")
            print(f"{C_STOP}[system] Agent '{agent_name}' stopped{C_RESET}", flush=True)
            return True, f"Stopped agent '{agent_name}'."

    async def _teardown_runtime(self, runtime, timeout: float = 10.0):
        """Stop a single runtime's queue task, backend, and Telegram app."""
        process_task = getattr(runtime, "process_task", None)
        if process_task is not None:
            process_task.cancel()
            with suppress(asyncio.CancelledError):
                await process_task
            runtime.process_task = None

        try:
            await asyncio.wait_for(runtime.shutdown(), timeout=timeout)
        except asyncio.TimeoutError:
            main_logger.warning(f"Shutdown timed out for '{runtime.name}'.")
            bridge_logger.warning(f"Agent '{runtime.name}' shutdown timed out after {timeout:.1f}s")
        except Exception as e:
            main_logger.warning(f"Shutdown warning for '{runtime.name}': {e}")
            bridge_logger.warning(f"Agent '{runtime.name}' shutdown warning: {type(e).__name__}: {e}")

    async def _shutdown_all_agents(self, timeout: float = 30.0):
        """Parallel shutdown of all agents. Used during orchestrator exit."""
        agents = list(self.runtimes)
        if not agents:
            return

        main_logger.info(f"Shutting down {len(agents)} agents in parallel...")
        bridge_logger.warning(f"Shutting down {len(agents)} active agents in parallel")

        async def _stop_one(rt):
            await self._teardown_runtime(rt, timeout=timeout - 2.0)
            main_logger.info(f"Agent '{rt.name}' stopped.")
            bridge_logger.info(f"Agent '{rt.name}' fully torn down")
            print(f"{C_STOP}[system] Agent '{rt.name}' stopped{C_RESET}", flush=True)

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_stop_one(rt) for rt in agents], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            main_logger.warning(
                f"Parallel agent shutdown timed out after {timeout}s. "
                f"Some agents may not have exited cleanly."
            )
            bridge_logger.warning(f"Parallel agent shutdown timed out after {timeout:.1f}s")

        self.runtimes.clear()

    def _reload_project_modules(self):
        """Reload all project Python modules so hot restart picks up code changes."""
        import importlib
        # Reload adapters first (leaf), then orchestrator (depends on adapters).
        # Skip transports — WhatsApp transport stays alive across restart.
        prefixes = ("adapters.", "orchestrator.")
        # Sort order: adapters first (leaf), then orchestrator utilities,
        # then runtime modules last (they import from utilities like voice_manager).
        # This ensures `from orchestrator.X import Y` picks up the reloaded version.
        def _reload_key(n: str):
            if n.startswith("adapters."):
                return (0, n)
            if "_runtime" in n:
                return (2, n)
            return (1, n)
        to_reload = sorted(
            (name for name in list(sys.modules) if any(name.startswith(p) for p in prefixes)),
            key=_reload_key,
        )
        reloaded = []
        for name in to_reload:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            try:
                importlib.reload(mod)
                reloaded.append(name)
            except Exception as e:
                main_logger.warning(f"Hot reload failed for {name}: {e}")
        if reloaded:
            main_logger.info(f"Hot reload: reloaded {len(reloaded)} modules.")

    async def _do_hot_restart(self, restart: dict):
        """Stop agents per restart mode, reload Python code + config, start new agents."""
        mode = restart.get("mode", "same")
        requesting_agent = restart.get("agent_name")
        agent_number = restart.get("agent_number")

        # Determine which agents to restart
        if mode == "min" and requesting_agent:
            targets = [requesting_agent]
        elif mode == "number" and agent_number is not None:
            all_names = self.configured_agent_names()
            idx = agent_number - 1
            if 0 <= idx < len(all_names):
                targets = [all_names[idx]]
            else:
                main_logger.warning(f"Restart: invalid agent number {agent_number}, restarting all.")
                targets = [rt.name for rt in self.runtimes]
        elif mode == "max":
            targets = [rt.name for rt in self.runtimes]
        else:  # "same" — restart whatever is currently running
            targets = [rt.name for rt in self.runtimes]

        # Set up boot_state for the banner animation
        boot_state = {name: "pending" for name in targets}
        boot_reason = {}

        main_logger.info(f"Hot restart: stopping {len(targets)} agent(s): {targets}")
        bridge_logger.warning(
            f"Hot restart begin (mode={mode}, requester={requesting_agent or '-'}, "
            f"number={agent_number if agent_number is not None else '-'}, targets={targets})"
        )
        for name in targets:
            try:
                await self.stop_agent(name, reason=f"hot-restart:{mode}")
            except Exception as e:
                main_logger.warning(f"Hot restart: failed to stop '{name}': {e}")
                bridge_logger.warning(f"Hot restart failed to stop '{name}': {type(e).__name__}: {e}")

        # Reload Python modules to pick up code changes
        self._reload_project_modules()

        # Reload config and start the same agents
        main_logger.info(f"Hot restart: starting agents: {targets}")
        try:
            _, agent_configs, _ = self._load_config_bundle()
            active_config_names = {cfg.name for cfg in agent_configs}
            # Agents that were running but have since been deactivated — skip them
            newly_inactive = [name for name in targets if name not in active_config_names]
            targets = [name for name in targets if name in active_config_names]
            for name in newly_inactive:
                boot_state.pop(name, None)
            inactive_agent_names = [cfg.name for cfg in agent_configs if cfg.name not in targets] + newly_inactive
        except Exception as e:
            main_logger.error(f"Hot restart: config reload failed: {e}")
            inactive_agent_names = []

        # Run banner animation concurrently with agent startups
        from orchestrator.banner import show_startup_banner
        loop = asyncio.get_running_loop()

        wa_enabled = self.whatsapp is not None
        workbench_port = getattr(self.global_cfg, "workbench_port", None) if self.global_cfg else None
        api_gw = self.api_gateway is not None

        async def _start_agent_with_state(name):
            boot_state[name] = "connecting"
            try:
                ok, msg = await self.start_agent(name)
                if ok:
                    new_state = "local" if "LOCAL MODE" in msg.upper() else "online"
                    boot_state[name] = new_state
                    if new_state == "local":
                        boot_reason[name] = "Telegram unavailable"
                else:
                    boot_state[name] = "failed"
                    boot_reason[name] = msg
                    main_logger.error(f"Hot restart: {msg}")
            except Exception as e:
                boot_state[name] = "failed"
                boot_reason[name] = f"{type(e).__name__}: {e}"
                main_logger.error(f"Hot restart: failed to start '{name}': {e}")

        def _run_banner():
            show_startup_banner(
                agent_names=targets,
                boot_state=boot_state,
                workbench_port=workbench_port,
                wa_enabled=wa_enabled,
                api_gateway_enabled=api_gw,
                inactive_agents=inactive_agent_names,
                boot_reason=boot_reason,
            )

        _mute = _AnimMute()
        _handler.addFilter(_mute)
        try:
            await asyncio.gather(
                loop.run_in_executor(None, _run_banner),
                *[_start_agent_with_state(name) for name in targets],
                return_exceptions=True,
            )
        finally:
            _handler.removeFilter(_mute)

        # Recreate the scheduler so it picks up reloaded code (e.g. loop_meta logic)
        if self.scheduler_task is not None:
            self.scheduler_task.cancel()
            try:
                await asyncio.wait_for(self.scheduler_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        # Re-import from reloaded module to get the updated class
        ReloadedScheduler = sys.modules["orchestrator.scheduler"].TaskScheduler
        self.scheduler = ReloadedScheduler(
            self.paths.tasks_path,
            self.paths.state_path,
            self.runtimes,
            self.global_cfg.authorized_id if self.global_cfg else 0,
            self.skill_manager,
            orchestrator=self,
        )
        self.scheduler_task = asyncio.create_task(self.scheduler.run(), name="scheduler")
        main_logger.info("Hot restart: scheduler recreated with reloaded code.")
        bridge_logger.info("Hot restart: scheduler recreated with reloaded code")

        if self.runtimes:
            main_logger.info(f"Hot restart complete. {len(self.runtimes)} agent(s) running.")
            bridge_logger.warning(f"Hot restart complete ({len(self.runtimes)} agent(s) running)")
            print(f"\033[38;5;108m  ✓ reboot complete — {len(self.runtimes)} agent(s) online\033[0m\n", flush=True)
        else:
            main_logger.critical("Hot restart: no agents running after restart.")
            bridge_logger.critical("Hot restart failed: no agents running after restart")
            print("\033[38;5;203m  ✗ reboot failed — no agents running\033[0m\n", flush=True)

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

        # Build agent directory so /hchat and other agent-to-agent commands can resolve runtimes
        from orchestrator.agent_directory import AgentDirectory as _AgentDirectory
        capabilities_path = self.paths.bridge_home / "agent_capabilities.json"
        self.agent_directory = _AgentDirectory(self.paths.config_path, capabilities_path, self.runtimes)

        try:
            self.workbench_api = WorkbenchApiServer(
                self.paths.config_path,
                global_cfg,
                self.runtimes,
                secrets=secrets,
                orchestrator=self,
            )
            await self.workbench_api.start()
            main_logger.info(
                f"Workbench API listening on http://127.0.0.1:{global_cfg.workbench_port}"
            )
        except Exception as e:
            self.workbench_api = None
            main_logger.warning(
                f"Workbench API failed to start; continuing without workbench integration: {e}"
            )
            main_logger.debug(traceback.format_exc())

        if self.enable_api_gateway:
            try:
                self.api_gateway = APIGatewayServer(
                    global_cfg,
                    secrets,
                    workspace_root=self.paths.workspaces_root,
                )
                await self.api_gateway.start()
                main_logger.info(
                    f"API Gateway listening on http://127.0.0.1:{global_cfg.api_gateway_port}"
                )
            except Exception as e:
                self.api_gateway = None
                main_logger.warning(
                    f"API Gateway failed to start; continuing without it: {e}"
                )
                main_logger.debug(traceback.format_exc())
        else:
            main_logger.info("API Gateway disabled (use --api-gateway to enable).")

        self.scheduler = TaskScheduler(
            self.paths.tasks_path,
            self.paths.state_path,
            self.runtimes,
            global_cfg.authorized_id,
            self.skill_manager,
            orchestrator=self,
        )
        self.scheduler_task = asyncio.create_task(self.scheduler.run(), name="scheduler")

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
            if self.scheduler_task is not None:
                bridge_logger.info("Stopping scheduler task")
                self.scheduler_task.cancel()
                try:
                    await asyncio.wait_for(self.scheduler_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    bridge_logger.warning("Scheduler task stop timed out or was cancelled")
            if self.workbench_api is not None:
                bridge_logger.info("Stopping Workbench API")
                try:
                    await asyncio.wait_for(self.workbench_api.shutdown(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    main_logger.warning(f"Workbench API shutdown warning: {e}")
                    bridge_logger.warning(f"Workbench API shutdown warning: {type(e).__name__}: {e}")
            if self.api_gateway is not None:
                bridge_logger.info("Stopping API Gateway")
                try:
                    await asyncio.wait_for(self.api_gateway.stop(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    main_logger.warning(f"API Gateway shutdown warning: {e}")
                    bridge_logger.warning(f"API Gateway shutdown warning: {type(e).__name__}: {e}")
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
