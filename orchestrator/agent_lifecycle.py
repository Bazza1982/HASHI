from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx

from orchestrator.bootstrap_logging import C_RESET, C_STOP

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")


class AgentLifecycleManager:
    """Start, stop, and tear down agent runtimes for the live kernel."""

    def __init__(self, kernel):
        self.kernel = kernel

    def build_runtime(self, agent_cfg, global_cfg, secrets):
        # Local imports so hot restart picks up reloaded module code.
        from orchestrator.agent_runtime import BridgeAgentRuntime as _BridgeRT
        from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime as _FlexRT
        from adapters.registry import get_backend_class as _get_backend

        if agent_cfg.type in {"flex", "limited"}:
            token = secrets.get(agent_cfg.telegram_token_key)
            if not token:
                main_logger.warning(
                    "No Telegram token found for flex agent '%s' (key: %s).",
                    agent_cfg.name,
                    agent_cfg.telegram_token_key,
                )
                token = "WORKBENCH_ONLY_NO_TOKEN"
            runtime = _FlexRT(agent_cfg, global_cfg, token, secrets, self.kernel.skill_manager)
        else:
            token = secrets.get(agent_cfg.name)
            api_key = secrets.get(f"{agent_cfg.engine}_key", None)
            if not token:
                main_logger.warning("No Telegram token found for agent '%s'.", agent_cfg.name)
                token = "WORKBENCH_ONLY_NO_TOKEN"
            backend = _get_backend(agent_cfg.engine)(agent_cfg, global_cfg, api_key)
            runtime = _BridgeRT(agent_cfg.name, backend, token, self.kernel.skill_manager, secrets=secrets)
        runtime.orchestrator = self.kernel
        runtime.bind_handlers()
        return runtime

    async def cleanup_runtime_start_failure(self, rt):
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
                bridge_logger.info("Telegram preflight OK for '%s'%s", agent_name, attempt_tag)
                return True
        except Exception as e:
            err_text = str(e) or "<no error message>"
            msg = f"Telegram preflight failed for '{agent_name}'{attempt_tag}: {type(e).__name__}: {err_text}"
            main_logger.warning(msg)
            bridge_logger.warning(msg)
            return False

    async def start_runtime(self, rt) -> tuple[bool, str]:
        """
        Start an agent runtime. Backend initialization failure is fatal.
        Telegram connection failure results in LOCAL MODE (Workbench + WhatsApp only).
        """
        bridge_logger.info("%s: starting backend initialization", rt.name)
        for attempt in range(1, 4):
            try:
                main_logger.info("Initializing backend for '%s' (attempt %s/3)...", rt.name, attempt)
                if hasattr(rt, "backend"):
                    backend_ok = await rt.backend.initialize()
                    if not backend_ok:
                        await rt.backend.shutdown()
                        bridge_logger.error("%s: backend.initialize() returned False", rt.name)
                        return False, f"Backend for '{rt.name}' failed to initialize."
                else:
                    backend_ok = await rt.initialize()
                    if not backend_ok:
                        await rt.shutdown()
                        bridge_logger.error("%s: flex initialize() returned False", rt.name)
                        return False, f"Flex initialization for '{rt.name}' failed."
                rt.backend_ready = True
                bridge_logger.info("%s: backend ready (attempt %s/3)", rt.name, attempt)
                break
            except Exception as e:
                bridge_logger.warning("%s: backend init attempt %s/3 failed: %s: %s", rt.name, attempt, type(e).__name__, e)
                if attempt < 3:
                    main_logger.warning(
                        "Backend init for '%s' attempt %s failed: %s. Retrying in 5s...",
                        rt.name,
                        attempt,
                        e,
                    )
                    await self.cleanup_runtime_start_failure(rt)
                    await asyncio.sleep(5)
                    continue
                if hasattr(rt, "error_logger"):
                    rt.error_logger.exception("Backend startup error for '%s' after 3 attempts: %s", rt.name, e)
                else:
                    main_logger.error("Backend startup error for '%s' after 3 attempts: %s", rt.name, e)
                bridge_logger.error("%s: backend init FAILED after 3 attempts", rt.name)
                return False, f"Failed to start '{rt.name}': {e}"

        bridge_logger.info("%s: starting Telegram connection", rt.name)
        telegram_ok = await self.try_telegram_connect(rt)

        rt.startup_success = True
        if hasattr(rt, "prepare_post_start_state"):
            rt.prepare_post_start_state()

        if telegram_ok:
            bridge_logger.info("%s: ONLINE (backend + Telegram)", rt.name)
            main_logger.info("Bot '%s' is online.", rt.name)
            return True, f"Started agent '{rt.name}'."

        bridge_logger.warning("%s: LOCAL MODE (backend ok, Telegram failed)", rt.name)
        main_logger.info("Bot '%s' started in LOCAL MODE (Telegram unavailable).", rt.name)
        return True, f"Started '{rt.name}' in LOCAL MODE (Workbench + WhatsApp only)."

    async def try_telegram_connect(self, rt) -> bool:
        """
        Attempt to connect Telegram. Returns True on success, False on failure.
        Does NOT block agent startup — local mode will be used if this fails.
        """
        for attempt in range(1, 4):
            preflight_ok = await self.telegram_preflight(rt.token, rt.name, attempt=attempt, max_attempts=3)
            if not preflight_ok:
                last_failure_reason = f"preflight {attempt}/3 failed"
                if attempt < 3:
                    bridge_logger.info("%s: %s, retrying in 5s...", rt.name, last_failure_reason)
                    await asyncio.sleep(5)
                    continue
                msg = (
                    f"⚠️ Telegram unavailable for '{rt.name}'. "
                    f"Agent will run in LOCAL MODE (Workbench + WhatsApp only)."
                )
                main_logger.warning(msg)
                bridge_logger.warning("%s: all 3 preflight attempts failed → LOCAL MODE", rt.name)
                rt.telegram_connected = False
                return False

            try:
                main_logger.info("Connecting Telegram for '%s'...", rt.name)
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
                        rt.logger.warning("Could not register command menu: %s", e)
                return True
            except Exception as e:
                last_failure_reason = f"connect attempt {attempt}/3: {type(e).__name__}: {e}"
                bridge_logger.warning("%s: %s", rt.name, last_failure_reason)
                if attempt < 3:
                    main_logger.warning(
                        "Telegram connect for '%s' attempt %s failed: %s. Retrying in 5s...",
                        rt.name,
                        attempt,
                        e,
                    )
                    with suppress(Exception):
                        await rt.app.shutdown()
                    await asyncio.sleep(5)
                    continue
                main_logger.warning(
                    "⚠️ Telegram connection failed for '%s' after 3 attempts: %s. Agent will run in LOCAL MODE.",
                    rt.name,
                    e,
                )
                bridge_logger.warning("%s: all 3 connect attempts failed → LOCAL MODE", rt.name)
                rt.telegram_connected = False
                return False

        rt.telegram_connected = False
        return False

    async def start_agent(self, agent_name: str) -> tuple[bool, str]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("start_agent() must run inside an asyncio task.")

        async with self.kernel._lifecycle_lock:
            if agent_name in self.kernel._runtime_map():
                return False, f"Agent '{agent_name}' is already running."
            if agent_name in self.kernel._startup_tasks:
                return False, f"Agent '{agent_name}' is already starting."
            self.kernel._startup_tasks[agent_name] = current_task

        agent_lock = self.kernel._agent_lock(agent_name)
        try:
            async with agent_lock:
                async with self.kernel._lifecycle_lock:
                    if agent_name in self.kernel._runtime_map():
                        return False, f"Agent '{agent_name}' is already running."
                    try:
                        global_cfg, agent_configs, secrets = self.kernel._load_config_bundle()
                    except Exception as e:
                        return False, f"Failed to load configuration: {e}"

                    agent_cfg = next((cfg for cfg in agent_configs if cfg.name == agent_name), None)
                    if agent_cfg is None:
                        return False, f"Agent '{agent_name}' is not configured."

                try:
                    runtime = self.build_runtime(agent_cfg, global_cfg, secrets)
                except Exception as e:
                    main_logger.error("Failed to initialize '%s': %s", agent_name, e)
                    return False, str(e)

                ok, message = await self.start_runtime(runtime)
                if not ok:
                    await self.cleanup_runtime_start_failure(runtime)
                    return False, message

                runtime.process_task = asyncio.create_task(runtime.process_queue(), name=f"queue-{runtime.name}")
                async with self.kernel._lifecycle_lock:
                    self.kernel.runtimes.append(runtime)
                if runtime.telegram_connected:
                    if hasattr(runtime, "enqueue_startup_bootstrap"):
                        await runtime.enqueue_startup_bootstrap(global_cfg.authorized_id)
                elif self.kernel.whatsapp is not None:
                    await self.kernel._send_whatsapp_startup_notification(runtime)
                return True, message
        finally:
            try:
                async with self.kernel._lifecycle_lock:
                    if self.kernel._startup_tasks.get(agent_name) is current_task:
                        self.kernel._startup_tasks.pop(agent_name, None)
            except Exception:
                self.kernel._startup_tasks.pop(agent_name, None)

    async def stop_agent(self, agent_name: str, reason: str = "manual-stop") -> tuple[bool, str]:
        async with self.kernel._lifecycle_lock:
            if agent_name in self.kernel._startup_tasks:
                return False, f"Agent '{agent_name}' is still starting."
            runtime = self.kernel._runtime_map().get(agent_name)
            if runtime is None:
                return False, f"Agent '{agent_name}' is not running."

            bridge_logger.info("Stopping agent '%s' (reason=%s)", agent_name, reason)
            await self.teardown_runtime(runtime)

            self.kernel.runtimes = [rt for rt in self.kernel.runtimes if rt.name != agent_name]
            main_logger.info("Agent '%s' stopped.", agent_name)
            bridge_logger.info("Agent '%s' stopped (reason=%s)", agent_name, reason)
            print(f"{C_STOP}[system] Agent '{agent_name}' stopped{C_RESET}", flush=True)
            return True, f"Stopped agent '{agent_name}'."

    async def teardown_runtime(self, runtime, timeout: float = 10.0):
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
            main_logger.warning("Shutdown timed out for '%s'.", runtime.name)
            bridge_logger.warning("Agent '%s' shutdown timed out after %.1fs", runtime.name, timeout)
        except Exception as e:
            main_logger.warning("Shutdown warning for '%s': %s", runtime.name, e)
            bridge_logger.warning("Agent '%s' shutdown warning: %s: %s", runtime.name, type(e).__name__, e)

    async def shutdown_all_agents(self, timeout: float = 30.0):
        """Parallel shutdown of all agents. Used during orchestrator exit."""
        agents = list(self.kernel.runtimes)
        if not agents:
            return

        main_logger.info("Shutting down %s agents in parallel...", len(agents))
        bridge_logger.warning("Shutting down %s active agents in parallel", len(agents))

        async def _stop_one(rt):
            await self.teardown_runtime(rt, timeout=timeout - 2.0)
            main_logger.info("Agent '%s' stopped.", rt.name)
            bridge_logger.info("Agent '%s' fully torn down", rt.name)
            print(f"{C_STOP}[system] Agent '{rt.name}' stopped{C_RESET}", flush=True)

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_stop_one(rt) for rt in agents], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            main_logger.warning(
                "Parallel agent shutdown timed out after %ss. Some agents may not have exited cleanly.",
                timeout,
            )
            bridge_logger.warning("Parallel agent shutdown timed out after %.1fs", timeout)

        self.kernel.runtimes.clear()
