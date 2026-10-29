"""Keep native Telegram card edits on the same PAO Message as their FC view."""

from __future__ import annotations

from orchestrator import runtime_delivery, runtime_session
from orchestrator.frontend_connector_registry import endpoint_id_for


class _View:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class _Query(_View):
    def __init__(self, query, runtime, reference, owner):
        super().__init__(query)
        self.runtime, self.reference, self.owner = runtime, reference, owner

    async def edit_message_text(self, text, **kwargs):
        parse_mode = str(kwargs.get("parse_mode") or "").casefold()
        content_format = (
            "telegram-html"
            if parse_mode == "html"
            else "markdown"
            if parse_mode.startswith("markdown")
            else "plain-text"
        )
        context = runtime_delivery.telegram_presentation_context(
            text=text,
            content_format="telegram-html"
            if parse_mode == "html"
            else "markdown"
            if parse_mode.startswith("markdown")
            else "plain-text",
            presentation_channel="command",
            reply_markup=kwargs.get("reply_markup"),
        )
        # Intent is durable before transport. If Telegram's edit is uncertain,
        # replay cannot run the setting mutation again; other views still see
        # the committed result rather than the stale pre-action text.
        runtime_session.ensure_store(self.runtime).update_presentation_message(
            session_id=self.reference["session_id"],
            owner_id=self.owner,
            agent_id=self.runtime.name,
            message_id=self.reference["message_id"],
            text=text,
            content_format=content_format,
            message_context=context,
        )
        return await self._wrapped.edit_message_text(text, **kwargs)


def project_callback_update(runtime, update):
    """Resolve the card, not the currently selected Session, before admission.

    A pre-migration transport card with no durable identity is not guessed into
    another Session. Its legacy transport path remains unchanged.
    """
    query = update.callback_query
    if getattr(getattr(query, "message", None), "message_id", None) is None:
        return update
    chat = getattr(query.message, "chat", None)
    channel = str(getattr(chat, "id", None) or getattr(query.message, "chat_id", ""))
    owner = runtime_session.owner_id(
        runtime, getattr(update, "_hashi_session_owner_id", None)
    )
    reference = runtime_session.ensure_store(
        runtime
    ).resolve_frontend_transport_reference(
        session_id=None,
        owner_id=owner,
        agent_id=runtime.name,
        connector_id="telegram",
        endpoint_id=endpoint_id_for(
            "telegram", ingress_transport="telegram", channel_key=channel
        ),
        transport_message_id=str(query.message.message_id),
    )
    if reference is None:
        return update
    session = runtime_session.ensure_store(runtime).get_session(
        reference["session_id"],
        owner_id=owner,
        agent_id=runtime.name,
    )
    if int(session["context_generation"]) != reference["context_generation"]:
        raise runtime_session.SessionConflict(
            "Telegram command card context has expired"
        )
    projected = _View(update)
    projected.callback_query = _Query(query, runtime, reference, owner)
    projected._hashi_session_id = reference["session_id"]
    projected._hashi_session_owner_id = owner
    projected._hashi_session_context_generation = reference["context_generation"]
    projected._hashi_session_surface = "telegram"
    projected._hashi_session_channel_key = channel
    return projected
