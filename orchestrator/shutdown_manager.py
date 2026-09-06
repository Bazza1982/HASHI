from __future__ import annotations

import logging
import os
import threading
import time

from orchestrator.bootstrap_logging import emit_bridge_audit

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")


class ShutdownManager:
    """Full shutdown sequence and post-cleanup watchdog."""

    def __init__(self, kernel):
        self.kernel = kernel

    async def full_shutdown(self):
        self.kernel.is_stopping = True
        startup_status = dict(getattr(self.kernel, "startup_status", {}) or {})
        startup_status.update(
            {
                "phase": "stopping",
                "ready": False,
                "degraded": False,
            }
        )
        self.kernel.startup_status = startup_status
        main_logger.info("Shutting down active agents...")
        bridge_logger.info(
            "Full shutdown begin (%s) active_agents=%s workbench=%s api_gateway=%s whatsapp=%s",
            self.kernel.lifecycle_state.shutdown_meta_text(self.kernel._shutdown_request),
            len(self.kernel.runtimes),
            "on" if self.kernel.workbench_api is not None else "off",
            "on" if self.kernel.api_gateway is not None else "off",
            "on" if self.kernel.whatsapp is not None else "off",
        )
        await self.kernel.service_manager.stop_runtime_services()
        if self.kernel.whatsapp is not None:
            ok, message = await self.kernel.stop_whatsapp_transport(persist_enabled=False)
            if not ok:
                bridge_logger.warning(message)
        await self.kernel._shutdown_all_agents()
        self.kernel.lifecycle_state.mark_shutdown(
            self.kernel._shutdown_request,
            clean=True,
            phase="python-cleanup-complete",
        )
        summary = dict(getattr(self.kernel, "last_shutdown_summary", {}) or {})
        bridge_logger.info(
            "Full shutdown complete (%s) agents=%s/%s complete=%s",
            self.kernel.lifecycle_state.shutdown_meta_text(self.kernel._shutdown_request),
            summary.get("stopped_agents", 0),
            summary.get("requested_agents", 0),
            bool(summary.get("complete", False)),
        )
        self.start_exit_watchdog()

    def start_exit_watchdog(self):
        def _exit_watchdog():
            time.sleep(5)
            msg = "Shutdown watchdog: forcing exit (Go runtime threads did not stop)."
            main_logger.warning(msg)
            emit_bridge_audit(self.kernel.paths, logging.WARNING, msg, bridge_logger)
            os._exit(0)

        threading.Thread(target=_exit_watchdog, daemon=True, name="exit-watchdog").start()
