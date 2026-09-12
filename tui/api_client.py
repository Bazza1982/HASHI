"""Async client for direct or Hashi-Remote-proxied Workbench access."""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import aiohttp

from orchestrator.frontend_delivery import tui_run_delivery_policy
from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_LOCALHOST_URL

logger = logging.getLogger(__name__)

TUI_TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "stopped", "superseded", "interrupted"}
)


def run_failure_text(payload: dict) -> str:
    """Return a user-visible terminal failure, or an empty string."""

    run = payload.get("run")
    if not isinstance(run, dict):
        return ""
    state = str(run.get("state") or "").strip().casefold()
    if state not in TUI_TERMINAL_RUN_STATES or state == "completed":
        return ""
    error = str(run.get("error_text") or "").strip()
    code = str(run.get("error_code") or "").strip()
    detail = error or code or state or "Request failed"
    return f"{code}: {detail}" if code and error and code not in error else detail


class TuiApiClient:
    """Talk to one Workbench while keeping transcript offsets instance-local.

    The launch instance uses local Workbench URLs directly. A switched peer
    uses only the launch instance's loopback Hashi Remote proxy; the TUI never
    connects to a peer Workbench port itself.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_WORKBENCH_LOCALHOST_URL,
        *,
        fallback_base_urls: list[str] | tuple[str, ...] | None = None,
        expected_instance_id: str | None = None,
        remote_url: str | None = None,
        target_instance: str | None = None,
    ):
        bases = [str(base_url or DEFAULT_WORKBENCH_LOCALHOST_URL).rstrip("/")]
        for candidate in fallback_base_urls or ():
            normalized = str(candidate or "").rstrip("/")
            if normalized and normalized not in bases:
                bases.append(normalized)
        self._bases = bases
        self.base = bases[0]
        self.expected_instance_id = str(expected_instance_id or target_instance or "").strip().upper()
        self.remote_url = str(remote_url or "").rstrip("/")
        self.target_instance = str(target_instance or "").strip().upper()
        self._offsets: dict[str, int | None] = {}

    @property
    def proxied(self) -> bool:
        return bool(self.remote_url and self.target_instance)

    async def _read_json_response(self, response: aiohttp.ClientResponse) -> dict:
        """Parse JSON when possible and preserve plain-text server errors."""
        body = await response.text()
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else {"ok": False, "error": "Invalid JSON object"}
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": body.strip() or f"HTTP {response.status}",
                "status": response.status,
            }

    async def _direct_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: float = 10,
    ) -> dict:
        ordered_bases = [self.base, *(base for base in self._bases if base != self.base)]
        first_error: Exception | None = None
        timed_out = False
        first_error_base = ordered_bases[0]
        for base in ordered_bases:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.request(method, f"{base}{path}", json=json_body) as response:
                        data = await self._read_json_response(response)
                if response.status >= 400:
                    data.setdefault("ok", False)
                    data.setdefault("status", response.status)
                    return data
                if base != self.base:
                    logger.info("TUI selected local Workbench route: %s", base)
                    self.base = base
                return data
            except (aiohttp.ClientError, TimeoutError) as exc:
                timed_out = timed_out or isinstance(exc, TimeoutError)
                if first_error is None:
                    first_error = exc
                    first_error_base = base
                logger.debug("TUI Workbench route unavailable: url=%s%s error=%s", base, path, exc)
        logger.warning(
            "TUI Workbench request failed: url=%s path=%s error=%s",
            first_error_base,
            path,
            first_error,
        )
        return {
            "ok": False,
            "code": "request_timeout" if timed_out else "connection_unavailable",
            "error": f"Cannot connect to local HASHI at {first_error_base}: "
            f"{first_error or 'Workbench unavailable'}",
        }

    async def _proxy_request(
        self,
        operation: str,
        *,
        agent: str | None = None,
        text: str | None = None,
        client_id: str | None = None,
        ui_locale: str | None = None,
        delivery_policy: dict | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        voice_profile: str | None = None,
        attachment: dict | None = None,
        workzone_ref: str | None = None,
        offset: int = 0,
        limit: int = 20,
        timeout: float = 25,
    ) -> dict:
        payload = {
            "target_instance": self.target_instance,
            "operation": operation,
            "offset": int(offset),
            "limit": int(limit),
        }
        if agent is not None:
            payload["agent"] = agent
        if text is not None:
            payload["text"] = text
        if client_id is not None:
            payload["client_id"] = client_id
        if ui_locale is not None:
            payload["ui_locale"] = ui_locale
        if delivery_policy is not None:
            payload["delivery_policy"] = dict(delivery_policy)
        if session_id is not None:
            payload["session_id"] = session_id
        if run_id is not None:
            payload["run_id"] = run_id
        if request_id is not None:
            payload["request_id"] = request_id
        if voice_profile is not None:
            payload["voice_profile"] = voice_profile
        if attachment is not None:
            payload["attachment"] = dict(attachment)
        if workzone_ref is not None:
            payload["workzone_ref"] = str(workzone_ref)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(f"{self.remote_url}/tui/proxy", json=payload) as response:
                    envelope = await self._read_json_response(response)
            if response.status >= 400 or not envelope.get("ok"):
                result = envelope.get("result")
                if isinstance(result, dict):
                    result.setdefault("ok", False)
                    result.setdefault("status", response.status)
                    return result
                envelope.setdefault("ok", False)
                envelope.setdefault("status", response.status)
                return envelope
            actual = str(envelope.get("target_instance") or "").strip().upper()
            if actual != self.target_instance:
                logger.error(
                    "TUI proxy returned wrong instance: expected=%s actual=%s",
                    self.target_instance,
                    actual or "missing",
                )
                return {"ok": False, "error": "target_identity_mismatch"}
            result = envelope.get("result")
            if not isinstance(result, dict):
                return {"ok": False, "error": "invalid_proxy_response"}
            return result
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning(
                "TUI Remote proxy request failed: target=%s operation=%s error=%s",
                self.target_instance,
                operation,
                exc,
            )
            return {
                "ok": False,
                "code": (
                    "request_timeout" if isinstance(exc, TimeoutError)
                    else "connection_unavailable"
                ),
                "error": str(exc),
            }

    async def health_info(self) -> dict:
        if self.proxied:
            data = await self._proxy_request("health", timeout=5)
        else:
            data = await self._direct_request("GET", "/api/health", timeout=2)
        actual = str(data.get("instance_id") or "").strip().upper()
        if data.get("ok") and self.expected_instance_id and actual != self.expected_instance_id:
            logger.error(
                "TUI Workbench identity mismatch: expected=%s actual=%s",
                self.expected_instance_id,
                actual or "missing",
            )
            return {
                "ok": False,
                "error": "instance_identity_mismatch",
                "expected_instance_id": self.expected_instance_id,
                "actual_instance_id": actual,
            }
        return data

    async def health(self) -> bool:
        return bool((await self.health_info()).get("ok"))

    async def agents_info(self) -> dict:
        """Return the full agent-directory response for transactional checks."""
        return (
            await self._proxy_request("agents")
            if self.proxied
            else await self._direct_request("GET", "/api/agents")
        )

    async def capabilities_info(self) -> dict:
        """Read optional Workbench capabilities without inferring from chat."""
        return (
            await self._proxy_request("capabilities", timeout=5)
            if self.proxied
            else await self._direct_request("GET", "/api/v1/capabilities", timeout=5)
        )

    async def log_tail(self, *, offset: int = 0, limit: int = 120) -> dict:
        """Read a bounded peer-owned host log tail through authenticated Remote."""
        if not self.proxied:
            return {"ok": False, "code": "local_log_owned_by_client", "error": "local log is read by the TUI client"}
        return await self._proxy_request(
            "log_tail",
            offset=max(0, int(offset)),
            limit=max(1, min(int(limit), 200)),
            timeout=5,
        )

    async def list_agents(self) -> list[dict]:
        data = await self.agents_info()
        agents = data.get("agents", [])
        return agents if isinstance(agents, list) else []

    async def agent_overview(self, agent: str) -> dict:
        """Read the canonical Workbench overview for one Agent."""
        if self.proxied:
            return await self._proxy_request("agent_overview", agent=agent, timeout=8)
        encoded_agent = quote(str(agent), safe="")
        return await self._direct_request(
            "GET", f"/api/agents/{encoded_agent}/overview", timeout=8
        )

    async def scheduler_jobs(self, agent: str) -> dict:
        """Read this Agent's authoritative HASHI Scheduler jobs."""
        if self.proxied:
            return await self._proxy_request("scheduler_jobs", agent=agent, timeout=8)
        encoded_agent = quote(str(agent), safe="")
        return await self._direct_request(
            "GET", f"/api/agents/{encoded_agent}/scheduler/jobs", timeout=8
        )

    async def background_jobs(self, agent: str, *, limit: int = 20) -> dict:
        """Read recent background jobs scoped to one Agent."""
        safe_limit = max(1, min(int(limit), 200))
        if self.proxied:
            return await self._proxy_request(
                "background_jobs", agent=agent, limit=safe_limit, timeout=8
            )
        encoded_agent = quote(str(agent), safe="")
        return await self._direct_request(
            "GET",
            f"/api/background-jobs?agent={encoded_agent}&limit={safe_limit}",
            timeout=8,
        )

    async def send_chat(
        self,
        agent: str,
        text: str,
        *,
        client_id: str | None = None,
        telegram_mirror: bool = True,
        ui_locale: str = "en",
    ) -> dict:
        """Send a text message without bypassing the selected transport."""
        policy = (
            tui_run_delivery_policy(
                telegram_mirror=telegram_mirror,
                client_id=client_id,
            )
            if client_id
            else None
        )
        if self.proxied:
            return await self._proxy_request(
                "chat",
                agent=agent,
                text=text,
                client_id=client_id,
                ui_locale=ui_locale,
                delivery_policy=policy,
            )
        payload = {"agent": agent, "text": text}
        if policy is not None:
            payload.update(
                {
                    "source": "tui",
                    "client_id": client_id,
                    "ui_locale": ui_locale,
                    "delivery_policy": policy,
                }
            )
        return await self._direct_request(
            "POST",
            "/api/chat",
            json_body=payload,
            timeout=25,
        )

    async def send_chat_attachment(
        self,
        agent: str,
        text: str,
        *,
        attachment: dict | None = None,
        workzone_ref: str | None = None,
        client_id: str | None = None,
        telegram_mirror: bool = True,
        ui_locale: str = "en",
    ) -> dict:
        """Submit one caption and one immutable attachment as one request."""
        if bool(attachment) == bool(workzone_ref):
            return {"ok": False, "code": "invalid_attachment", "error": "exactly one attachment source is required"}
        policy = tui_run_delivery_policy(
            telegram_mirror=telegram_mirror,
            client_id=str(client_id or "tui-client"),
        )
        if self.proxied:
            return await self._proxy_request(
                "chat_attachment",
                agent=agent,
                text=text,
                client_id=client_id,
                ui_locale=ui_locale,
                delivery_policy=policy,
                attachment=attachment,
                workzone_ref=workzone_ref,
                timeout=35,
            )
        payload = {
            "agent": agent,
            "text": text,
            "source": "tui",
            "client_id": client_id,
            "ui_locale": ui_locale,
            "delivery_policy": policy,
        }
        if attachment is not None:
            payload["attachment"] = dict(attachment)
        else:
            payload["workzone_ref"] = str(workzone_ref)
        return await self._direct_request(
            "POST", "/api/chat", json_body=payload, timeout=35
        )

    async def voice_state(self, agent: str) -> dict:
        if self.proxied:
            return await self._proxy_request(
                "voice_state", agent=agent, timeout=10
            )
        return await self._direct_request(
            "POST", "/api/tui/voice", json_body={"agent": agent}, timeout=10
        )

    async def set_voice_profile(self, agent: str, profile: str) -> dict:
        if self.proxied:
            return await self._proxy_request(
                "voice_profile",
                agent=agent,
                voice_profile=profile,
                timeout=15,
            )
        return await self._direct_request(
            "POST",
            "/api/tui/voice",
            json_body={"agent": agent, "profile": profile},
            timeout=15,
        )

    async def synthesize_speech(
        self, agent: str, text: str, *, request_id: str
    ) -> dict:
        """Generate audio on the selected instance without Connector delivery."""

        if self.proxied:
            return await self._proxy_request(
                "speech",
                agent=agent,
                text=text,
                request_id=request_id,
                timeout=130,
            )
        return await self._direct_request(
            "POST",
            "/api/tui/speech",
            json_body={"agent": agent, "text": text, "request_id": request_id},
            timeout=130,
        )

    async def run_info(self, session_id: str, run_id: str) -> dict:
        """Read the durable status of one directly submitted Session Run."""

        if self.proxied:
            return await self._proxy_request(
                "run_info",
                session_id=session_id,
                run_id=run_id,
                timeout=5,
            )
        encoded_session = quote(str(session_id), safe="")
        encoded_run = quote(str(run_id), safe="")
        return await self._direct_request(
            "GET",
            f"/api/v1/sessions/{encoded_session}/runs/{encoded_run}",
            timeout=5,
        )

    async def poll_transcript(self, agent: str) -> list[dict]:
        offset = self._offsets.get(agent, 0)
        if offset is None:
            # Agent selection starts an asynchronous recent-history load.  Do
            # not race that request from byte zero or the initial assistant
            # messages will be rendered twice.
            return []
        if self.proxied:
            data = await self._proxy_request("transcript_poll", agent=agent, offset=offset)
        else:
            encoded_agent = quote(agent, safe="")
            data = await self._direct_request(
                "GET",
                f"/api/transcript/{encoded_agent}/poll?offset={offset}",
            )
        if not data.get("ok", True) and data.get("error"):
            logger.warning("TUI transcript poll failed: agent=%s error=%s", agent, data.get("error"))
            return []
        new_offset = data.get("offset", offset)
        if isinstance(new_offset, int) and new_offset > offset:
            self._offsets[agent] = new_offset
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def get_recent_transcript(self, agent: str, limit: int = 20) -> list[dict]:
        if self.proxied:
            data = await self._proxy_request("transcript_recent", agent=agent, limit=limit)
        else:
            encoded_agent = quote(agent, safe="")
            data = await self._direct_request(
                "GET",
                f"/api/transcript/{encoded_agent}?limit={int(limit)}",
            )
        if not data.get("ok", True) and data.get("error"):
            logger.warning("TUI transcript load failed: agent=%s error=%s", agent, data.get("error"))
            return []
        offset = data.get("offset", 0)
        self._offsets[agent] = offset if isinstance(offset, int) else 0
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    def reset_offset(self, agent: str):
        self._offsets[agent] = None
