from __future__ import annotations

import asyncio
import logging
import traceback

main_logger = logging.getLogger("BridgeU.Orchestrator")


class WhatsAppManager:
    """Control WhatsApp transport while the live transport handle stays on the kernel."""

    def __init__(self, kernel):
        self.kernel = kernel

    def load_config(self) -> tuple[dict, dict]:
        raw_cfg = self.kernel._load_raw_config()
        wa_cfg = raw_cfg.get("global", {}).get("whatsapp", {}) or {}
        return raw_cfg, wa_cfg

    async def start_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        async with self.kernel._lifecycle_lock:
            if self.kernel.whatsapp is not None:
                return False, "WhatsApp transport is already running."

            try:
                raw_cfg, wa_cfg = self.load_config()
            except Exception as e:
                return False, f"Failed to load WhatsApp config: {e}"

            if persist_enabled and not wa_cfg.get("enabled"):
                raw_cfg.setdefault("global", {}).setdefault("whatsapp", {})
                raw_cfg["global"]["whatsapp"]["enabled"] = True
                try:
                    self.kernel._write_raw_config(raw_cfg)
                except Exception as e:
                    return False, f"Failed to persist WhatsApp enabled flag: {e}"
                wa_cfg = raw_cfg["global"]["whatsapp"]

            if self.kernel.global_cfg is None:
                try:
                    global_cfg, _, secrets = self.kernel._load_config_bundle()
                    self.kernel.global_cfg = global_cfg
                    self.kernel.secrets = secrets
                except Exception as e:
                    return False, f"Failed to load runtime configuration: {e}"
            else:
                global_cfg = self.kernel.global_cfg

            try:
                from transports.whatsapp import WhatsAppTransport

                self.kernel.whatsapp = WhatsAppTransport(self.kernel, global_cfg, wa_cfg)
                await self.kernel.whatsapp.start()
                main_logger.info(
                    "WhatsApp transport started. If this account is not paired yet, "
                    "scan the QR code in this bridge-u-f console window."
                )
                return True, (
                    "WhatsApp transport started. "
                    "If this is the first login, scan the QR code in the bridge-u-f console window."
                )
            except Exception as e:
                self.kernel.whatsapp = None
                main_logger.warning("WhatsApp transport failed to start: %s", e)
                main_logger.debug(traceback.format_exc())
                return False, f"WhatsApp transport failed to start: {e}"

    async def stop_transport(self, persist_enabled: bool = True) -> tuple[bool, str]:
        async with self.kernel._lifecycle_lock:
            config_note = ""
            if persist_enabled:
                try:
                    raw_cfg, wa_cfg = self.load_config()
                    if wa_cfg.get("enabled"):
                        raw_cfg.setdefault("global", {}).setdefault("whatsapp", {})
                        raw_cfg["global"]["whatsapp"]["enabled"] = False
                        self.kernel._write_raw_config(raw_cfg)
                    config_note = " Future startups will keep WhatsApp disabled."
                except Exception as e:
                    return False, f"Failed to persist WhatsApp disabled flag: {e}"

            if self.kernel.whatsapp is None:
                return True, f"WhatsApp transport is already stopped.{config_note}"

            try:
                await asyncio.wait_for(self.kernel.whatsapp.shutdown(), timeout=5.0)
            except Exception as e:
                self.kernel.whatsapp = None
                main_logger.warning("WhatsApp shutdown warning: %s", e)
                return False, f"WhatsApp shutdown warning: {e}"

            self.kernel.whatsapp = None
            main_logger.info("WhatsApp transport stopped.")
            return True, f"WhatsApp transport stopped.{config_note}"

    async def send_text(self, phone_number: str, text: str) -> tuple[bool, str]:
        async with self.kernel._lifecycle_lock:
            if self.kernel.whatsapp is None:
                return False, "WhatsApp transport is not running."
            try:
                await self.kernel.whatsapp.send_text_to_number(phone_number, text)
                return True, f"Sent WhatsApp message to {phone_number}."
            except Exception as e:
                main_logger.warning("WhatsApp admin send failed for %s: %s", phone_number, e)
                return False, f"Failed to send WhatsApp message to {phone_number}: {e}"

    async def send_startup_notification(self, runtime):
        if self.kernel.whatsapp is None:
            return
        try:
            wa_cfg = self.kernel.global_cfg.__dict__.get("whatsapp", {}) if self.kernel.global_cfg else {}
            admin_numbers = wa_cfg.get("allowed_numbers", []) if isinstance(wa_cfg, dict) else []
            if not admin_numbers:
                main_logger.debug("No WhatsApp admin numbers configured for startup notification.")
                return

            display_name = getattr(runtime, "get_display_name", lambda: runtime.name)()
            emoji = getattr(runtime, "get_agent_emoji", lambda: "🤖")()
            message = (
                f"{emoji} {display_name} started in LOCAL MODE\n"
                f"⚠️ Telegram unavailable — using Workbench + WhatsApp\n"
                f"Use /agent to check status"
            )

            for phone in admin_numbers[:1]:
                try:
                    await self.kernel.whatsapp.send_text_to_number(phone, message)
                    main_logger.info("Sent WhatsApp startup notification for '%s' to %s", runtime.name, phone)
                    break
                except Exception as e:
                    main_logger.warning("Failed to send WhatsApp startup notification: %s", e)
        except Exception as e:
            main_logger.debug("WhatsApp startup notification skipped: %s", e)
