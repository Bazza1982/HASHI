from __future__ import annotations

import json
import os
import string
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "zh-CN")
PREFERENCES_VERSION = 1

_LOCALE_ALIASES = {
    "en": "en",
    "en-au": "en",
    "en-gb": "en",
    "en-us": "en",
    "english": "en",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-sg": "zh-CN",
    "chinese": "zh-CN",
    "chinese-simplified": "zh-CN",
    "简体中文": "zh-CN",
    "中文": "zh-CN",
}

_ACTIVE_LOCALE: ContextVar[str] = ContextVar(
    "hashi_ui_locale",
    default=DEFAULT_LOCALE,
)
_PREFERENCES_LOCK = globals().get("_PREFERENCES_LOCK") or threading.RLock()


@dataclass(frozen=True)
class LanguageCatalog:
    locale: str
    display_name: str
    native_name: str
    telegram_language_code: str
    strings: Mapping[str, str]
    commands: Mapping[str, str]
    titles: Mapping[str, str]


def normalize_locale(value: Any, *, fallback: str = DEFAULT_LOCALE) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    normalized = _LOCALE_ALIASES.get(raw.casefold())
    if normalized:
        return normalized
    if raw in SUPPORTED_LOCALES:
        return raw
    return fallback


def _catalog_root() -> Path:
    return Path(__file__).resolve().parent.parent / "locales" / "runtime"


@lru_cache(maxsize=len(SUPPORTED_LOCALES))
def load_catalog(locale: str) -> LanguageCatalog:
    normalized = normalize_locale(locale)
    path = _catalog_root() / f"{normalized}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid UI language catalog: {path.name}")
    return LanguageCatalog(
        locale=normalized,
        display_name=str(payload.get("display_name") or normalized),
        native_name=str(payload.get("native_name") or payload.get("display_name") or normalized),
        telegram_language_code=str(payload.get("telegram_language_code") or ""),
        strings=dict(payload.get("strings") or {}),
        commands=dict(payload.get("commands") or {}),
        titles=dict(payload.get("titles") or {}),
    )


def current_locale() -> str:
    return normalize_locale(_ACTIVE_LOCALE.get())


def _format(template: str, values: Mapping[str, Any]) -> str:
    if not values:
        return template
    return template.format(**values)


def tr(key: str, *, locale: str | None = None, **values: Any) -> str:
    selected = normalize_locale(locale or current_locale())
    english = load_catalog(DEFAULT_LOCALE)
    catalog = load_catalog(selected)
    template = catalog.strings.get(key) or english.strings.get(key) or key
    try:
        return _format(str(template), values)
    except (KeyError, IndexError, ValueError):
        fallback = english.strings.get(key) or key
        try:
            return _format(str(fallback), values)
        except (KeyError, IndexError, ValueError):
            return str(fallback)


def command_description(
    command_name: str,
    fallback: str,
    *,
    locale: str | None = None,
) -> str:
    selected = normalize_locale(locale or current_locale())
    catalog = load_catalog(selected)
    if command_name in catalog.commands:
        return str(catalog.commands[command_name])
    english = load_catalog(DEFAULT_LOCALE)
    return str(english.commands.get(command_name) or fallback)


def title(text: str, *, locale: str | None = None) -> str:
    selected = normalize_locale(locale or current_locale())
    catalog = load_catalog(selected)
    if text in catalog.titles:
        return str(catalog.titles[text])
    english = load_catalog(DEFAULT_LOCALE)
    return str(english.titles.get(text) or text)


def language_options() -> tuple[LanguageCatalog, ...]:
    return tuple(load_catalog(locale) for locale in SUPPORTED_LOCALES)


def _bridge_home(runtime: Any) -> Path:
    global_config = getattr(runtime, "global_config", None)
    configured = getattr(global_config, "bridge_home", None)
    if configured:
        return Path(configured)
    project_root = getattr(global_config, "project_root", None)
    if project_root:
        return Path(project_root)
    workspace = getattr(runtime, "workspace_dir", None)
    if workspace:
        path = Path(workspace)
        return path.parent.parent if len(path.parents) >= 2 else path.parent
    return Path(".")


def preferences_path(runtime: Any) -> Path:
    return _bridge_home(runtime) / "state" / "ui_language.json"


def _read_preferences(runtime: Any) -> dict[str, Any]:
    path = preferences_path(runtime)
    with _PREFERENCES_LOCK:
        if not path.exists():
            return {"version": PREFERENCES_VERSION, "users": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": PREFERENCES_VERSION, "users": {}}
    if not isinstance(payload, dict):
        return {"version": PREFERENCES_VERSION, "users": {}}
    users = payload.get("users")
    if not isinstance(users, dict):
        users = {}
    return {
        "version": PREFERENCES_VERSION,
        "users": {
            str(key): normalize_locale(value)
            for key, value in users.items()
            if str(key).strip()
        },
    }


def _write_preferences(runtime: Any, payload: Mapping[str, Any]) -> Path:
    path = preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "version": PREFERENCES_VERSION,
        "users": dict(payload.get("users") or {}),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    with _PREFERENCES_LOCK:
        try:
            temporary.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def actor_id_from_update(update: Any | None, *, fallback: Any = None) -> str:
    if update is not None:
        owner_id = getattr(update, "_hashi_session_owner_id", None)
        if owner_id is not None and str(owner_id).strip():
            return str(owner_id)
        effective_user = getattr(update, "effective_user", None)
        user_id = getattr(effective_user, "id", None)
        if user_id is None:
            query = getattr(update, "callback_query", None)
            user_id = getattr(getattr(query, "from_user", None), "id", None)
        if user_id is not None:
            return str(user_id)
    return str(fallback) if fallback is not None and str(fallback).strip() else "default"


def chat_id_from_update(update: Any | None, *, fallback: Any = None) -> int | str | None:
    if update is not None:
        effective_chat = getattr(update, "effective_chat", None)
        chat_id = getattr(effective_chat, "id", None)
        if chat_id is None:
            query = getattr(update, "callback_query", None)
            chat_id = getattr(getattr(getattr(query, "message", None), "chat", None), "id", None)
        if chat_id is not None:
            return chat_id
    return fallback


def configured_default_locale(runtime: Any) -> str:
    global_config = getattr(runtime, "global_config", None)
    return normalize_locale(getattr(global_config, "ui_language", DEFAULT_LOCALE))


def preferred_locale(
    runtime: Any,
    update: Any | None = None,
    *,
    actor_id: Any = None,
) -> str:
    fallback_actor = actor_id
    if fallback_actor is None:
        fallback_actor = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
    key = actor_id_from_update(update, fallback=fallback_actor)
    users = _read_preferences(runtime).get("users") or {}
    return normalize_locale(users.get(key), fallback=configured_default_locale(runtime))


def set_preferred_locale(
    runtime: Any,
    locale: str,
    update: Any | None = None,
    *,
    actor_id: Any = None,
) -> Path:
    selected = normalize_locale(locale, fallback="")
    if selected not in SUPPORTED_LOCALES:
        raise ValueError(f"Unsupported UI language: {locale}")
    fallback_actor = actor_id
    if fallback_actor is None:
        fallback_actor = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
    key = actor_id_from_update(update, fallback=fallback_actor)
    with _PREFERENCES_LOCK:
        payload = _read_preferences(runtime)
        users = dict(payload.get("users") or {})
        users[key] = selected
        payload["users"] = users
        return _write_preferences(runtime, payload)


def reset_preferred_locale(
    runtime: Any,
    update: Any | None = None,
    *,
    actor_id: Any = None,
) -> Path:
    fallback_actor = actor_id
    if fallback_actor is None:
        fallback_actor = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
    key = actor_id_from_update(update, fallback=fallback_actor)
    with _PREFERENCES_LOCK:
        payload = _read_preferences(runtime)
        users = dict(payload.get("users") or {})
        users.pop(key, None)
        payload["users"] = users
        return _write_preferences(runtime, payload)


def saved_user_locales(runtime: Any) -> dict[str, str]:
    return dict(_read_preferences(runtime).get("users") or {})


@contextmanager
def language_scope(
    runtime: Any,
    update: Any | None = None,
    *,
    actor_id: Any = None,
    locale: str | None = None,
) -> Iterator[str]:
    selected = normalize_locale(locale or preferred_locale(runtime, update, actor_id=actor_id))
    token = _ACTIVE_LOCALE.set(selected)
    try:
        yield selected
    finally:
        _ACTIVE_LOCALE.reset(token)


def _placeholder_names(template: str) -> frozenset[str]:
    names: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(template):
        if field_name:
            names.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return frozenset(names)


def validate_catalogs() -> list[str]:
    errors: list[str] = []
    english = load_catalog(DEFAULT_LOCALE)
    for locale in SUPPORTED_LOCALES:
        catalog = load_catalog(locale)
        missing_strings = sorted(set(english.strings) - set(catalog.strings))
        missing_commands = sorted(set(english.commands) - set(catalog.commands))
        missing_titles = sorted(set(english.titles) - set(catalog.titles))
        for kind, missing in (
            ("strings", missing_strings),
            ("commands", missing_commands),
            ("titles", missing_titles),
        ):
            if missing:
                errors.append(f"{locale}: missing {kind}: {', '.join(missing)}")
        for key, template in english.strings.items():
            translated = catalog.strings.get(key)
            if translated is None:
                continue
            if _placeholder_names(str(template)) != _placeholder_names(str(translated)):
                errors.append(f"{locale}: placeholder mismatch for {key}")
    return errors
