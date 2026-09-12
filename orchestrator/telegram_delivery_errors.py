"""Safe, typed Telegram delivery outcomes across Function Worker RPC."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from math import ceil
from typing import Any

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut


def _retry_after_seconds(value: Any) -> int:
    if isinstance(value, timedelta):
        return max(0, ceil(value.total_seconds()))
    try:
        return max(0, ceil(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


class TelegramDeliveryError(RuntimeError):
    """A transport-safe delivery classification with no credential material."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        permanent: bool,
        retry_after_s: int | None = None,
        error_type: str = "TelegramError",
        reason: str | None = None,
    ) -> None:
        self.code = str(code or "delivery_failed")
        self.retryable = bool(retryable)
        self.permanent = bool(permanent)
        self.retry_after_s = (
            None if retry_after_s is None else max(0, int(retry_after_s))
        )
        self.error_type = str(error_type or "TelegramError")
        self.reason = str(reason or self.code)
        super().__init__(f"{self.code}: {self.reason}")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "permanent": self.permanent,
            "retry_after_s": self.retry_after_s,
            "error_type": self.error_type,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TelegramDeliveryError":
        return cls(
            str(value.get("code") or "delivery_failed"),
            retryable=bool(value.get("retryable", False)),
            permanent=bool(value.get("permanent", False)),
            retry_after_s=value.get("retry_after_s"),
            error_type=str(value.get("error_type") or "TelegramError"),
            reason=str(value.get("reason") or value.get("code") or "delivery_failed"),
        )


def classify_telegram_delivery_error(exc: BaseException) -> TelegramDeliveryError:
    """Classify by exception type first; text only refines ``BadRequest``."""

    if isinstance(exc, TelegramDeliveryError):
        return exc
    error_type = type(exc).__name__
    if isinstance(exc, RetryAfter):
        return TelegramDeliveryError(
            "retry_after",
            retryable=True,
            permanent=False,
            retry_after_s=_retry_after_seconds(getattr(exc, "retry_after", 0)),
            error_type=error_type,
            reason="telegram_rate_limit",
        )
    if isinstance(exc, Forbidden):
        return TelegramDeliveryError(
            "destination_forbidden",
            retryable=False,
            permanent=True,
            error_type=error_type,
            reason="telegram_destination_rejected_permission",
        )
    if isinstance(exc, BadRequest):
        normalized = " ".join(str(exc).casefold().split())
        code = (
            "destination_not_found"
            if "chat not found" in normalized
            else "destination_bad_request"
        )
        return TelegramDeliveryError(
            code,
            retryable=False,
            permanent=True,
            error_type=error_type,
            reason="telegram_destination_rejected",
        )
    if isinstance(exc, (TimedOut, NetworkError, TimeoutError, ConnectionError, OSError)):
        return TelegramDeliveryError(
            "transient_transport",
            retryable=True,
            permanent=False,
            error_type=error_type,
            reason="telegram_transport_temporarily_unavailable",
        )
    return TelegramDeliveryError(
        "transient_unknown",
        retryable=True,
        permanent=False,
        error_type=error_type,
        reason="unclassified_delivery_failure",
    )
