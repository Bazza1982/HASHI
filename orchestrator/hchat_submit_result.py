"""Per-call HChat presentation facts; no routing, authorization or retry authority."""
from __future__ import annotations

import errno
import logging
import socket
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable
from urllib.error import URLError

from orchestrator.operation_failures import OperationFailure, OperationRejected, operation_failure

logger = logging.getLogger("HASHI.HChatSubmission")


@dataclass(frozen=True)
class HChatSubmission:
    handled: bool
    status: str
    accepted: bool | None
    failure: OperationFailure | None = None
    recipients: tuple[HChatSubmission, ...] = ()

    def __bool__(self) -> bool:
        return self.handled


@dataclass
class _Capture:
    result: HChatSubmission | None = None


_CAPTURE: ContextVar[_Capture | None] = ContextVar("hchat_submission_capture", default=None)


class _StopRouting(BaseException):
    """An uncertain acknowledgement must bypass legacy transport fallbacks."""


def _diagnosed_failure(error: Exception, *, phase: str, unconfirmed: bool = False) -> OperationFailure:
    failure = operation_failure(error, operation="hchat", phase=phase, unconfirmed=unconfirmed)
    try:
        logger.warning("HChat submission failure: %s", failure.diagnostic())
    except Exception:
        pass
    return failure


def begin_hchat_dispatch() -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        capture.result = None


def record_hchat_failure(error: Exception | None = None, *, reason: str | None = None,
                         phase: str = "dispatch", accepted: bool | None = False, preserve: bool = False) -> bool:
    capture = _CAPTURE.get()
    if capture is None:
        return False
    if preserve and capture.result is not None:
        return False
    failure = _diagnosed_failure(OperationRejected(reason) if reason else error or RuntimeError(),
        phase=phase, unconfirmed=accepted is None)
    capture.result = HChatSubmission(False, "unconfirmed" if accepted is None else "failed", accepted, failure)
    if accepted is None:
        raise _StopRouting()
    return False


def record_hchat_transport_failure(error: Exception, *, phase: str = "request") -> bool:
    capture = _CAPTURE.get()
    known = capture.result if capture is not None else None
    if known is not None and (known.handled or (known.accepted is False and known.failure and known.failure.phase == "dispatch")):
        # A post-receipt bookkeeping failure cannot revoke the receiver's result.
        raise error
    source = error.reason if isinstance(error, URLError) and isinstance(error.reason, Exception) else error
    number = getattr(source, "errno", None)
    refused = isinstance(source, ConnectionRefusedError) or number in {errno.ECONNREFUSED, 10061}
    missing_name = isinstance(source, socket.gaierror)
    reason = "connection_refused" if refused else "name_not_resolved" if missing_name else None
    return record_hchat_failure(source, reason=reason, phase=phase,
        accepted=False if phase == "request" and (refused or missing_name) else None)


def record_hchat_response(payload: dict) -> None:
    capture = _CAPTURE.get()
    if capture is None:
        return
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    state = str(body.get("state") or payload.get("state") or "queued").strip().lower()
    aliases = {"accepted": "queued", "delivered": "sent", "confirming": "awaiting_confirmation",
        "pending_confirmation": "awaiting_confirmation", "confirmation_expired": "expired",
        "confirmation_superseded": "superseded"}
    state = aliases.get(state, state)
    if state not in {"queued", "sent", "awaiting_confirmation", "confirmed", "discarded", "expired", "superseded"}:
        state = "queued"
    capture.result = HChatSubmission(True, state, state not in {"discarded", "expired", "superseded"})


def stop_hchat_routing() -> None:
    capture = _CAPTURE.get()
    if capture is not None and capture.result is not None:
        raise _StopRouting()


def record_hchat_rejection(payload: dict, *, status: int = 0, terminal: bool = True) -> bool:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    code = body.get("code") or payload.get("code") or payload.get("error_code")
    causes = {
        "target_agent_not_found": "target_not_found", "target_not_found": "target_not_found",
        "local_enqueue_failed": "backend_unavailable", "target_agent_unavailable": "backend_unavailable",
        "reply_timeout": "timed_out", "timed_out": "timed_out", "delivery_timed_out": "timed_out",
        "target_instance_not_found": "route_unavailable", "peer_offline": "route_unavailable",
        "auth_required": "authentication_rejected", "auth_failed": "authentication_rejected",
        "invalid_signature": "authentication_rejected", "attachment_capability_missing": "capability_missing",
        "reply_correlation_missing": "correlation_failed", "reply_correlation_mismatch": "correlation_failed",
        "delivery_rejected": "backend_refused", "invalid_message": "backend_refused",
        "handshake_rejected": "backend_refused", "handshake_incompatible": "capability_missing",
    }
    reason = causes.get(code) if isinstance(code, str) else None
    explicit = body.get("accepted", payload.get("accepted"))
    nonacceptance = {"target_agent_not_found", "target_not_found", "target_instance_not_found",
        "auth_required", "auth_failed", "invalid_signature", "attachment_capability_missing",
        "reply_correlation_missing", "reply_correlation_mismatch", "delivery_rejected", "invalid_message",
        "handshake_rejected", "handshake_incompatible"}
    rejected = explicit is False or (isinstance(code, str) and code in nonacceptance) or status in {400, 401, 403, 404, 413, 422}
    if status in {401, 403}:
        reason = "authentication_rejected"
    result = record_hchat_failure(reason=reason or ("backend_refused" if rejected else "response_invalid"),
        phase="dispatch", accepted=False if rejected else None)
    if terminal:
        stop_hchat_routing()
    return result


def capture_hchat_submission(sender: Callable, *args, **kwargs) -> HChatSubmission:
    capture = _Capture()
    token = _CAPTURE.set(capture)
    try:
        try:
            value = sender(*args, **kwargs)
            if isinstance(value, HChatSubmission):
                return value
            handled = bool(value)
        except _StopRouting:
            return capture.result
        except Exception as error:
            if capture.result is not None:
                from dataclasses import replace
                return replace(capture.result, failure=capture.result.failure or _diagnosed_failure(error, phase="persist"))
            failure = _diagnosed_failure(error, phase="dispatch", unconfirmed=True)
            return HChatSubmission(False, "unconfirmed", None, failure)
        if handled:
            return capture.result if capture.result and capture.result.handled else HChatSubmission(True, "queued", True)
        if capture.result is not None:
            return capture.result
        failure = _diagnosed_failure(OperationRejected("response_invalid"), phase="dispatch", unconfirmed=True)
        return HChatSubmission(False, "unconfirmed", None, failure)
    finally:
        _CAPTURE.reset(token)


def record_hchat_group(results: list[HChatSubmission]) -> None:
    capture = _CAPTURE.get()
    if capture is not None:
        capture.result = HChatSubmission(any(item.handled for item in results), "group_result", None,
            next((item.failure for item in results if item.failure), None), tuple(results))
