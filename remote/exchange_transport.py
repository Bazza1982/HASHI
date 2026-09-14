"""Outbound-only client for the independent HASHI Exchange service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiohttp

from orchestrator.exchange_config import (
    ExchangeConfig,
    load_exchange_config,
    load_exchange_credential,
    published_agent_records,
)
from orchestrator.exchange_ingress import (
    build_exchange_ingress_claims,
    render_exchange_hchat_prompt,
)
from orchestrator.message_context import seal_connector_evidence
from remote.exchange_outbox import (
    ExchangeOutbox,
    ExchangeOutboxConflict,
    OutboxRecord,
)
from remote.exchange_protocol import (
    AUTHORIZED_ROUTES_CAPABILITY,
    CAPABILITIES,
    MAX_FRAME_BYTES,
    SUBPROTOCOL,
    ack_frame,
    canonical_json,
    decode_server_frame,
    hello_frame,
    parse_timestamp,
    publish_frame,
    resolve_frame,
    routes_frame,
    send_frame,
    utc_timestamp,
    validate_delivery_deadline,
)
from remote.internet_address import ExchangeAddress, PublicAddress
from remote.local_http import local_http_hosts, local_http_url
from remote.security.shared_token import load_shared_token


logger = logging.getLogger(__name__)


class ExchangeTransportError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = str(code or "EXCHANGE_UNAVAILABLE")
        self.retryable = bool(retryable)
        super().__init__(self.code)


class ExchangeTransport:
    """One bounded WSS connection per registered HASHI instance."""

    def __init__(
        self,
        *,
        hashi_root: Path | str,
        instance_info: Mapping[str, Any],
        workbench_port: int,
        clock=time.time,
        random_source: random.Random | None = None,
    ):
        self.hashi_root = Path(hashi_root)
        self.instance_info = dict(instance_info)
        self.workbench_port = int(workbench_port)
        self.clock = clock
        self._random = random_source or random.Random()
        self.outbox = ExchangeOutbox(
            self.hashi_root / "state" / "exchange_outbox.sqlite3"
        )
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._welcome: dict[str, Any] | None = None
        self._config: ExchangeConfig | None = None
        self._credential_digest = ""
        self._published_records: list[dict[str, Any]] = []
        self._published_agents: set[str] = set()
        self._publication_revision = 0
        self._request_waiters: dict[str, asyncio.Future] = {}
        self._receipt_waiters: dict[str, asyncio.Future] = {}
        self._last_error_code: str | None = None
        self._connected_at: float | None = None
        self._last_outbox_purge = 0.0
        self._negotiated_capabilities: set[str] = set()
        self._authorized_routes: list[dict[str, Any]] = []
        self._routes_grant_revision: int | None = None
        self._routes_refreshed_at: str | None = None
        self._routes_observed_at: float | None = None
        self._routes_stale = False
        self._routes_supported: bool | None = None

    @property
    def enabled(self) -> bool:
        try:
            return load_exchange_config(self.hashi_root).enabled
        except (OSError, ValueError):
            return False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            config = load_exchange_config(self.hashi_root)
        except (OSError, ValueError) as exc:
            raise ExchangeTransportError("INVALID_CONFIGURATION") from exc
        self._config = config
        if not config.enabled:
            return
        if not load_shared_token(self.hashi_root):
            raise ExchangeTransportError("LOCAL_INGRESS_AUTH_UNAVAILABLE")
        # Resolve once before backgrounding so an enabled-but-broken
        # credential reference fails closed and is visible at startup.
        load_exchange_credential(self.hashi_root, config)
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(),
            name="hashi-exchange-transport",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        self._ready_event.clear()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._fail_waiters("EXCHANGE_STOPPED")
        if self._routes_supported:
            self._routes_stale = True

    def status(self) -> dict[str, Any]:
        config = self._config
        connected = bool(self._ready_event.is_set())
        return {
            "ok": True,
            "enabled": bool(config and config.enabled),
            "connected": connected,
            "state": (
                "ready"
                if self._ready_event.is_set()
                else ("disconnected" if config and config.enabled else "disabled")
            ),
            "authority_id": config.authority_id if config else None,
            "registered_instance_id": (
                config.registered_instance_id if config else None
            ),
            "instance_alias": config.instance_alias if config else None,
            "published_agents": sorted(self._published_agents),
            "endpoint": str(config.url) if config and config.url else None,
            "instance_address": str(
                (self._welcome or {}).get("instance_address") or ""
            ) or None,
            "connection_epoch": (
                str((self._welcome or {}).get("epoch") or "") or None
            ),
            "connected_at": self._connected_at,
            "last_error_code": self._last_error_code,
            "capabilities": sorted(self._negotiated_capabilities),
            "client_capabilities": list(CAPABILITIES),
            "authorized_routes_supported": self._routes_supported,
            "authorized_routes": [
                {
                    "to": dict(item["to"]),
                    "message_kinds": list(item["message_kinds"]),
                    "available": bool(item["available"]),
                }
                for item in self._authorized_routes
            ],
            "routes_grant_revision": self._routes_grant_revision,
            "routes_refreshed_at": self._routes_refreshed_at,
            "routes_observed_at": self._routes_observed_at,
            "routes_stale": bool(self._routes_stale or (
                not connected and self._authorized_routes
            )),
        }

    async def _run_forever(self) -> None:
        delay = 0.5
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_code = self._safe_error_code(exc)
                logger.warning(
                    "Exchange connection ended (%s)", self._last_error_code
                )
            finally:
                self._ready_event.clear()
                self._ws = None
                self._welcome = None
                self._connected_at = None
                self._published_agents.clear()
                self._negotiated_capabilities.clear()
                if self._routes_supported:
                    self._routes_stale = True
                self._fail_waiters("EXCHANGE_RECONNECTING")
            if self._stop_event.is_set():
                break
            jitter = self._random.uniform(0.0, min(1.0, delay / 2))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=min(30.0, delay + jitter),
                )
            except asyncio.TimeoutError:
                pass
            delay = min(30.0, delay * 2)

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code[:64]
        if isinstance(exc, asyncio.TimeoutError):
            return "TIMEOUT"
        if isinstance(exc, aiohttp.ClientConnectorCertificateError):
            return "TLS_CERTIFICATE_REJECTED"
        if isinstance(exc, aiohttp.ClientError):
            return "CONNECTION_FAILED"
        return type(exc).__name__.upper()[:64]

    def _current_config_and_token(self) -> tuple[ExchangeConfig, str]:
        config = load_exchange_config(self.hashi_root)
        if not config.enabled:
            raise ExchangeTransportError("EXCHANGE_DISABLED")
        token = load_exchange_credential(self.hashi_root, config)
        return config, token

    @staticmethod
    async def _reject_redirect(_session, _context, _params) -> None:
        raise ExchangeTransportError("REDIRECT_REJECTED")

    async def _connect_once(self) -> None:
        config, credential = self._current_config_and_token()
        if (
            self._config is not None
            and config.connection_fingerprint()
            != self._config.connection_fingerprint()
        ):
            self._clear_authorized_routes()
        self._config = config
        credential_digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10)
        headers = {"Authorization": f"Bearer {credential}"}
        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_redirect.append(self._reject_redirect)
        async with aiohttp.ClientSession(
            timeout=timeout,
            trace_configs=[trace_config],
        ) as session:
            async with session.ws_connect(
                str(config.url),
                headers=headers,
                protocols=(SUBPROTOCOL,),
                autoping=True,
                autoclose=True,
                max_msg_size=65536,
            ) as ws:
                if ws.protocol != SUBPROTOCOL:
                    raise ExchangeTransportError("SUBPROTOCOL_REJECTED")
                self._ws = ws
                await self._send_frame(ws, hello_frame())
                welcome = await asyncio.wait_for(self._receive_frame(ws), timeout=5)
                if welcome.get("type") != "welcome":
                    raise ExchangeTransportError("WELCOME_REQUIRED")
                self._validate_welcome(config, welcome)
                negotiated = {
                    str(value) for value in welcome.get("capabilities") or []
                }
                self._negotiated_capabilities = negotiated
                if AUTHORIZED_ROUTES_CAPABILITY in negotiated:
                    self._routes_supported = True
                    self._routes_stale = True
                else:
                    self._clear_authorized_routes(supported=False)
                records = published_agent_records(self.hashi_root, config)
                max_agents = int(
                    (welcome.get("limits") or {}).get(
                        "max_published_agents",
                        0,
                    )
                )
                if len(records) > max_agents:
                    raise ExchangeTransportError("PUBLISH_LIMIT_EXCEEDED")
                self._publication_revision = 1
                request_id = self._new_id("pub")
                await self._send_frame(
                    ws,
                    publish_frame(
                        request_id=request_id,
                        revision=self._publication_revision,
                        agents=records,
                    ),
                )
                published = await asyncio.wait_for(
                    self._receive_frame(ws), timeout=5
                )
                if (
                    published.get("type") != "published"
                    or published.get("request_id") != request_id
                    or published.get("revision") != self._publication_revision
                ):
                    raise ExchangeTransportError("PUBLISH_REJECTED")
                self._welcome = welcome
                self._config = config
                self._credential_digest = credential_digest
                self._published_records = records
                self._published_agents = {
                    str(item["agent_id"]) for item in records
                }
                self._connected_at = self.clock()
                self._last_error_code = None
                self._ready_event.set()

                delivery_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
                reader = asyncio.create_task(
                    self._reader_loop(ws, delivery_queue),
                    name="hashi-exchange-reader",
                )
                worker = asyncio.create_task(
                    self._delivery_loop(ws, delivery_queue),
                    name="hashi-exchange-delivery",
                )
                watcher = asyncio.create_task(
                    self._publication_loop(ws),
                    name="hashi-exchange-publication",
                )
                recovery = asyncio.create_task(
                    self._recover_outbox(ws),
                    name="hashi-exchange-outbox-recovery",
                )
                tasks = {reader, worker, watcher, recovery}
                if AUTHORIZED_ROUTES_CAPABILITY in negotiated:
                    tasks.add(asyncio.create_task(
                        self._authorized_routes_loop(ws),
                        name="hashi-exchange-authorized-routes",
                    ))
                try:
                    watched = set(tasks)
                    while watched:
                        done, watched = await asyncio.wait(
                            watched,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        recovery_finished = recovery in done
                        if recovery_finished:
                            recovery.result()
                            done.remove(recovery)
                            if not done:
                                continue
                        for task in done:
                            task.result()
                        raise ExchangeTransportError(
                            "CONNECTION_CLOSED",
                            retryable=True,
                        )
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(
                        *tasks, return_exceptions=True
                    )

    def _validate_welcome(
        self,
        config: ExchangeConfig,
        welcome: Mapping[str, Any],
    ) -> None:
        try:
            instance = PublicAddress.parse(
                f"x@{str(welcome.get('instance_address') or '')}"
            )
        except ValueError as exc:
            raise ExchangeTransportError("WELCOME_IDENTITY_MISMATCH") from exc
        if (
            welcome.get("authority_id") != config.authority_id
            or welcome.get("registered_instance_id")
            != config.registered_instance_id
            or instance.instance_alias != config.instance_alias
            or parse_timestamp(welcome.get("lease_expires_at")) <= self.clock()
        ):
            raise ExchangeTransportError("WELCOME_IDENTITY_MISMATCH")

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    async def _receive_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> dict[str, Any]:
        message = await ws.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            return decode_server_frame(message.data)
        if message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        }:
            raise ExchangeTransportError("CONNECTION_CLOSED", retryable=True)
        if message.type == aiohttp.WSMsgType.ERROR:
            raise ExchangeTransportError("CONNECTION_FAILED", retryable=True)
        raise ExchangeTransportError("BINARY_FRAME_REJECTED")

    async def _send_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        frame: Mapping[str, Any],
    ) -> None:
        if ws.closed or ws is not self._ws:
            raise ExchangeTransportError("STALE_CONNECTION", retryable=True)
        raw = canonical_json(dict(frame)).decode("ascii")
        async with self._write_lock:
            await ws.send_str(raw)

    async def _reader_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        delivery_queue: asyncio.Queue,
    ) -> None:
        while not ws.closed:
            frame = await self._receive_frame(ws)
            kind = frame["type"]
            if kind == "delivery":
                self._validate_delivery_binding(frame)
                try:
                    delivery_queue.put_nowait(frame)
                except asyncio.QueueFull as exc:
                    await ws.close(code=1013)
                    raise ExchangeTransportError("LOCAL_BACKPRESSURE") from exc
                continue
            if kind in {"resolved", "published", "authorized_routes"}:
                request_id = str(frame.get("request_id") or "")
                waiter = self._request_waiters.pop(request_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame)
                continue
            if kind == "receipt":
                message_id = str(frame.get("message_id") or "")
                status = str(frame.get("status") or "")
                code = str(frame.get("code") or "") or None
                if self.outbox.get(message_id) is not None:
                    self.outbox.set_state(message_id, status, code=code)
                request_id = str(frame.get("request_id") or "")
                waiter = (
                    self._request_waiters.pop(request_id, None)
                    if request_id
                    else self._receipt_waiters.pop(message_id, None)
                )
                if waiter is not None and not waiter.done():
                    waiter.set_result(frame)
                continue
            if kind == "error":
                error = ExchangeTransportError(
                    str(frame.get("code") or "EXCHANGE_ERROR"),
                    retryable=bool(frame.get("retryable")),
                )
                request_id = str(frame.get("request_id") or "")
                message_id = str(frame.get("message_id") or "")
                waiter = None
                if request_id:
                    waiter = self._request_waiters.pop(request_id, None)
                if waiter is None and message_id:
                    waiter = self._receipt_waiters.pop(message_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_exception(error)
                    continue
                if error.code in {
                    "AUTH_FAILED",
                    "LEASE_EXPIRED",
                    "UNSUPPORTED_VERSION",
                    "UNSUPPORTED_CAPABILITY",
                }:
                    raise error
                logger.warning("Exchange rejected an uncorrelated frame (%s)", error.code)
                continue
            raise ExchangeTransportError("UNEXPECTED_SERVER_FRAME")

    def _validate_delivery_binding(self, delivery: Mapping[str, Any]) -> None:
        config = self._config
        welcome = self._welcome
        if config is None or welcome is None:
            raise ExchangeTransportError("STALE_CONNECTION")
        validate_delivery_deadline(delivery, now=self.clock())
        sender = ExchangeAddress.from_mapping(delivery["sender"])
        recipient = ExchangeAddress.from_mapping(delivery["recipient"])
        welcome_instance = PublicAddress.parse(
            f"x@{welcome['instance_address']}"
        )
        if (
            sender.authority_id != config.authority_id
            or recipient.authority_id != config.authority_id
            or recipient.actor_id != welcome["actor_id"]
            or recipient.registered_instance_id
            != config.registered_instance_id
            or recipient.public_address.instance_address
            != welcome_instance.instance_address
            or delivery["recipient_epoch"] != welcome["epoch"]
            or recipient.agent_id not in self._published_agents
        ):
            raise ExchangeTransportError("RECIPIENT_BINDING_MISMATCH")

    async def _delivery_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        queue: asyncio.Queue,
    ) -> None:
        while not ws.closed:
            delivery = await queue.get()
            try:
                await self._accept_ack_schedule(ws, delivery)
            finally:
                queue.task_done()

    async def _accept_ack_schedule(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        delivery: Mapping[str, Any],
    ) -> None:
        welcome = self._welcome
        if welcome is None:
            raise ExchangeTransportError("STALE_CONNECTION")
        prompt = render_exchange_hchat_prompt(delivery)
        claims = build_exchange_ingress_claims(
            delivery=delivery,
            welcome=welcome,
        )
        evidence = seal_connector_evidence(
            self.hashi_root,
            claims=claims,
            prompt=prompt,
        )
        if evidence is None:
            raise ExchangeTransportError("LOCAL_INGRESS_AUTH_UNAVAILABLE")
        status, result = await self._post_ingress(
            "/api/bridge/exchange/accept",
            prompt=prompt,
            evidence=evidence,
        )
        if status >= 500:
            await ws.close(code=1011)
            raise ExchangeTransportError("LOCAL_INGRESS_UNAVAILABLE", retryable=True)
        if status >= 400 or not result.get("ok"):
            remote_code = str(result.get("code") or "")
            ack_code = (
                "PRIVATE_AUTH_FAILED"
                if remote_code == "PRIVATE_AUTH_FAILED"
                else (
                    "PERMISSION_DENIED"
                    if status in {401, 403, 404}
                    else "INVALID_MESSAGE"
                )
            )
            await self._send_frame(
                ws,
                ack_frame(
                    delivery_id=str(delivery["delivery_id"]),
                    message_id=str(delivery["message_id"]),
                    status="rejected",
                    code=ack_code,
                ),
            )
            return
        # The inbox transaction is durable at this point.  ACK precedes PAO
        # scheduling by contract.
        await self._send_frame(
            ws,
            ack_frame(
                delivery_id=str(delivery["delivery_id"]),
                message_id=str(delivery["message_id"]),
                status="delivered",
            ),
        )
        schedule_status, schedule = await self._post_ingress(
            "/api/bridge/exchange/schedule",
            prompt=prompt,
            evidence=evidence,
        )
        if schedule_status >= 400 or not schedule.get("ok"):
            # Workbench owns a periodic recovery loop for this already-ACKed
            # accepted row.  Never ask Exchange to create a second delivery.
            logger.warning(
                "Exchange inbox scheduling deferred (%s)",
                str(schedule.get("code") or f"HTTP_{schedule_status}")[:64],
            )

    async def _post_ingress(
        self,
        path: str,
        *,
        prompt: str,
        evidence: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = {
            "prompt": prompt,
            "connector_evidence": dict(evidence),
        }
        timeout = aiohttp.ClientTimeout(total=10)
        last_status = 503
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for host in local_http_hosts():
                url = local_http_url(self.workbench_port, path, host=host)
                try:
                    async with session.post(url, json=payload) as response:
                        last_status = int(response.status)
                        value = await response.json(content_type=None)
                        if isinstance(value, dict):
                            return last_status, value
                        return 502, {
                            "ok": False,
                            "code": "INVALID_WORKBENCH_RESPONSE",
                        }
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue
        return last_status, {
            "ok": False,
            "code": "LOCAL_INGRESS_UNAVAILABLE",
        }

    async def _publication_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        while not ws.closed:
            await asyncio.sleep(1)
            now = self.clock()
            if now - self._last_outbox_purge >= 60:
                await asyncio.to_thread(self.outbox.purge, now=now)
                self._last_outbox_purge = now
            config, credential = self._current_config_and_token()
            if (
                self._config is None
                or config.connection_fingerprint()
                != self._config.connection_fingerprint()
                or hashlib.sha256(credential.encode("utf-8")).hexdigest()
                != self._credential_digest
            ):
                await ws.close(code=1000)
                return
            records = published_agent_records(self.hashi_root, config)
            if records == self._published_records:
                continue
            self._publication_revision += 1
            request_id = self._new_id("pub")
            waiter = self._new_request_waiter(request_id)
            try:
                await self._send_frame(
                    ws,
                    publish_frame(
                        request_id=request_id,
                        revision=self._publication_revision,
                        agents=records,
                    ),
                )
                response = await asyncio.wait_for(waiter, timeout=5)
            finally:
                if self._request_waiters.get(request_id) is waiter:
                    self._request_waiters.pop(request_id, None)
            if (
                response.get("type") != "published"
                or response.get("revision") != self._publication_revision
            ):
                raise ExchangeTransportError("PUBLISH_REJECTED")
            self._published_records = records
            self._published_agents = {
                str(item["agent_id"]) for item in records
            }
            if AUTHORIZED_ROUTES_CAPABILITY in self._negotiated_capabilities:
                await self._refresh_authorized_routes(ws)

    def _clear_authorized_routes(self, *, supported: bool | None = None) -> None:
        self._authorized_routes = []
        self._routes_grant_revision = None
        self._routes_refreshed_at = None
        self._routes_observed_at = None
        self._routes_stale = False
        self._routes_supported = supported

    async def _refresh_authorized_routes(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        if AUTHORIZED_ROUTES_CAPABILITY not in self._negotiated_capabilities:
            return
        request_id = self._new_id("routes")
        waiter = self._new_request_waiter(request_id)
        try:
            await self._send_frame(ws, routes_frame(request_id=request_id))
            response = await asyncio.wait_for(waiter, timeout=5)
        finally:
            if self._request_waiters.get(request_id) is waiter:
                self._request_waiters.pop(request_id, None)
        if response.get("type") != "authorized_routes":
            raise ExchangeTransportError("AUTHORIZED_ROUTES_REJECTED")
        self._authorized_routes = [
            {
                "to": dict(item["to"]),
                "message_kinds": list(item["message_kinds"]),
                "available": bool(item["available"]),
            }
            for item in response["routes"]
        ]
        self._routes_grant_revision = int(response["grant_revision"])
        self._routes_refreshed_at = str(response["refreshed_at"])
        self._routes_observed_at = self.clock()
        self._routes_stale = False
        self._routes_supported = True

    async def _authorized_routes_loop(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        while not ws.closed:
            await self._refresh_authorized_routes(ws)
            await asyncio.sleep(5)

    def _new_request_waiter(self, request_id: str) -> asyncio.Future:
        if request_id in self._request_waiters:
            raise ExchangeTransportError(
                "REQUEST_IN_FLIGHT",
                retryable=True,
            )
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._request_waiters[request_id] = waiter
        return waiter

    def _new_receipt_waiter(self, message_id: str) -> asyncio.Future:
        if message_id in self._receipt_waiters:
            raise ExchangeTransportError(
                "MESSAGE_IN_FLIGHT",
                retryable=True,
            )
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._receipt_waiters[message_id] = waiter
        return waiter

    async def _wait_ready(self, timeout: float = 10) -> None:
        if self._config is None or not self._config.enabled:
            raise ExchangeTransportError("EXCHANGE_DISABLED")
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ExchangeTransportError(
                "EXCHANGE_UNAVAILABLE", retryable=True
            ) from exc

    async def send_message(
        self,
        *,
        from_agent: str,
        to_address: str,
        text: str,
        message_id: str | None = None,
        conversation_id: str | None = None,
        message_type: str = "agent_message",
        in_reply_to: str | None = None,
        expires_in_seconds: int = 600,
        authorization_resources: list[str] | None = None,
        private_authorization_proofs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if authorization_resources or private_authorization_proofs:
            raise ExchangeTransportError("UNSUPPORTED_CAPABILITY")
        destination = PublicAddress.parse(to_address)
        await self._wait_ready()
        ws = self._ws
        if ws is None or ws.closed:
            raise ExchangeTransportError("EXCHANGE_UNAVAILABLE", retryable=True)
        normalized_sender = str(from_agent or "").strip().lower()
        if normalized_sender not in self._published_agents:
            raise ExchangeTransportError("SENDER_NOT_PUBLISHED")
        if message_id:
            existing = self.outbox.get(str(message_id))
            if existing is not None:
                self._validate_exact_retry(
                    existing,
                    from_agent=normalized_sender,
                    destination=destination,
                    text=str(text),
                    conversation_id=conversation_id,
                    message_type=message_type,
                    in_reply_to=in_reply_to,
                )
                return await self._transmit_record(
                    ws,
                    existing,
                    replayed=True,
                )
        resolve_id = self._new_id("resolve")
        resolve_waiter = self._new_request_waiter(resolve_id)
        try:
            await self._send_frame(
                ws,
                resolve_frame(
                    request_id=resolve_id,
                    from_agent=normalized_sender,
                    address=destination.canonical,
                ),
            )
            resolved = await asyncio.wait_for(resolve_waiter, timeout=10)
        finally:
            if self._request_waiters.get(resolve_id) is resolve_waiter:
                self._request_waiters.pop(resolve_id, None)
        target = resolved.get("to")
        if not isinstance(target, Mapping):
            raise ExchangeTransportError("RESOLVE_REJECTED")
        now = self.clock()
        lifetime = max(1, min(int(expires_in_seconds), 600))
        outbound = send_frame(
            message_id=message_id or self._new_id("msg"),
            conversation_id=conversation_id or self._new_id("conv"),
            from_agent=normalized_sender,
            to=target,
            created_at=utc_timestamp(now),
            expires_at=utc_timestamp(now + lifetime),
            message_type=message_type,
            in_reply_to=in_reply_to,
            text=str(text),
        )
        try:
            record = self.outbox.put(outbound)
        except ExchangeOutboxConflict as exc:
            raise ExchangeTransportError("IDEMPOTENCY_CONFLICT") from exc
        return await self._transmit_record(
            ws,
            record,
            replayed=record.replayed,
        )

    @staticmethod
    def _validate_exact_retry(
        record: OutboxRecord,
        *,
        from_agent: str,
        destination: PublicAddress,
        text: str,
        conversation_id: str | None,
        message_type: str,
        in_reply_to: str | None,
    ) -> None:
        frame = record.frame
        content = frame.get("content")
        if (
            record.from_agent != from_agent
            or record.to_address != destination.canonical
            or (
                conversation_id is not None
                and record.conversation_id != str(conversation_id)
            )
            or frame.get("message_type") != message_type
            or frame.get("in_reply_to") != in_reply_to
            or not isinstance(content, Mapping)
            or content.get("text") != text
        ):
            raise ExchangeTransportError("IDEMPOTENCY_CONFLICT")

    async def _transmit_record(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        record: OutboxRecord,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        if record.state in {"delivered", "rejected"}:
            return {
                "ok": record.state == "delivered",
                "state": record.state,
                "message_id": record.message_id,
                "conversation_id": record.conversation_id,
                "replayed": replayed,
                "code": record.code,
            }
        if record.expires_at <= self.clock() or record.state == "expired":
            self.outbox.set_state(
                record.message_id,
                "expired",
                code="MESSAGE_EXPIRED",
            )
            return {
                "ok": False,
                "state": "expired",
                "message_id": record.message_id,
                "conversation_id": record.conversation_id,
                "replayed": replayed,
                "code": "MESSAGE_EXPIRED",
            }
        negotiated_limit = int(
            ((self._welcome or {}).get("limits") or {}).get(
                "max_message_bytes",
                MAX_FRAME_BYTES,
            )
        )
        if len(canonical_json(record.frame)) > negotiated_limit:
            self.outbox.set_state(
                record.message_id,
                "rejected",
                code="PAYLOAD_TOO_LARGE",
            )
            return {
                "ok": False,
                "state": "rejected",
                "message_id": record.message_id,
                "conversation_id": record.conversation_id,
                "replayed": replayed,
                "code": "PAYLOAD_TOO_LARGE",
            }
        receipt_waiter = self._new_receipt_waiter(record.message_id)
        try:
            self.outbox.set_state(record.message_id, "sending")
            await self._send_frame(ws, record.frame)
            receipt = await asyncio.wait_for(receipt_waiter, timeout=10)
        except asyncio.TimeoutError:
            self.outbox.set_state(
                record.message_id,
                "delivery_unknown",
                code="RECEIPT_TIMEOUT",
            )
            return {
                "ok": False,
                "state": "delivery_unknown",
                "message_id": record.message_id,
                "conversation_id": record.conversation_id,
                "replayed": replayed,
                "retryable": True,
            }
        except ExchangeTransportError as exc:
            self.outbox.set_state(
                record.message_id,
                "delivery_unknown" if exc.retryable else "rejected",
                code=exc.code,
            )
            raise
        except Exception:
            self.outbox.set_state(
                record.message_id,
                "delivery_unknown",
                code="CONNECTION_FAILED",
            )
            raise
        finally:
            if self._receipt_waiters.get(record.message_id) is receipt_waiter:
                self._receipt_waiters.pop(record.message_id, None)
        state = str(receipt.get("status") or "unknown")
        return {
            "ok": state in {"accepted", "delivered"},
            "state": state,
            "message_id": record.message_id,
            "conversation_id": record.conversation_id,
            "replayed": replayed,
            "code": receipt.get("code"),
            "retryable": state in {"delivery_unknown", "unknown"},
        }

    async def _recover_outbox(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        for record in await asyncio.to_thread(self.outbox.pending, now=self.clock()):
            if ws.closed:
                return
            if record.from_agent not in self._published_agents:
                self.outbox.set_state(
                    record.message_id,
                    "rejected",
                    code="SENDER_NOT_PUBLISHED",
                )
                continue
            try:
                await self._transmit_record(
                    ws,
                    record,
                    replayed=True,
                )
            except ExchangeTransportError as exc:
                if exc.retryable:
                    raise
                logger.warning(
                    "Exchange outbox retry rejected (%s)",
                    exc.code,
                )
        await asyncio.to_thread(self.outbox.purge, now=self.clock())

    def _fail_waiters(self, code: str) -> None:
        error = ExchangeTransportError(code, retryable=True)
        for mapping in (self._request_waiters, self._receipt_waiters):
            values = list(mapping.values())
            mapping.clear()
            for waiter in values:
                if not waiter.done():
                    waiter.set_exception(error)


__all__ = [
    "ExchangeTransport",
    "ExchangeTransportError",
]
