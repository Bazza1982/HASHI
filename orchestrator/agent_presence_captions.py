"""Disposable captions from the standard, owner-scoped Frontend Event outbox."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from orchestrator import agent_presence_settings


def caption_content(runtime: Any, owner: str, event: Mapping[str, Any]) -> dict | None:
    """Derive a view from the existing route, primary Session and public Message."""
    if (event.get("visibility") != "public" or event.get("audience") != "user"
            or event.get("semantic_kind") not in {"final", "message", "notification"}
            or event.get("presentation_channel") in {"reasoning", "technical", "commentary", "command", "call"}):
        return None
    store = runtime.session_store
    config = runtime.global_config
    route = agent_presence_settings.load_settings(config.bridge_home)
    if route["default_agent"] != runtime.name:
        return None
    session = store.resolve_primary_session(owner_id=owner, agent_id=runtime.name, establish=False)
    if not session or session["session_id"] != event["session_id"] or not event.get("message_id"):
        return None
    message = store.get_message(event["message_id"], session_id=session["session_id"], owner_id=owner)
    if (message.get("role") not in {"user", "assistant"}
            or message.get("visibility") != "visible"
            or message.get("context_generation") != session["context_generation"]):
        return None
    # Use only public semantic text blocks. Media markers, tools and cards are not captions.
    text = "\n".join(str(block.get("text") or "") for block in event.get("content_blocks", ())
                     if isinstance(block, Mapping) and block.get("type") == "text").strip()
    voice = [block for block in event.get("content_blocks", ())
             if isinstance(block, Mapping) and block.get("type") == "media_ref"
             and block.get("role") in {"voice", "voice_message"}]
    if not text and voice:
        text = "\n".join(str(block.get("caption") or "") for block in voice).strip()
    if not text:
        return None
    target = dict(instance_id=config.instance_id, agent_id=runtime.name,
                  session_id=session["session_id"], context_generation=session["context_generation"],
                  route_revision=route["revision"])
    request_id = event.get("request_id")
    if not request_id and event.get("run_id"):
        run = store.get_run(str(event["run_id"]), owner_id=owner)
        if run["session_id"] != session["session_id"]:
            return None
        request_id = run.get("request_id")
    caption_id = ("voice:" + str(event["event_id"])) if voice else (
        str(message["role"]) + ":" + str(request_id or event["message_id"]))
    return dict(target=target, caption_id=caption_id,
        sequence=int(event["sequence"]), speaker=message["role"], text=text,
        phase="preparing" if voice else "final", source="voice" if voice else "text",
        call_id="", speech_id="")


async def deliver_run_caption(runtime: Any, run: Mapping[str, Any], owner: str) -> dict | None:
    """Claim the exact existing final task; no new transcript, delivery route or history scan."""
    from orchestrator.agent_presence_notify import AgentPresenceConnectorAdapter
    from orchestrator.frontend_dispatch import FrontendDispatcher
    store = runtime.session_store
    route = run.get("delivery_route") or {}
    destination = next((item for item in [route.get("primary"), *route.get("mirrors", [])]
                        if item and item.get("surface") == "agentpresence"), None)
    if destination is None:
        return None
    claim = await asyncio.to_thread(
        store.claim_run_delivery_outbox, request_id=str(run["request_id"]), owner_id=owner,
        surface="agentpresence", channel_key=str(destination["channel_key"]),
        worker_id="fc-agentpresence-caption-" + str(run["request_id"])[:48])
    if not claim or claim.get("state") != "claimed":
        return claim
    adapter = AgentPresenceConnectorAdapter(
        runtime.global_config.bridge_home, owner, enabled=True,
        caption_resolver=lambda event: caption_content(runtime, owner, event))
    dispatcher = FrontendDispatcher(store, worker_id="fc-agentpresence-caption",
                                   adapters={"agentpresence": adapter})
    return await dispatcher.dispatch_claimed_task(
        claim["claim"], session_id=claim["session_id"], owner_id=owner)


def schedule_run_caption(runtime: Any, run: Mapping[str, Any], owner: str) -> Any:
    """Optional presentation never changes the canonical terminal outcome."""
    from orchestrator.frontend_agent_presence import _schedule_optional_task
    if run.get("state") != "completed":
        return None
    destinations = [run.get("delivery_route", {}).get("primary"),
                    *run.get("delivery_route", {}).get("mirrors", [])]
    if not any(item and item.get("surface") == "agentpresence" for item in destinations):
        return None
    if len(getattr(runtime, "_optional_frontend_tasks", ())) >= 8:
        return None
    return _schedule_optional_task(runtime, lambda: deliver_run_caption(runtime, run, owner),
                                   str(run["request_id"]))
# HASHI-Fingerprint: hfp_5e254e23c1924a0c94f030e6bd54d9e0
