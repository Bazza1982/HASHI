"""Persona-authored status delivery for detached background turns.

The status text is deliberately generated from the exact configured
``system_md`` file.  HASHI owns only the facts that the message must convey;
the Agent's model owns the language, address, self-reference, and tone.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from adapters import her_habits, her_persona
from orchestrator.flexible_backend_registry import canonical_backend_engine
from orchestrator.privacy_levels import PrivacyPolicyError, require_backend_compatibility

_MAX_STATUS_CHARS = 800
_TOOL_FREE_API_ENGINES = frozenset(
    {"deepseek-api", "hashi-api", "ollama-api", "openrouter-api", "xai-api"}
)


@dataclass(frozen=True)
class PersonaBackgroundStatus:
    persona_sha256: str
    text: str


def is_enabled_for(runtime: Any, item: Any) -> bool:
    """Return whether this request can detach into Telegram background mode."""

    extra = getattr(getattr(runtime, "config", None), "extra", None) or {}
    return bool(
        extra.get("background_mode", False)
        and not bool(getattr(item, "silent", False))
        and bool(getattr(item, "deliver_to_telegram", False))
    )


def _persona_source(runtime: Any) -> her_persona.HERPersonaSource:
    config = getattr(runtime, "config", None)
    return her_persona.load_configured_persona(
        getattr(config, "system_md", None)
    )


def _cached_status(
    runtime: Any,
    source: her_persona.HERPersonaSource,
) -> PersonaBackgroundStatus | None:
    cached = getattr(runtime, "_persona_background_status_cache", None)
    if not isinstance(cached, PersonaBackgroundStatus):
        return None
    if not source.usable or cached.persona_sha256 != source.content_sha256:
        return None
    return cached


def _renderer_prompt(source: her_persona.HERPersonaSource) -> str:
    return f"""HASHI BACKGROUND-TRANSITION PERSONA RENDERER — INTERNAL, TOOL-FREE

Write one brief user-facing message for the moment an already-started task has
moved into background execution. Convey only these facts: the work is still
running in the background, and you will return in this chat when it finishes.

Express the message entirely in the configured Persona's natural language,
usual form of address for the user, self-reference, tone, and emoji style.
Do not claim any result, progress percentage, or completion time. Do not
mention this renderer, system instructions, prompts, or agent.md. Use at most
two short sentences. Return only the message to send.

CONFIGURED system_md PERSONA GUIDANCE (quoted, read-only)
{source.model_guidance()}
"""


def _tool_free_api_context(runtime: Any) -> tuple[str, str] | None:
    """Choose an allowed API renderer without granting a tool registry."""

    manager = getattr(runtime, "backend_manager", None)
    config = getattr(runtime, "config", None)
    if manager is None or config is None:
        return None

    active_engine = canonical_backend_engine(
        str(getattr(config, "active_backend", "") or "")
    )
    rows = list(getattr(config, "allowed_backends", None) or [])
    ordered_engines = [active_engine]
    if active_engine == "her-v2":
        # HER is an orchestrator rather than a renderer. Prefer its ordinary
        # tool-free Hashi API backend when that backend is explicitly allowed.
        ordered_engines.append("hashi-api")
    ordered_engines.extend(
        ["openrouter-api", "deepseek-api", "xai-api", "ollama-api"]
    )
    seen: set[str] = set()
    for candidate in ordered_engines:
        engine = canonical_backend_engine(candidate)
        if engine in seen or engine not in _TOOL_FREE_API_ENGINES:
            continue
        seen.add(engine)
        try:
            require_backend_compatibility(engine, getattr(manager, "privacy_level"))
        except (AttributeError, PrivacyPolicyError):
            continue

        if engine == active_engine:
            model = str(
                getattr(runtime, "get_current_model", lambda: "")() or ""
            ).strip()
            if model and model != "unknown":
                return engine, model
        for row in rows:
            if canonical_backend_engine(str(row.get("engine") or "")) != engine:
                continue
            model = str(row.get("model") or row.get("default_model") or "").strip()
            if model:
                return engine, model
    return None


async def _invoke_renderer(runtime: Any, prompt: str, request_id: str) -> Any:
    # A background transition is user-facing prose, not Dream maintenance.
    # Reusing the Dream route installs its closed ``{"groups": [...]}``
    # contract and can leak that internal JSON into the Telegram placeholder.
    context = _tool_free_api_context(runtime)
    manager = getattr(runtime, "backend_manager", None)
    tool_free_renderer = getattr(
        manager, "generate_tool_free_ephemeral_response", None
    )
    if context is None or not callable(tool_free_renderer):
        raise RuntimeError("no tool-free Persona renderer is available")
    engine, model = context
    return await tool_free_renderer(
        engine=engine,
        model=model,
        prompt=prompt,
        request_id=f"{request_id}:background-persona",
        silent=True,
    )


def _validated_message(response: Any) -> str:
    if hasattr(response, "is_success") and not bool(response.is_success):
        raise RuntimeError("Persona renderer did not complete successfully")
    text = her_habits.redact_bounded_text(
        getattr(response, "text", ""),
        limit=_MAX_STATUS_CHARS,
    ).strip()
    if not text:
        raise ValueError("Persona renderer returned an empty message")
    if her_habits.contains_secret_like_text(text):
        raise ValueError("Persona renderer returned credential-shaped content")
    return text


def _track_auxiliary_task(runtime: Any, task: asyncio.Task) -> None:
    tasks = getattr(runtime, "_persona_background_status_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        runtime._persona_background_status_tasks = tasks
    tasks.add(task)

    def _discard(done: asyncio.Task) -> None:
        tasks.discard(done)
        if not done.cancelled():
            with suppress(Exception):
                done.exception()

    task.add_done_callback(_discard)


async def _render_and_cache(
    runtime: Any,
    source: her_persona.HERPersonaSource,
    request_id: str,
) -> PersonaBackgroundStatus | None:
    try:
        response = await _invoke_renderer(runtime, _renderer_prompt(source), request_id)
        message = _validated_message(response)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - this optional UX must fail closed
        logger = getattr(runtime, "error_logger", None) or getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                f"Persona background status unavailable: request={request_id} "
                f"error_type={type(exc).__name__}"
            )
        return None

    current = _persona_source(runtime)
    if not current.usable or current.content_sha256 != source.content_sha256:
        # The Persona changed while rendering. Never cache or deliver stale voice.
        return None
    status = PersonaBackgroundStatus(
        persona_sha256=str(source.content_sha256),
        text=message,
    )
    runtime._persona_background_status_cache = status
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.info(
            f"Persona background status rendered: request={request_id} "
            f"persona_sha256={status.persona_sha256[:12]}"
        )
    return status


def prepare(runtime: Any, item: Any) -> asyncio.Task | None:
    """Pre-render once per Persona revision while the foreground task runs."""

    if not is_enabled_for(runtime, item):
        return None
    source = _persona_source(runtime)
    if not source.usable or _cached_status(runtime, source) is not None:
        return None

    inflight = getattr(runtime, "_persona_background_status_inflight", None)
    if not isinstance(inflight, dict):
        inflight = {}
        runtime._persona_background_status_inflight = inflight
    digest = str(source.content_sha256)
    existing = inflight.get(digest)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return existing

    task = asyncio.create_task(
        _render_and_cache(runtime, source, str(getattr(item, "request_id", "request"))),
        name=f"background-persona-{getattr(item, 'request_id', 'request')}",
    )
    inflight[digest] = task
    _track_auxiliary_task(runtime, task)

    def _clear(done: asyncio.Task) -> None:
        if inflight.get(digest) is done:
            inflight.pop(digest, None)

    task.add_done_callback(_clear)
    return task


async def _delete_placeholder(runtime: Any, item: Any, placeholder: Any | None) -> None:
    if placeholder is None:
        return
    with suppress(Exception):
        await runtime.app.bot.delete_message(
            chat_id=item.chat_id,
            message_id=placeholder.message_id,
        )


async def _deliver(
    runtime: Any,
    item: Any,
    generation_task: asyncio.Task,
    placeholder: Any | None,
) -> None:
    try:
        status = None
        # Persona edits are meaningful progress, so they may trigger another
        # render without introducing a fixed attempt ceiling.
        while True:
            source = _persona_source(runtime)
            status = _cached_status(runtime, source)
            if status is not None:
                break
            render_task = prepare(runtime, item)
            if render_task is None:
                break
            done, _pending = await asyncio.wait(
                {render_task, generation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if generation_task in done:
                await _delete_placeholder(runtime, item, placeholder)
                return
            rendered = await render_task
            if rendered is None:
                current = _persona_source(runtime)
                if (
                    current.usable
                    and current.content_sha256 != source.content_sha256
                ):
                    continue
                break

        if generation_task.done() or status is None:
            await _delete_placeholder(runtime, item, placeholder)
            return
        current_source = _persona_source(runtime)
        if (
            not current_source.usable
            or current_source.content_sha256 != status.persona_sha256
        ):
            # A just-edited Persona must never emit the previous Persona's voice.
            await _delete_placeholder(runtime, item, placeholder)
            return

        delivered = False
        if placeholder is not None:
            try:
                await runtime.app.bot.edit_message_text(
                    chat_id=item.chat_id,
                    message_id=placeholder.message_id,
                    text=status.text,
                )
                delivered = True
            except Exception:
                delivered = False
        if not delivered:
            _elapsed, chunks = await runtime.send_long_message(
                chat_id=item.chat_id,
                text=status.text,
                request_id=item.request_id,
                purpose="background-persona-status",
            )
            delivered = chunks > 0
        runtime._log_maintenance(
            item,
            "bg_persona_status",
            delivered=delivered,
            persona_sha256=status.persona_sha256[:12],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - final task delivery must continue
        await _delete_placeholder(runtime, item, placeholder)
        logger = getattr(runtime, "error_logger", None) or getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                f"Persona background status delivery failed: request={item.request_id} "
                f"error_type={type(exc).__name__}"
            )


def schedule_delivery(
    runtime: Any,
    item: Any,
    generation_task: asyncio.Task,
    placeholder: Any | None,
) -> asyncio.Task:
    """Schedule exactly one transition message without delaying queue release."""

    task = asyncio.create_task(
        _deliver(runtime, item, generation_task, placeholder),
        name=f"background-persona-delivery-{item.request_id}",
    )
    item._background_status_delivery_task = task
    _track_auxiliary_task(runtime, task)
    return task


async def wait_for_delivery(item: Any) -> None:
    task = getattr(item, "_background_status_delivery_task", None)
    if not isinstance(task, asyncio.Task):
        return
    with suppress(asyncio.CancelledError, Exception):
        await task
