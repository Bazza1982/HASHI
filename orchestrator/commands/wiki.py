"""Generic HASHI Wiki command and provider contract.

This module contains no knowledge data, local path, deployment convention, or
credential. Instances supply only provider identity and a permitted capability.
"""

from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    return bool(checker(user_id)) if callable(checker) else True


async def _reply(runtime: Any, update: Any, text: str) -> None:
    responder = getattr(runtime, "_reply_text", None)
    if callable(responder):
        await responder(update, text, parse_mode="HTML")
        return
    message = getattr(update, "effective_message", None)
    if message is not None:
        await message.reply_text(text, parse_mode="HTML")


def build_wiki_prompt(*, query: str, provider_id: str, capability: str) -> str:
    """Build the public provider-neutral retrieval prompt."""

    return (
        "HASHI CORE WIKI RETRIEVAL REQUEST\n\n"
        f"Knowledge provider id: {provider_id}\n"
        f"Authorised retrieval capability: {capability}\n\n"
        "Use only that currently permitted capability to search and read the configured "
        "curated Wiki. Preserve source provenance, distinguish retrieved facts from inference, "
        "and say clearly when evidence is absent. Do not search raw cross-Agent memory, infer "
        "a local path, or expose provider configuration or credentials.\n\n"
        "AUTHORITATIVE USER WIKI QUERY:\n"
        + query
    )


async def wiki_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    query = " ".join(
        str(value).strip()
        for value in (getattr(context, "args", None) or [])
        if str(value).strip()
    )
    if not query:
        await _reply(runtime, update, "Usage: <code>/wiki &lt;question&gt;</code>")
        return

    provider = dict(getattr(runtime.global_config, "wiki_provider", None) or {})
    provider_id = str(provider.get("id") or "").strip()
    capability = str(provider.get("capability") or "").strip()
    if not provider_id or not capability:
        await _reply(
            runtime,
            update,
            "⚠️ <b>Wiki unavailable.</b> This HASHI instance has no configured Wiki provider.",
        )
        return

    connectivity_check = getattr(runtime, "_is_hashi_tool_connected", None)
    connected = (
        bool(connectivity_check(capability))
        if callable(connectivity_check)
        else capability
        in {
            item["name"]
            for item in getattr(runtime, "_get_available_tool_catalogue")()
        }
    )
    if not connected:
        await _reply(
            runtime,
            update,
            "⚠️ <b>Wiki unavailable.</b> The configured retrieval capability is not "
            "authorised or connected for the active backend.",
        )
        return

    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    request_id = await runtime.enqueue_request(
        chat_id=chat_id,
        prompt=build_wiki_prompt(
            query=query,
            provider_id=provider_id,
            capability=capability,
        ),
        source="wiki:query",
        summary=f"Wiki query: {query[:120]}",
        request_metadata={
            "tool_allowlist": [capability],
            "wiki_provider_id": provider_id,
        },
    )
    if not request_id:
        await _reply(runtime, update, "⚠️ <b>Wiki request could not be queued.</b>")


COMMANDS = [
    RuntimeCommand(
        name="wiki",
        description="Search the configured curated Wiki",
        callback=wiki_command,
    )
]
