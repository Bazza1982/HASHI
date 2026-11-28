"""Whiteboard stage-transition source for HAL v2.

Read-only except for one explicit stop notice. This module owns every HTTP
detail so the runtime stays pure: the service hands it a cursor and gets back
event documents, and the runtime never learns how they were fetched.

Two properties matter for correctness:

- The cursor is the board's own monotonic ``seq``. A gap reported by the board
  (retention window) is surfaced instead of being silently skipped.
- A stop notice is a card comment, so the human sees it where the work is. A
  failed comment never fails the stop itself: the reason is still recorded.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import quote

import aiohttp

DEFAULT_BASE_URL = "http://127.0.0.1:4417"
DEFAULT_TIMEOUT_SECONDS = 5.0
PAGE_LIMIT = 500
# A board's history is paged to find the enable baseline. The bound exists so a
# pathological board cannot stall an enable forever; if it is hit we refuse the
# enable rather than guess a baseline and replay history.
MAX_BASELINE_PAGES = 40


class WhiteboardUnavailable(RuntimeError):
    """The board could not be read, or answered with an unusable payload."""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class WhiteboardClient:
    """Minimal client for the whiteboard workflow/event API."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = (token or "").strip() or None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        # Caller headers (e.g. an idempotency key) extend the auth headers
        # instead of replacing them; passing both would be a duplicate keyword.
        headers = {**self._headers(), **(kwargs.pop("headers", None) or {})}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(method, url, headers=headers, **kwargs) as response:
                    if response.status != 200:
                        raise WhiteboardUnavailable(
                            f"whiteboard {method} {path} answered {response.status}"
                        )
                    payload = await response.json()
        except asyncio.TimeoutError as exc:
            raise WhiteboardUnavailable(f"whiteboard {method} {path} timed out") from exc
        except aiohttp.ClientError as exc:
            raise WhiteboardUnavailable(
                f"whiteboard {method} {path} unreachable: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise WhiteboardUnavailable(f"whiteboard {method} {path} returned a non-object")
        return payload

    # ------------------------------------------------------------- read side

    async def available(self) -> bool:
        try:
            await self._request("GET", "/api/health")
            return True
        except WhiteboardUnavailable:
            return False

    async def fetch_events(self, after: int, limit: int = 200) -> dict[str, Any]:
        """One bounded page of transitions after ``after``."""
        payload = await self._request(
            "GET", f"/api/workflow/events?after={int(after)}&limit={int(limit)}"
        )
        events = payload.get("events")
        if not isinstance(events, list):
            raise WhiteboardUnavailable("whiteboard event page has no events array")
        next_cursor = _int(payload.get("next_cursor"))
        if next_cursor is None:
            raise WhiteboardUnavailable("whiteboard event page has no integer next_cursor")
        return {
            "events": [event for event in events if isinstance(event, dict)],
            "next_cursor": next_cursor,
            "has_more": bool(payload.get("has_more")),
            "gap": payload.get("gap") if isinstance(payload.get("gap"), dict) else None,
        }

    async def head_seq(self) -> int:
        """Current highest committed transition seq.

        Establishes the enable baseline so enabling never replays history. Paged
        from zero on purpose: the board exposes no "latest" field, and inventing
        one here would mean changing a deployed service.
        """
        cursor = 0
        highest = 0
        for _ in range(MAX_BASELINE_PAGES):
            page = await self.fetch_events(cursor, PAGE_LIMIT)
            events = page["events"]
            for event in events:
                seq = _int(event.get("seq"))
                if seq is not None and seq > highest:
                    highest = seq
            next_cursor = page["next_cursor"]
            if next_cursor > highest:
                highest = next_cursor
            if not page["has_more"]:
                return highest
            if next_cursor <= cursor:
                raise WhiteboardUnavailable(
                    "whiteboard event page did not advance while reading the baseline"
                )
            cursor = next_cursor
        raise WhiteboardUnavailable(
            "whiteboard event history exceeds the baseline page bound; "
            "refusing to guess an enable baseline"
        )

    async def card(self, card_id: str) -> dict[str, Any]:
        """One card's CURRENT document.

        This is the authority for "may this chain continue": the board fuses a
        card, and its owner edits gates, at any moment. A cached projection
        could therefore let automation walk through a stop that already exists.
        """
        payload = await self._request("GET", f"/api/cards/{quote(card_id, safe='')}")
        card = payload.get("card")
        if isinstance(card, dict):
            return card
        if isinstance(payload.get("id"), str):
            return payload
        raise WhiteboardUnavailable("whiteboard card payload has no card object")

    async def cards(self) -> dict[str, dict[str, Any]]:
        """Every live card keyed by id (used to resync after a cursor gap)."""
        payload = await self._request("GET", "/api/board")
        cards = payload.get("cards")
        if not isinstance(cards, list):
            raise WhiteboardUnavailable("whiteboard board payload has no cards array")
        return {
            str(card["id"]): card
            for card in cards
            if isinstance(card, dict) and isinstance(card.get("id"), str)
        }

    async def stages(self, project: str = "") -> dict[str, dict[str, Any]]:
        """Resolved stage table for one project, read live on every call.

        Never cached: the gate and round-budget decisions below are taken from
        this table, and the board lets its owner edit the definition at any
        moment. A stale copy would silently authorise a move through a gate that
        exists by now.
        """
        query = f"?project={quote(str(project or ''), safe='')}" if project else ""
        payload = await self._request("GET", f"/api/workflow{query}")
        workflow = payload.get("workflow")
        stages = workflow.get("stages") if isinstance(workflow, dict) else None
        if not isinstance(stages, list):
            raise WhiteboardUnavailable("whiteboard workflow has no stages array")
        table: dict[str, dict[str, Any]] = {}
        for stage in stages:
            if isinstance(stage, dict) and isinstance(stage.get("key"), str):
                table[stage["key"]] = stage
        return table

    # ------------------------------------------------------------ write side

    async def post_comment(self, card_id: str, body: str, *, actor: str = "HAL") -> tuple[bool, str]:
        """Post the stop reason where the humans already look. Never raises."""
        try:
            # The board accepts exactly {"body": ...}; the comment's author is
            # the authenticated identity, never a client-supplied field. The
            # mutation endpoint also requires an idempotency key, so a retried
            # stop notice cannot double-post.
            await self._request(
                "POST",
                f"/api/cards/{quote(card_id, safe='')}/comments",
                json={"body": body},
                headers={**self._headers(), "Idempotency-Key": f"hal-notice-{uuid.uuid4()}"},
            )
        except WhiteboardUnavailable as exc:
            return False, str(exc)
        return True, "comment posted"
# HASHI-Fingerprint: hfp_ba023a6dbc7c49f4afac248e3e1341f7
