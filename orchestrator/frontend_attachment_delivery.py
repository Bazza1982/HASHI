"""Frontend Connector delivery for canonical managed assistant attachments."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from orchestrator.audio_assets import AudioAssetError
from orchestrator.session_store import SessionConflict, SessionNotFound, SessionStore


def _result(
    *,
    state: str,
    complete: bool,
    receipts: list[dict[str, str]] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    items = list(receipts or ())
    delivered = sum(1 for item in items if item.get("state") == "delivered")
    result: dict[str, Any] = {
        "state": str(state),
        "complete": bool(complete),
        "attempted": len(items),
        "delivered": delivered,
        "failed": len(items) - delivered,
        "receipts": items,
    }
    if reason:
        result["reason"] = str(reason)
    return result


def _telegram_method(part: Mapping[str, Any]) -> tuple[str, str]:
    modality = str(part.get("modality") or "").strip().casefold()
    if modality == "image":
        return "send_photo", "photo"
    if modality == "audio":
        return "send_audio", "audio"
    if modality == "video":
        return "send_video", "video"
    return "send_document", "document"


async def send_telegram_run_attachments(runtime: Any, item: Any) -> dict[str, Any]:
    """Project one completed Run's managed attachments to Telegram exactly once.

    This function performs no retry.  A Telegram message identifier is required
    before an item is reported as delivered, and returned receipts never expose
    instance-local paths or file contents.
    """

    store = getattr(runtime, "session_store", None)
    if not isinstance(store, SessionStore):
        return _result(state="not_applicable", complete=True)
    request_id = str(getattr(item, "request_id", "") or "").strip()
    owner_id = str(getattr(item, "owner_id", "") or "").strip()
    agent_id = str(getattr(runtime, "name", "") or "").strip().casefold()
    if not request_id or not owner_id or not agent_id:
        # Legacy/non-Session turns cannot have used the managed binding tool.
        return _result(state="not_applicable", complete=True)
    try:
        run = store.get_run_by_request(
            request_id,
            owner_id=owner_id,
            agent_id=agent_id,
        )
    except SessionNotFound:
        return _result(state="not_applicable", complete=True)
    expected_run_id = str(getattr(item, "run_id", "") or "").strip()
    if expected_run_id and str(run.get("run_id") or "") != expected_run_id:
        return _result(
            state="rejected",
            complete=False,
            reason="run_binding_changed",
        )
    if str(run.get("state") or "") != "completed":
        return _result(
            state="rejected",
            complete=False,
            reason="run_not_completed",
        )
    try:
        attachments = store.run_output_attachment_content(
            request_id,
            owner_id=owner_id,
            agent_id=agent_id,
        )
    except (AudioAssetError, OSError, SessionConflict, SessionNotFound):
        return _result(
            state="unavailable",
            complete=False,
            reason="managed_attachment_unavailable",
        )
    if not attachments:
        return _result(state="not_applicable", complete=True)

    existing_outcome = store.assistant_delivery_outcome(
        request_id,
        surface="telegram",
        channel_key=str(getattr(item, "chat_id", "") or ""),
    )
    if existing_outcome is not None:
        detail = existing_outcome.get("detail")
        raw_receipts = (
            detail.get("attachment_receipts") if isinstance(detail, Mapping) else None
        )
        if isinstance(raw_receipts, list) and raw_receipts:
            receipts = [dict(receipt) for receipt in raw_receipts]
            delivered = sum(
                1 for receipt in receipts if receipt.get("state") == "delivered"
            )
            if delivered == len(receipts):
                state = "delivered"
            elif delivered:
                state = "partial"
            else:
                state = "failed"
            replay = _result(
                state=state,
                complete=delivered == len(receipts),
                receipts=receipts,
            )
            replay["replayed"] = True
            return replay
        return _result(
            state="rejected",
            complete=False,
            reason="delivery_outcome_already_recorded",
        )

    bot = getattr(getattr(runtime, "app", None), "bot", None)
    receipts: list[dict[str, str]] = []
    for part in attachments:
        attachment_id = str(part.get("attachment_id") or "").strip()
        method_name, argument_name = _telegram_method(part)
        sender = getattr(bot, method_name, None)
        if not attachment_id or not callable(sender):
            receipts.append(
                {
                    "attachment_id": attachment_id,
                    "state": "failed",
                    "error_type": "transport_unavailable",
                }
            )
            continue
        path = Path(str(part.get("local_ref") or ""))
        caption = str(part.get("caption") or "").strip()
        kwargs: dict[str, Any] = {"chat_id": getattr(item, "chat_id", None)}
        try:
            if len(caption) > 1024:
                raise ValueError("attachment caption exceeds Telegram limit")
            if caption:
                kwargs["caption"] = caption
            with path.open("rb") as handle:
                kwargs[argument_name] = handle
                message = await sender(**kwargs)
            message_id = str(getattr(message, "message_id", "") or "").strip()
            if not message_id:
                raise RuntimeError("missing Telegram transport receipt")
            receipts.append(
                {
                    "attachment_id": attachment_id,
                    "state": "delivered",
                    "transport_message_id": message_id,
                }
            )
        except Exception as exc:
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(
                    "Managed attachment delivery failed for %s (%s)",
                    attachment_id,
                    type(exc).__name__,
                )
            receipts.append(
                {
                    "attachment_id": attachment_id,
                    "state": "failed",
                    "error_type": type(exc).__name__,
                }
            )

    delivered = sum(1 for receipt in receipts if receipt["state"] == "delivered")
    if delivered == len(receipts):
        state = "delivered"
    elif delivered:
        state = "partial"
    else:
        state = "failed"
    return _result(
        state=state,
        complete=delivered == len(receipts),
        receipts=receipts,
    )


__all__ = ["send_telegram_run_attachments"]
