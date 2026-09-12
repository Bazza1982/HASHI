"""Bounded, disposable command-menu projections (Frontend Connector / Functions).

No Telegram network client, model execution, or configuration writer lives here.
The bridge supplies registered callbacks and an authenticated, canonical binding.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

VERSION = 1
MAX_TEXT = 65536
MAX_BUTTONS = 100
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
_UNSET = object()


class InteractionError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status

    def result(self):
        return {"ok": False, "command_ui_version": VERSION, "error_code": self.code,
                "error": self.code, "http_status": self.status}


def validate_operation(payload: Any) -> None:
    """Small public wire validation, shared by API ingress and Worker dispatch."""
    if not isinstance(payload, Mapping) or type(payload.get("version")) is not int or payload["version"] != VERSION:
        raise InteractionError("command_menu_version_unsupported", 400)
    op = payload.get("op")
    if not isinstance(op, str) or op not in {"catalogue", "open", "act", "close"}:
        raise InteractionError("command_menu_operation_invalid", 400)
    for field in ("client_id", "request_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise InteractionError("command_menu_request_invalid", 400)
    if "ui_locale" in payload and payload["ui_locale"] is not None and (
        not isinstance(payload["ui_locale"], str) or len(payload["ui_locale"]) > 24
    ):
        raise InteractionError("command_menu_request_invalid", 400)
    if op in {"act", "close"}:
        if not isinstance(payload.get("menu_id"), str) or not ID_PATTERN.fullmatch(payload["menu_id"]):
            raise InteractionError("command_menu_request_invalid", 400)
        if type(payload.get("revision")) is not int or payload["revision"] < 1:
            raise InteractionError("command_menu_request_invalid", 400)
    if op == "act" and (not isinstance(payload.get("button_id"), str) or not ID_PATTERN.fullmatch(payload["button_id"])):
        raise InteractionError("command_menu_request_invalid", 400)
    if op == "open" and (not isinstance(payload.get("command"), str)
                         or len(payload["command"]) > 16384 or not payload["command"].lstrip().startswith("/")):
        raise InteractionError("command_menu_command_invalid", 400)


@dataclass(frozen=True)
class Binding:
    """Identity must be supplied by an authenticated ingress, never a button."""
    instance: str
    agent: str
    actor: str
    session: str
    context_generation: int
    client: str
    connection: str


@dataclass
class Menu:
    id: str
    message_id: int
    binding: Binding
    command: str
    expires: float
    expires_at: float
    revision: int = 1
    text: str = ""
    parse_mode: str = "HTML"
    rows: list = field(default_factory=list)
    actions: dict = field(default_factory=dict)
    closed: bool = False


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password:
            return None
        return value
    except ValueError:
        return None


def markup_rows(value: Any) -> list:
    """Accept public to_dict() or a JSON mapping; never parse repr()/eval()."""
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping) or "inline_keyboard" not in value:
        raise InteractionError("command_menu_markup_unsupported", 422)
    rows = value["inline_keyboard"]
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_BUTTONS:
        raise InteractionError("command_menu_markup_invalid", 422)
    total = 0
    output = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            raise InteractionError("command_menu_markup_invalid", 422)
        total += len(row)
        if total > MAX_BUTTONS:
            raise InteractionError("command_menu_too_large", 422)
        output.append(list(row))
    return output


class MenuStore:
    def __init__(self, *, ttl: float = 900, max_menus: int = 128,
                 max_requests: int = 2048, clock=time.monotonic, wall_clock=time.time):
        self.ttl, self.max_menus, self.max_requests = ttl, max_menus, max_requests
        self.clock, self.wall_clock = clock, wall_clock
        self.menus: OrderedDict[str, Menu] = OrderedDict()
        self.requests: OrderedDict[tuple, tuple] = OrderedDict()
        self.lock = asyncio.Lock()
        self._sequence = 0

    def prune(self):
        now = self.clock()
        for key, menu in list(self.menus.items()):
            if menu.expires <= now:
                del self.menus[key]
        for key, (_, _, expires) in list(self.requests.items()):
            if expires <= now:
                del self.requests[key]

    def create(self, binding: Binding, command: str) -> Menu:
        self.prune()
        if len(self.menus) >= self.max_menus:
            # Only disposable closed cards may be evicted early.
            victim = next((k for k, m in self.menus.items() if m.closed), None)
            if victim is None:
                raise InteractionError("command_menu_capacity", 429)
            del self.menus[victim]
        self._sequence += 1
        menu = Menu(secrets.token_urlsafe(24), -self._sequence, binding, command,
                    self.clock() + self.ttl, self.wall_clock() + self.ttl)
        self.menus[menu.id] = menu
        return menu

    def require(self, menu_id: str, binding: Binding, revision: Any = _UNSET) -> Menu:
        self.prune()
        if not isinstance(menu_id, str) or not ID_PATTERN.fullmatch(menu_id):
            raise InteractionError("command_menu_expired")
        menu = self.menus.get(menu_id)
        if menu is None or menu.binding != binding:
            raise InteractionError("command_menu_expired")
        if revision is not _UNSET and (type(revision) is not int or menu.revision != revision):
            raise InteractionError("command_menu_stale")
        return menu

    def invalidate(self, binding: Binding) -> list[Menu]:
        changed = []
        for menu in self.menus.values():
            if menu.binding == binding and not menu.closed:
                menu.revision += 1
                menu.actions.clear()
                menu.closed = True
                changed.append(menu)
        return changed

    async def once(self, binding: Binding, request_id: str, payload: Mapping,
                   perform: Callable[[], Awaitable[dict]]) -> dict:
        if not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id):
            raise InteractionError("command_menu_request_invalid", 400)
        try:
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InteractionError("command_menu_request_invalid", 400) from exc
        if len(encoded) > MAX_TEXT:
            raise InteractionError("command_menu_request_too_large", 413)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        key = (binding, request_id)
        # All local menu writes for an Agent are serialized. The normal command
        # executor retains its existing independent local-admin lock.
        async with self.lock:
            self.prune()
            if key in self.requests:
                prior_digest, result, _ = self.requests[key]
                if prior_digest != digest:
                    raise InteractionError("command_menu_request_conflict")
                return copy.deepcopy(result)
            if len(self.requests) >= self.max_requests:
                raise InteractionError("command_menu_request_capacity", 429)
            uncertain = InteractionError("command_menu_outcome_unknown").result()
            # Reserve BEFORE awaiting a handler, even when it later raises or is
            # cancelled. A duplicate must never execute an uncertain operation.
            self.requests[key] = (digest, uncertain, self.clock() + self.ttl)
            try:
                result = await perform()
                json.dumps(result, allow_nan=False)  # Validate the public boundary.
            except InteractionError as exc:
                result = exc.result()
            except BaseException:
                # No exception strings/credentials or private callback payloads in UI.
                raise
            self.requests[key] = (digest, copy.deepcopy(result), self.clock() + self.ttl)
            return result

    def render(self, menu: Menu) -> dict:
        return {"version": VERSION, "menu_id": menu.id, "revision": menu.revision,
                "expires_at": int(menu.expires_at * 1000), "closed": menu.closed,
                "rows": copy.deepcopy(menu.rows) if not menu.closed else []}

    def message(self, menu: Menu, *, deleted: bool = False) -> dict:
        return {"message_ref": "command-ui:" + menu.id, "channel": "command-ui",
                "op": "delete" if deleted else "upsert", "text": menu.text,
                "meta": {"parse_mode": menu.parse_mode}, "command_ui": self.render(menu)}


class Capture:
    """A narrow Telegram-compatible reply/edit projection, scoped to one request."""
    def __init__(self, store: MenuStore, binding: Binding, command: str,
                 resolve_callback: Callable[[str], tuple | None]):
        self.store, self.binding, self.command = store, binding, command
        self.resolve_callback = resolve_callback
        self.active = True
        self.messages: list[dict] = []
        self.notifications: list[dict] = []
        self.chat_id: int | str = 0
        self._owned: dict[int, Menu] = {}

    def _ensure(self):
        if not self.active:
            raise InteractionError("command_menu_capture_closed")

    def _record(self, menu: Menu, *, deleted=False):
        item = self.store.message(menu, deleted=deleted)
        self.messages[:] = [m for m in self.messages if m["message_ref"] != item["message_ref"]]
        self.messages.append(item)

    def attach(self, menu: Menu):
        self._owned[menu.message_id] = menu
        return CapturedMessage(self, menu)

    def _set(self, menu: Menu, text: str, parse_mode: str | None, markup: Any):
        self._ensure()
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise InteractionError("command_menu_text_invalid", 422)
        rows = []
        actions = {}
        for row in markup_rows(markup):
            buttons = []
            for button in row:
                if hasattr(button, "to_dict"):
                    button = button.to_dict()
                if not isinstance(button, Mapping):
                    raise InteractionError("command_menu_button_invalid", 422)
                label = str(button.get("text") or "")[:256]
                if not label:
                    raise InteractionError("command_menu_button_invalid", 422)
                item = {"text": label, "disabled": True, "reason": "unsupported"}
                callback = button.get("callback_data")
                url = safe_url(button.get("url"))
                # Telegram specialty buttons are visible but never reinterpreted.
                special = any(button.get(k) is not None for k in (
                    "web_app", "login_url", "switch_inline_query", "switch_inline_query_current_chat",
                    "switch_inline_query_chosen_chat", "callback_game", "pay", "copy_text"))
                if not special and isinstance(callback, str) and not button.get("url"):
                    if 0 < len(callback.encode("utf-8")) <= 64:
                        resolved = self.resolve_callback(callback)
                        if resolved:
                            key = secrets.token_urlsafe(18)
                            # Raw callback data stays server-side.
                            actions[key] = (callback, resolved[0])
                            item = {"text": label, "button_id": key, "disabled": False}
                        else:
                            item["reason"] = "callback_unavailable"
                elif not special and url and callback is None:
                    item = {"text": label, "url": url, "disabled": False}
                buttons.append(item)
            if buttons:
                rows.append(buttons)
        menu.text, menu.parse_mode = text, str(parse_mode or "")
        menu.rows, menu.actions = rows, actions
        menu.closed = not bool(rows)
        self._record(menu)

    async def capture_reply(self, text: str, **kwargs):
        return await self.capture_send(self.chat_id, text, **kwargs)

    async def capture_send(self, chat_id: int | str, text: str, **kwargs):
        self._ensure()
        # The local output channel may not silently redirect a cross-chat send.
        if str(chat_id) != str(self.chat_id):
            raise InteractionError("command_menu_cross_chat_unsupported", 422)
        menu = self.store.create(self.binding, self.command)
        self._owned[menu.message_id] = menu
        try:
            self._set(menu, text, kwargs.get("parse_mode"), kwargs.get("reply_markup"))
        except BaseException:
            self.store.menus.pop(menu.id, None)
            raise
        return CapturedMessage(self, menu)

    def result(self, **extra):
        return {"ok": True, "command_ui_version": VERSION, "messages": self.messages,
                "notifications": self.notifications, **extra}


class CapturedMessage:
    def __init__(self, capture: Capture, menu: Menu):
        self.capture, self.menu = capture, menu
        self.message_id = menu.message_id
        self.chat_id = capture.chat_id
        self.chat = SimpleNamespace(id=self.chat_id, type="private")
        self.from_user = SimpleNamespace(id=0, is_bot=True)

    @property
    def text(self):
        return self.menu.text

    @property
    def text_html(self):
        return self.menu.text

    async def reply_text(self, text, **kwargs):
        return await self.capture.capture_send(self.chat_id, text, **kwargs)

    async def edit_text(self, text, *, parse_mode=None, reply_markup=None, **kwargs):
        self.capture._set(self.menu, text, parse_mode, reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.capture._set(self.menu, self.menu.text, self.menu.parse_mode, reply_markup)
        return self

    async def delete(self, **kwargs):
        self.capture._ensure()
        self.menu.actions.clear()
        self.menu.closed = True
        self.capture._record(self.menu, deleted=True)
        return True


class CapturedQuery:
    def __init__(self, capture: Capture, menu: Menu, data: str, actor_id: int):
        self.capture = capture
        self.data = data
        self.id = secrets.token_urlsafe(18)
        self.from_user = SimpleNamespace(id=actor_id, is_bot=False)
        self.message = capture.attach(menu)
        self.chat_instance = capture.binding.session
        self.inline_message_id = None

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.capture._ensure()
        if text:
            self.capture.notifications.append({"text": str(text)[:4096], "alert": bool(show_alert)})
        return True

    async def edit_message_text(self, text, **kwargs):
        return await self.message.edit_text(text, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        return await self.message.edit_reply_markup(reply_markup=reply_markup, **kwargs)

    async def delete_message(self, **kwargs):
        return await self.message.delete(**kwargs)


async def perform_action(store: MenuStore, binding: Binding, payload: Mapping,
                         capture: Capture, *, actor_id: int,
                         authorize: Callable[[str], bool],
                         invoke: Callable[[tuple, CapturedQuery], Awaitable[None]]) -> dict:
    """Called inside once(); reserve the revision before invoking any callback."""
    menu = store.require(payload.get("menu_id"), binding, payload.get("revision"))
    if menu.closed:
        raise InteractionError("command_menu_closed")
    if not authorize(menu.command):
        raise InteractionError("command_menu_forbidden", 403)
    action = menu.actions.get(payload.get("button_id"))
    if action is None:
        raise InteractionError("command_menu_button_invalid", 400)
    data, fingerprint = action
    resolved = capture.resolve_callback(data)
    if not resolved or resolved[0] != fingerprint:
        raise InteractionError("command_menu_handler_changed")
    capture.command = menu.command
    menu.revision += 1
    menu.actions.clear()
    menu.closed = True
    capture._record(menu)
    query = CapturedQuery(capture, menu, data, actor_id)
    try:
        await invoke(resolved, query)
    except BaseException:
        menu.closed = True
        menu.actions.clear()
        capture._record(menu)
        raise
    return capture.result()
