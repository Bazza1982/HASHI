from __future__ import annotations
"""
WhatsApp transport for bridge-u-f using neonize (Python Whatsmeow wrapper).

Message flow:
  WhatsApp → neonize aio event → _on_message() → command check → route to agent(s)
  Agent response → register_request_listener callback → _on_agent_response() → neonize send

Installation:
  pip install neonize

Authentication:
  First run prints a QR code to the terminal. Scan with WhatsApp on your phone.
  Credentials are saved to wa_session/ and reused on subsequent runs.
  If the session expires, delete wa_session/ and re-scan.

Commands handled at transport level (never reach the agent):
  /agent <name>           — route all messages to one agent
  /agent <n1> <n2> ...    — group chat: fan-out to multiple agents
  /all                    — broadcast to every running agent
  /agent (no args)        — show current routing + online/offline status

All other messages (including /new, /verbose, /skill, etc.) are forwarded
to the target agent(s) unchanged.
"""

import asyncio
import inspect
import json
import logging
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from orchestrator.pathing import resolve_path_value
from transports.chat_router import ChatRouter

logger = logging.getLogger("WhatsApp")

_C_WA_IN = "\033[38;5;116m"
_C_WA_OUT = "\033[38;5;115m"
_C_WA_SYS = "\033[38;5;109m"
_C_RESET = "\033[0m"


def _print_wa_line(color: str, label: str, text: str):
    line = f"{color}[whatsapp:{label}] {text}{_C_RESET}" if label else f"{color}{text}{_C_RESET}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


class WhatsAppTransport:
    """Connects WhatsApp via neonize.aioze (async) to bridge-u-f's agent runtimes."""

    def __init__(self, orchestrator, global_cfg, wa_cfg: dict):
        self.orchestrator = orchestrator
        self.global_cfg = global_cfg
        self.wa_cfg = wa_cfg
        self._ensure_file_logging()
        self._config_path = Path(str(global_cfg.config_path))
        self._config_mtime_ns = 0

        session_dir = resolve_path_value(
            wa_cfg.get("session_dir", "@home/wa_session"),
            config_dir=self._config_path.parent,
            bridge_home=self.global_cfg.bridge_home,
        ) or (self.global_cfg.bridge_home / "wa_session")
        session_dir.mkdir(parents=True, exist_ok=True)
        # neonize uses the name as the DB filename — pass relative path to put it
        # inside wa_session/ rather than the CWD root.
        self._client_name = str(session_dir / "bridge-u-f")

        # Whitelist: accept by phone candidates (+E.164) and/or exact chat JID.
        # Empty sets = accept from anyone (not recommended for production).
        self._allowed_numbers: set[str] = set()
        self._allowed_chat_ids: set[str] = set()

        # Routing state persisted to wa_session/routing.json
        routing_state_path = session_dir / "routing.json"
        self._router = ChatRouter(state_path=routing_state_path)
        self._client = None
        self._connect_task = None

        # Cache of chat_key_str → JID object so we can reply without re-parsing
        self._jid_cache: dict[str, object] = {}

        base_media = Path(str(global_cfg.base_media_dir))
        self._media_dir = base_media / "whatsapp"
        self._media_dir.mkdir(parents=True, exist_ok=True)
        self._refresh_runtime_config(force=True)

        logger.info(
            "WhatsApp transport configured: session_dir=%s allowed_numbers=%s allowed_chat_ids=%s",
            session_dir,
            sorted(self._allowed_numbers),
            sorted(self._allowed_chat_ids),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Connect the async neonize client and start receiving messages."""
        try:
            from neonize.aioze.client import NewAClient
            from neonize.aioze.events import MessageEv, ConnectedEv, DisconnectedEv

            self._client = NewAClient(self._client_name)

            @self._client.event(ConnectedEv)
            async def _on_connected(client, ev):
                logger.info("WhatsApp connected successfully.")
                _print_wa_line(_C_WA_SYS, "system", "connected")

            @self._client.event(DisconnectedEv)
            async def _on_disconnected(client, ev):
                logger.warning("WhatsApp disconnected.")

            @self._client.event(MessageEv)
            async def _on_message(client, msg):
                await self._on_message(msg)

            # connect() creates an asyncio task (non-blocking) and returns it
            self._connect_task = await self._client.connect()
            logger.info("WhatsApp transport started — scan QR code if prompted.")
            _print_wa_line(_C_WA_SYS, "system", "transport started; scan QR if prompted")

        except ImportError:
            logger.error(
                "neonize is not installed. Run: pip install neonize\n"
                "WhatsApp transport will not be available."
            )
            raise
        except Exception as e:
            logger.error(f"WhatsApp transport start error: {e}", exc_info=True)
            raise

    async def shutdown(self):
        """Disconnect the WhatsApp client."""
        if self._connect_task is not None:
            self._connect_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._connect_task
        if self._client is not None:
            with suppress(Exception):
                await self._client.disconnect()
        logger.info("WhatsApp transport shut down.")
        _print_wa_line(_C_WA_SYS, "system", "transport shut down")

    # ------------------------------------------------------------------
    # Incoming message handler
    # ------------------------------------------------------------------

    async def _on_message(self, msg):
        """Handle an incoming WhatsApp message."""
        try:
            self._refresh_runtime_config()
            src = msg.Info.MessageSource

            # Ignore our own messages
            if src.IsFromMe:
                logger.info("Ignoring self-originated WhatsApp message.")
                return

            chat_jid = src.Chat           # JID object
            sender_jid = src.Sender       # JID object

            # Build string key for router + logging
            chat_key = self._jid_str(chat_jid)
            self._jid_cache[chat_key] = chat_jid

            # Whitelist check
            sender_candidates = self._phone_candidates(sender_jid)
            chat_candidates = self._phone_candidates(chat_jid)
            all_candidates = sorted(sender_candidates | chat_candidates)
            if self._allowed_numbers or self._allowed_chat_ids:
                allowed_by_number = any(candidate in self._allowed_numbers for candidate in all_candidates)
                allowed_by_chat = chat_key in self._allowed_chat_ids
                if not (allowed_by_number or allowed_by_chat):
                    logger.info(
                        "Ignored WhatsApp message from unlisted sender/chat. "
                        "chat=%s sender_candidates=%s chat_candidates=%s",
                        chat_key,
                        sorted(sender_candidates),
                        sorted(chat_candidates),
                    )
                    return

            phone = next(iter(sender_candidates or chat_candidates), "unknown")
            if self._allowed_numbers:
                phone = next(
                    (candidate for candidate in all_candidates if candidate in self._allowed_numbers),
                    phone,
                )

            text = self._extract_text(msg)
            is_voice = self._is_voice(msg)
            media_kind = self._detect_media_kind(msg)
            logger.info(
                "Received WhatsApp message: sender=%s chat=%s voice=%s media=%s text_preview=%r",
                phone,
                chat_key,
                is_voice,
                media_kind,
                (text[:120] if text else ""),
            )
            text_is_media_link = bool(text and ("mmg.whatsapp.net/" in text or "whatsapp.net/o1/" in text))
            if media_kind == "voice":
                preview = "[whatsapp inbound] voice"
            elif media_kind in {"photo", "video"}:
                preview = f"[whatsapp inbound] {media_kind}"
            elif media_kind == "document" or text_is_media_link:
                payload = self._message_payload(msg)
                doc = getattr(payload, "DocumentMessage", None) or getattr(payload, "documentMessage", None) if payload is not None else None
                file_name = getattr(doc, "FileName", None) or getattr(doc, "file_name", None) if doc is not None else None
                preview = f"[whatsapp inbound] document ({file_name})" if file_name else "[whatsapp inbound] document"
            elif text:
                preview = f"[whatsapp inbound] {text[:160].replace(chr(10), ' ')}"
            else:
                preview = "[whatsapp inbound]"
            _print_wa_line(_C_WA_IN, "", preview)

            # Routing and lifecycle commands are intercepted here; never forwarded to agents
            if text:
                first_word = text.strip().split()[0].lower()
                if first_word in ("/agent", "/all"):
                    await self._handle_routing_command(chat_key, text.strip())
                    return
                if first_word in ("/reboot", "/terminate", "/start", "/stop"):
                    await self._handle_lifecycle_command(chat_key, text.strip())
                    return

            # Resolve target agents: persisted route → auto single-agent → prompt user
            targets = self._router.get_targets(chat_key)
            if not targets:
                running = [rt.name for rt in self.orchestrator.runtimes]
                if len(running) == 1:
                    targets = running
            if not targets:
                logger.warning("No WhatsApp routing target for chat=%s", chat_key)
                await self._send_text(
                    chat_key,
                    "No agent selected.\nUse /agent <name> to route, or /agent to see options.",
                )
                return
            logger.info("Resolved WhatsApp routing: chat=%s mode=%s targets=%s", chat_key, self._router.get_mode(chat_key), targets)

            # Build prompt text (text-first for real text; media-first for media links/payloads)
            prompt = None
            source_kind = "whatsapp"
            media_kind = self._detect_media_kind(msg)
            text_is_media_link = bool(text and ("mmg.whatsapp.net/" in text or "whatsapp.net/o1/" in text))

            if media_kind == "voice":
                # Voice takes priority — even if text contains an encrypted media URL
                prompt = await self._handle_voice(msg, chat_key)
                source_kind = "voice_transcript"
                if prompt is None:
                    return
            elif text and not text_is_media_link:
                # Normal text (e.g., "hi") — never treat as media
                prompt = text
                source_kind = "whatsapp"
            elif text_is_media_link or media_kind in {"photo", "document", "video"}:
                # Media links from WhatsApp should prefer media handling path
                media_prompt, resolved_kind = await self._handle_media_message(msg, chat_key)
                if media_prompt:
                    prompt = media_prompt
                    source_kind = resolved_kind or media_kind or "document"
                else:
                    # Do not downgrade media links to plain text; they are encrypted pointers.
                    return
            else:
                prompt = text
                if not prompt:
                    payload = self._message_payload(msg)
                    payload_fields = "unknown"
                    if payload is not None:
                        list_fields = getattr(payload, "ListFields", None)
                        if callable(list_fields):
                            try:
                                payload_fields = [str(fd.name) for fd, _ in list_fields()]
                            except Exception:
                                payload_fields = "listfields-error"
                    preview_raw = ""
                    try:
                        preview_raw = str(getattr(msg, "Message", None) or getattr(msg, "message", None) or "")
                        if len(preview_raw) > 220:
                            preview_raw = preview_raw[:220] + "..."
                    except Exception:
                        preview_raw = "<raw-preview-error>"
                    dict_keys = []
                    try:
                        from google.protobuf.json_format import MessageToDict
                        root = getattr(msg, "Message", None) or getattr(msg, "message", None)
                        as_dict = MessageToDict(root, preserving_proto_field_name=True) if root is not None else {}
                        if isinstance(as_dict, dict):
                            dict_keys = list(as_dict.keys())[:20]
                    except Exception:
                        dict_keys = []
                    logger.info(
                        "WhatsApp message had no extractable text/voice/media. chat=%s payload_fields=%s dict_keys=%s raw_preview=%r",
                        chat_key,
                        payload_fields,
                        dict_keys,
                        preview_raw,
                    )
                    await self._send_text(chat_key, "Message received, but content could not be parsed yet. Please resend.")
                    return

            # Fan-out to target agents
            mode = self._router.get_mode(chat_key)
            for agent_name in targets:
                runtime = self._get_runtime(agent_name)
                if runtime is None:
                    logger.warning("WhatsApp target agent is not running: %s", agent_name)
                    await self._send_text(chat_key, f"Agent '{agent_name}' is not running.")
                    continue

                req_id = await runtime.enqueue_request(
                    chat_id=0,              # unused — deliver_to_telegram=False
                    prompt=prompt,
                    source=source_kind,
                    summary=f"WA[{source_kind}]: {prompt[:60]}",
                    deliver_to_telegram=False,
                )
                if req_id:
                    logger.info("Enqueued WhatsApp request: agent=%s request_id=%s", agent_name, req_id)
                    # register_request_listener calls callback(payload).
                    # If callback returns a coroutine, it's wrapped in create_task.
                    def _make_callback(aname: str, m: str, tgts: list, ckey: str):
                        def callback(payload: dict):
                            return self._on_agent_response(ckey, aname, payload, m, tgts)
                        return callback

                    runtime.register_request_listener(
                        req_id,
                        _make_callback(agent_name, mode, list(targets), chat_key),
                    )

        except Exception as e:
            logger.error(f"Error handling WhatsApp message: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Agent response → WhatsApp
    # ------------------------------------------------------------------

    async def _on_agent_response(
        self,
        chat_key: str,
        agent_name: str,
        payload: dict,
        mode: str,
        all_targets: list,
    ):
        """Send an agent's response back to the originating WhatsApp chat."""
        if not payload.get("success"):
            err = payload.get("error", "Unknown error")
            logger.warning("WhatsApp agent response failed: agent=%s error=%s", agent_name, err)
            await self._send_text(chat_key, f"[{agent_name}] Error: {err}")
            return

        text = payload.get("text") or ""
        if not text:
            logger.info("WhatsApp agent response empty: agent=%s", agent_name)
            return

        # Always prefix agent name for text replies so routing/switching is clear
        full_text = f"[{agent_name}]: {text}"

        await self._send_text(chat_key, full_text)
        runtime = self._get_runtime(agent_name)
        if runtime is None or not hasattr(runtime, "voice_manager"):
            return
        try:
            asset = await runtime.voice_manager.synthesize_reply(
                agent_name,
                payload.get("request_id", "wa"),
                text,
            )
        except Exception as e:
            logger.error(f"Failed to synthesize WhatsApp voice reply for {agent_name}: {e}", exc_info=True)
            return
        if asset is None:
            return
        await self._send_voice(chat_key, asset.ogg_path)

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    async def _handle_routing_command(self, chat_key: str, text: str):
        """Handle /agent [...] and /all routing commands."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/all":
            all_agents = [rt.name for rt in self.orchestrator.runtimes]
            if not all_agents:
                await self._send_text(chat_key, "No agents are currently running.")
                return
            self._router.set_broadcast(chat_key, all_agents)
            await self._send_text(
                chat_key, f"Broadcasting to all agents: {', '.join(all_agents)}"
            )
            return

        # /agent [name...]
        if len(parts) == 1:
            await self._send_text(chat_key, self._routing_status(chat_key))
            return

        requested = parts[1:]
        running_names = {rt.name for rt in self.orchestrator.runtimes}
        unknown = [a for a in requested if a not in running_names]
        if unknown:
            available = ", ".join(sorted(running_names)) or "(none running)"
            await self._send_text(
                chat_key,
                f"Unknown or stopped agent(s): {', '.join(unknown)}\nRunning: {available}",
            )
            return

        if len(requested) == 1:
            self._router.set_single(chat_key, requested[0])
            await self._send_text(chat_key, f"Routing to: {requested[0]}")
        else:
            self._router.set_group(chat_key, requested)
            await self._send_text(chat_key, f"Group chat with: {', '.join(requested)}")

    def _routing_status(self, chat_key: str) -> str:
        """Build a status string: current routing + all agents online/offline."""
        route = self._router.get_route(chat_key)
        mode = self._router.get_mode(chat_key)

        lines = []
        if mode == "none":
            running = [rt.name for rt in self.orchestrator.runtimes]
            if len(running) == 1:
                lines.append(f"Routing: {running[0]} (only agent)")
            else:
                lines.append("Routing: none — use /agent <name>")
        else:
            agents_str = ", ".join(route.agents) if route else ""
            lines.append(f"Routing: {agents_str} ({mode})")

        lines.append("")
        lines.append("Agents:")
        runtime_map = {rt.name: rt for rt in self.orchestrator.runtimes}
        for name in self.orchestrator.configured_agent_names():
            rt = runtime_map.get(name)
            if rt is None:
                lines.append(f"  ✗ {name} (stopped)")
            elif not getattr(rt, "backend_ready", False):
                lines.append(f"  ✗ {name} (backend error)")
            elif not getattr(rt, "telegram_connected", True):
                lines.append(f"  ⚡ {name} (local mode)")
            else:
                lines.append(f"  ✓ {name}")

        return "\n".join(lines)

    async def _handle_lifecycle_command(self, chat_key: str, text: str):
        """Handle /reboot, /terminate, /start, /stop lifecycle commands via WhatsApp."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/reboot":
            # Request hot restart of all agents
            await self._send_text(chat_key, "🔄 Requesting hot restart...")
            self.orchestrator.request_restart(mode="same")
            return

        if cmd == "/terminate":
            # Stop a specific agent or show options
            if len(parts) < 2:
                running = [rt.name for rt in self.orchestrator.runtimes]
                if not running:
                    await self._send_text(chat_key, "No agents are currently running.")
                else:
                    await self._send_text(
                        chat_key,
                        f"Usage: /terminate <agent_name>\nRunning: {', '.join(running)}"
                    )
                return
            agent_name = parts[1]
            ok, message = await self.orchestrator.stop_agent(agent_name)
            await self._send_text(chat_key, f"{'✓' if ok else '✗'} {message}")
            return

        if cmd == "/start":
            # Start a specific agent
            if len(parts) < 2:
                startable = self.orchestrator.get_startable_agent_names()
                if not startable:
                    await self._send_text(chat_key, "All configured agents are already running.")
                else:
                    await self._send_text(
                        chat_key,
                        f"Usage: /start <agent_name>\nAvailable: {', '.join(startable)}"
                    )
                return
            agent_name = parts[1]
            await self._send_text(chat_key, f"🚀 Starting {agent_name}...")
            ok, message = await self.orchestrator.start_agent(agent_name)
            await self._send_text(chat_key, f"{'✓' if ok else '✗'} {message}")
            return

        if cmd == "/stop":
            # Alias for /terminate
            if len(parts) < 2:
                running = [rt.name for rt in self.orchestrator.runtimes]
                if not running:
                    await self._send_text(chat_key, "No agents are currently running.")
                else:
                    await self._send_text(
                        chat_key,
                        f"Usage: /stop <agent_name>\nRunning: {', '.join(running)}"
                    )
                return
            agent_name = parts[1]
            ok, message = await self.orchestrator.stop_agent(agent_name)
            await self._send_text(chat_key, f"{'✓' if ok else '✗'} {message}")
            return

    # ------------------------------------------------------------------
    # Voice handling
    # ------------------------------------------------------------------

    async def _handle_voice(self, msg, chat_key: str) -> str | None:
        """Download WhatsApp voice/audio and transcribe locally."""
        try:
            from orchestrator.voice_transcriber import get_transcriber

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_path = self._media_dir / f"wa_voice_{ts}.ogg"

            # neonize download_any expects full Message protobuf, not sub-message
            root_msg = getattr(msg, "Message", None) or getattr(msg, "message", None)
            if root_msg is None:
                await self._send_text(chat_key, "Voice message container missing.")
                return None

            # Try to find AudioMessage on the unwrapped payload first;
            # if not found, still proceed with download_any which walks the
            # full protobuf tree and finds the actual audio data.
            audio_data = await self._client.download_any(root_msg)
            if not audio_data:
                await self._send_text(chat_key, "Failed to download voice message.")
                return None

            # Verify we got audio (OGG magic bytes) rather than garbage
            if audio_data[:4] != b"OggS":
                logger.warning(
                    "Voice download did not yield OGG data (got %r…). "
                    "Proceeding anyway.", audio_data[:8]
                )

            local_path.write_bytes(audio_data)
            logger.debug(f"Voice saved to {local_path} ({len(audio_data)} bytes)")

            transcriber = get_transcriber()
            transcript = await transcriber.transcribe(local_path)

            if transcript.startswith("[Transcription error]"):
                logger.error(f"Voice transcription failed: {transcript}")
                await self._send_text(chat_key, "Failed to transcribe voice message.")
                return None

            logger.info(f"Voice transcribed: {len(transcript)} chars")
            return f"[Voice message transcription] {transcript}"

        except Exception as e:
            logger.error(f"Voice handling error: {e}", exc_info=True)
            await self._send_text(chat_key, "Failed to process voice message.")
            return None

    def _detect_media_kind(self, msg) -> str | None:
        payload = self._message_payload(msg)
        if payload is None:
            return None

        def _nonempty(obj) -> bool:
            if obj is None:
                return False
            lf = getattr(obj, "ListFields", None)
            if callable(lf):
                try:
                    return len(lf()) > 0
                except Exception:
                    return False
            return True

        audio = getattr(payload, "AudioMessage", None) or getattr(payload, "audioMessage", None)
        if _nonempty(audio):
            if any(
                getattr(audio, k, None)
                for k in ("URL", "DirectPath", "url", "direct_path", "Mimetype", "mimetype")
            ):
                return "voice"

        image = getattr(payload, "ImageMessage", None) or getattr(payload, "imageMessage", None)
        if _nonempty(image):
            return "photo"

        document = getattr(payload, "DocumentMessage", None) or getattr(payload, "documentMessage", None)
        if _nonempty(document):
            return "document"

        video = getattr(payload, "VideoMessage", None) or getattr(payload, "videoMessage", None)
        if _nonempty(video):
            return "video"

        # Fallback: deep protobuf introspection for wrapped/nested media
        root_msg = getattr(msg, "Message", None) or getattr(msg, "message", None)
        if root_msg is not None:
            try:
                from google.protobuf.json_format import MessageToDict
                as_dict = MessageToDict(root_msg, preserving_proto_field_name=True)
                if isinstance(as_dict, dict):
                    return self._detect_media_from_dict(as_dict)
            except Exception:
                pass

        return None

    def _detect_media_from_dict(self, d: dict, depth: int = 0) -> str | None:
        """Recursively scan a protobuf-as-dict for media type indicators."""
        if depth > 8 or not isinstance(d, dict):
            return None
        # Direct key match
        media_keys = {
            "audioMessage": "voice", "AudioMessage": "voice",
            "pttMessage": "voice",
            "imageMessage": "photo", "ImageMessage": "photo",
            "documentMessage": "document", "DocumentMessage": "document",
            "videoMessage": "video", "VideoMessage": "video",
        }
        for key, kind in media_keys.items():
            val = d.get(key)
            if isinstance(val, dict) and val:
                return kind
        # Recurse into nested dicts
        for key, val in d.items():
            if isinstance(val, dict) and val:
                found = self._detect_media_from_dict(val, depth + 1)
                if found:
                    return found
        return None

    async def _handle_media_message(self, msg, chat_key: str):
        """Handle non-voice media (image/document/video) and return (prompt, source_kind)."""
        payload = self._message_payload(msg)
        if payload is None:
            return None, None

        media_fields = [
            ("photo", "ImageMessage", ".jpg"),
            ("photo", "imageMessage", ".jpg"),
            ("document", "DocumentMessage", ""),
            ("document", "documentMessage", ""),
            ("video", "VideoMessage", ".mp4"),
            ("video", "videoMessage", ".mp4"),
        ]

        def _nonempty(obj) -> bool:
            if obj is None:
                return False
            lf = getattr(obj, "ListFields", None)
            if callable(lf):
                try:
                    return len(lf()) > 0
                except Exception:
                    return False
            return True

        media_obj = None
        media_kind = None
        default_ext = ""
        for kind, field, ext in media_fields:
            obj = getattr(payload, field, None)
            if _nonempty(obj):
                media_obj = obj
                media_kind = kind
                default_ext = ext
                break

        if media_obj is None:
            return None, None

        try:
            # neonize download_any expects full Message protobuf, not sub-message payload
            root_msg = getattr(msg, "Message", None) or getattr(msg, "message", None)
            if root_msg is None:
                await self._send_text(chat_key, f"Failed to locate {media_kind} message container.")
                return None, None
            data = await self._client.download_any(root_msg)
        except Exception as e:
            logger.error(f"Failed to download WhatsApp {media_kind}: {e}", exc_info=True)
            await self._send_text(chat_key, f"Failed to download {media_kind} message.")
            return None, None

        if not data:
            await self._send_text(chat_key, f"Empty {media_kind} payload received.")
            return None, None

        # Post-download: detect actual content type by magic bytes
        # This catches misclassified voice messages (OGG audio saved as .jpg)
        if data[:4] == b"OggS":
            logger.info("Post-download reclassification: data is OGG audio (voice), not %s", media_kind)
            return await self._reclassify_as_voice(data, chat_key)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = getattr(media_obj, "FileName", "") or getattr(media_obj, "file_name", "") or ""
        mime = getattr(media_obj, "Mimetype", "") or getattr(media_obj, "mimetype", "") or ""

        ext = default_ext
        if not ext and file_name and "." in file_name:
            ext = "." + file_name.split(".")[-1]
        if not ext and mime:
            mime_map = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "application/pdf": ".pdf",
                "video/mp4": ".mp4",
            }
            ext = mime_map.get(mime.lower(), "")

        local_path = self._media_dir / f"wa_{media_kind}_{ts}{ext}"
        local_path.write_bytes(data)

        caption = ""
        for cap_name in ("Caption", "caption", "Text", "text"):
            cap = getattr(media_obj, cap_name, None)
            if isinstance(cap, str) and cap.strip():
                caption = cap.strip()
                break

        if media_kind == "photo":
            prompt = f"[WhatsApp photo] User sent an image saved at {local_path}. Analyze it and answer the user."
            if caption:
                prompt += f" Caption: {caption}"
        elif media_kind == "document":
            prompt = f"[WhatsApp document] User sent a document saved at {local_path}. Read/analyze and answer the user."
            if caption:
                prompt += f" Caption: {caption}"
        else:
            prompt = f"[WhatsApp video] User sent a video saved at {local_path}. Analyze and answer the user."
            if caption:
                prompt += f" Caption: {caption}"

        logger.info(
            "WhatsApp media handled: kind=%s bytes=%s path=%s",
            media_kind,
            len(data),
            local_path,
        )
        return prompt, media_kind

    async def _reclassify_as_voice(self, audio_data: bytes, chat_key: str) -> tuple:
        """Handle audio data that was misclassified as non-voice media."""
        try:
            from orchestrator.voice_transcriber import get_transcriber

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_path = self._media_dir / f"wa_voice_{ts}.ogg"
            local_path.write_bytes(audio_data)
            logger.info("Reclassified voice saved: %s (%d bytes)", local_path, len(audio_data))

            transcriber = get_transcriber()
            transcript = await transcriber.transcribe(local_path)

            if transcript.startswith("[Transcription error]"):
                logger.error(f"Voice transcription failed after reclassify: {transcript}")
                await self._send_text(chat_key, "Failed to transcribe voice message.")
                return None, None

            logger.info(f"Reclassified voice transcribed: {len(transcript)} chars")
            return f"[Voice message transcription] {transcript}", "voice_transcript"

        except Exception as e:
            logger.error(f"Reclassify-as-voice error: {e}", exc_info=True)
            await self._send_text(chat_key, "Failed to process voice message.")
            return None, None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _message_payload(self, msg):
        """Return the innermost message payload, unwrapping common WA wrappers."""
        payload = getattr(msg, "Message", None) or getattr(msg, "message", None)
        if payload is None:
            return None

        # Common wrappers in WhatsApp protobufs (ephemeral/view-once/edited/futureproof)
        wrapper_fields = (
            "EphemeralMessage", "ephemeralMessage",
            "ViewOnceMessage", "viewOnceMessage",
            "ViewOnceMessageV2", "viewOnceMessageV2",
            "ViewOnceMessageV2Extension",
            "EditedMessage", "editedMessage",
            "DocumentWithCaptionMessage",
            "FutureProofMessage",
            "DeviceSentMessage", "deviceSentMessage",
            "PtvMessage", "ptvMessage",            # push-to-talk voice
            "BcallMessage", "bcallMessage",
        )

        for _ in range(10):
            progressed = False
            for field in wrapper_fields:
                outer = getattr(payload, field, None)
                if outer is None:
                    continue
                inner = getattr(outer, "Message", None) or getattr(outer, "message", None)
                if inner is not None and inner is not payload:
                    payload = inner
                    progressed = True
                    break
            if not progressed:
                break
        return payload

    def _extract_text(self, msg) -> str:
        """Extract plain text from a neonize MessageEv."""
        payload = self._message_payload(msg)
        if payload is None:
            payload = getattr(msg, "Message", None) or getattr(msg, "message", None)
        if payload is None:
            return ""

        def _try_text(obj, depth: int = 0) -> str:
            if obj is None or depth > 4:
                return ""

            # Direct field probes across naming styles
            for name in (
                "Conversation", "conversation",
                "Text", "text",
                "Caption", "caption",
                "MatchedText", "matchedText",
            ):
                val = getattr(obj, name, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, (bytes, bytearray)):
                    try:
                        decoded = bytes(val).decode("utf-8", errors="ignore").strip()
                        if decoded:
                            return decoded
                    except Exception:
                        pass

            # Known nested text carriers
            for nested_name in (
                "ExtendedTextMessage", "extendedTextMessage", "extended_text_message",
                "ImageMessage", "imageMessage", "image_message",
                "VideoMessage", "videoMessage", "video_message",
                "DocumentMessage", "documentMessage", "document_message",
            ):
                nested = getattr(obj, nested_name, None)
                if nested is not None:
                    got = _try_text(nested, depth + 1)
                    if got:
                        return got

            # Generic protobuf reflection fallback
            list_fields = getattr(obj, "ListFields", None)
            if callable(list_fields):
                try:
                    for field_desc, value in list_fields():
                        fname = str(getattr(field_desc, "name", "")).lower()
                        if isinstance(value, str) and value.strip():
                            if fname in {"conversation", "text", "caption"} or "text" in fname or "caption" in fname:
                                return value.strip()
                        # recurse nested messages
                        if not isinstance(value, (str, bytes, int, float, bool)):
                            got = _try_text(value, depth + 1)
                            if got:
                                return got
                except Exception:
                    pass

            # Last-resort fallback: parse protobuf debug string
            try:
                raw = str(obj)
                for key in ("conversation", "text", "caption"):
                    m = re.search(rf'{key}:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw)
                    if m:
                        candidate = bytes(m.group(1), "utf-8").decode("unicode_escape").strip()
                        if candidate:
                            return candidate
            except Exception:
                pass

            return ""

        text = _try_text(payload)
        if text:
            return text

        root = getattr(msg, "Message", None) or getattr(msg, "message", None) or payload

        # Fallback 1: parse protobuf debug text directly
        try:
            raw = str(root)
            for key in ("conversation", "text", "caption"):
                m = re.search(rf'{key}:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw)
                if m:
                    candidate = bytes(m.group(1), "utf-8").decode("unicode_escape").strip()
                    if candidate:
                        return candidate
        except Exception:
            pass

        # Fallback 2: protobuf -> dict -> recursive text search
        try:
            from google.protobuf.json_format import MessageToDict
            data = MessageToDict(root, preserving_proto_field_name=True)

            def _find_in_dict(node, depth: int = 0):
                if depth > 8:
                    return ""
                if isinstance(node, str):
                    return node.strip()
                if isinstance(node, dict):
                    for k in ("conversation", "text", "caption", "matched_text"):
                        v = node.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                    for _k, v in node.items():
                        got = _find_in_dict(v, depth + 1)
                        if got:
                            return got
                elif isinstance(node, list):
                    for item in node:
                        got = _find_in_dict(item, depth + 1)
                        if got:
                            return got
                return ""

            return _find_in_dict(data)
        except Exception:
            return ""

    def _is_voice(self, msg) -> bool:
        """True if the message contains audio/voice content."""
        payload = self._message_payload(msg)
        if payload is None:
            return False
        audio = getattr(payload, "AudioMessage", None) or getattr(payload, "audioMessage", None)
        if audio is None:
            return False
        try:
            lf = getattr(audio, "ListFields", None)
            if callable(lf) and len(lf()) == 0:
                return False
        except Exception:
            pass
        return bool(
            getattr(audio, "URL", None)
            or getattr(audio, "DirectPath", None)
            or getattr(audio, "url", None)
            or getattr(audio, "direct_path", None)
            or getattr(audio, "Mimetype", None)
            or getattr(audio, "mimetype", None)
        )

    def _jid_str(self, jid) -> str:
        """Convert a neonize JID object to a stable string key."""
        try:
            from neonize.client import Jid2String
            return Jid2String(jid)
        except Exception:
            # Fallback: construct manually from proto fields
            server = getattr(jid, "Server", "s.whatsapp.net")
            user = getattr(jid, "User", "unknown")
            return f"{user}@{server}"

    def _get_runtime(self, agent_name: str):
        for rt in self.orchestrator.runtimes:
            if rt.name == agent_name:
                return rt
        return None

    def _refresh_runtime_config(self, force: bool = False):
        try:
            stat = self._config_path.stat()
        except FileNotFoundError:
            return
        if not force and stat.st_mtime_ns == self._config_mtime_ns:
            return
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning("Failed to reload WhatsApp config from %s: %s", self._config_path, e)
            return
        wa_cfg = (raw.get("global", {}).get("whatsapp") or {}).copy()
        raw_numbers = wa_cfg.get("allowed_numbers", []) or []
        self._allowed_numbers = {str(number).strip() for number in raw_numbers if str(number).strip()}
        raw_chat_ids = wa_cfg.get("allowed_chat_ids", []) or []
        self._allowed_chat_ids = {str(cid).strip() for cid in raw_chat_ids if str(cid).strip()}
        self.wa_cfg = wa_cfg
        self._config_mtime_ns = stat.st_mtime_ns
        logger.info(
            "Reloaded WhatsApp config: allowed_numbers=%s allowed_chat_ids=%s",
            sorted(self._allowed_numbers),
            sorted(self._allowed_chat_ids),
        )

    def _phone_candidates(self, jid) -> set[str]:
        candidates: set[str] = set()
        if jid is None:
            return candidates
        user = str(getattr(jid, "User", "") or "").strip()
        if user.isdigit():
            candidates.add(f"+{user}")
        jid_text = self._jid_str(jid)
        if "@" in jid_text:
            user_part = jid_text.split("@", 1)[0].strip()
            if user_part.isdigit():
                candidates.add(f"+{user_part}")
        return candidates

    def _ensure_file_logging(self):
        log_dir = Path(str(self.global_cfg.base_logs_dir)) / "whatsapp"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "transport.log"
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path:
                return
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)

    async def send_text_to_number(self, phone_number: str, text: str):
        self._refresh_runtime_config()
        normalized = phone_number.strip()
        if not normalized:
            raise ValueError("Phone number is required.")
        if normalized.startswith("+"):
            normalized = normalized[1:]
        if not normalized.isdigit():
            raise ValueError(f"Invalid WhatsApp phone number: {phone_number}")
        if self._client is None:
            raise RuntimeError("WhatsApp client is not connected.")
        is_connected = self._client.is_connected
        if inspect.isawaitable(is_connected):
            is_connected = await is_connected
        if not is_connected:
            raise RuntimeError("WhatsApp client is not connected.")
        chat_key = f"{normalized}@s.whatsapp.net"
        logger.info("Admin-triggered WhatsApp send: number=%s chars=%s", phone_number, len(text))
        await self._send_text(chat_key, text)

    async def _send_text(self, chat_key: str, text: str):
        """Send a plain-text message to a WhatsApp chat."""
        if self._client is None:
            logger.warning("Cannot send: WhatsApp client not connected.")
            return
        preview = text[:160].replace("\n", " ")
        if len(text) > 160:
            preview += "..."
        _print_wa_line(_C_WA_OUT, chat_key, f"<- {preview}")

        # Retrieve the JID object from cache, or parse from string
        jid = self._jid_cache.get(chat_key)
        if jid is None:
            try:
                from neonize.client import build_jid
                # chat_key format: "number@server" or "uuid@g.us"
                parts = chat_key.split("@")
                server = parts[1] if len(parts) == 2 else "s.whatsapp.net"
                jid = build_jid(parts[0], server)
            except Exception as e:
                logger.error(f"Cannot parse JID from '{chat_key}': {e}")
                return

        try:
            chunks = _split_text(text, limit=4000)
            for chunk in chunks:
                logger.info("Sending WhatsApp text: chat=%s chars=%s", chat_key, len(chunk))
                await self._client.send_message(jid, chunk)
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message to {chat_key}: {e}", exc_info=True)

    async def _send_voice(self, chat_key: str, audio_path: Path):
        if self._client is None:
            logger.warning("Cannot send voice: WhatsApp client not connected.")
            return

        jid = self._jid_cache.get(chat_key)
        if jid is None:
            try:
                from neonize.client import build_jid
                parts = chat_key.split("@")
                server = parts[1] if len(parts) == 2 else "s.whatsapp.net"
                jid = build_jid(parts[0], server)
            except Exception as e:
                logger.error(f"Cannot parse JID from '{chat_key}' for voice send: {e}")
                return

        data = audio_path.read_bytes()
        try:
            send_audio = getattr(self._client, "send_audio", None)
            if callable(send_audio):
                for args, kwargs in (
                    ((jid, data), {"ptt": True, "mime_type": "audio/ogg"}),
                    ((jid, data), {"ptt": True}),
                    ((jid, str(audio_path)), {"ptt": True, "mime_type": "audio/ogg"}),
                    ((jid, str(audio_path)), {"ptt": True}),
                ):
                    try:
                        result = send_audio(*args, **kwargs)
                        if inspect.isawaitable(result):
                            await result
                        logger.info(f"Sent WhatsApp voice reply to {chat_key} ({audio_path.name})")
                        return
                    except TypeError:
                        continue

            build_audio = getattr(self._client, "build_audio_message", None)
            send_message = getattr(self._client, "send_message", None)
            if callable(build_audio) and callable(send_message):
                for kwargs in (
                    {"file": data, "mime_type": "audio/ogg", "ptt": True},
                    {"file": data, "ptt": True},
                    {"data": data, "mime_type": "audio/ogg", "ptt": True},
                ):
                    try:
                        msg = build_audio(**kwargs)
                        result = send_message(jid, msg)
                        if inspect.isawaitable(result):
                            await result
                        logger.info(f"Sent WhatsApp voice reply to {chat_key} ({audio_path.name})")
                        return
                    except TypeError:
                        continue

            raise RuntimeError("No compatible neonize audio send method found.")
        except Exception as e:
            logger.error(f"Failed to send WhatsApp voice to {chat_key}: {e}", exc_info=True)


def _split_text(text: str, limit: int = 4000) -> list[str]:
    """Split text into chunks at newline boundaries, max `limit` chars each."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
