"""Shared message freshness classification for HASHI network ingress.

Functional owner: PAO
Engineering layer: Functions
The Session API, ordinary HChat requests and Telegram messages treat age as
informational by default. Some reply and Exchange paths retain their existing
handling until independently migrated.
Authentication, replay prevention and idempotency are separate decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from typing import Any, Callable
from collections.abc import Mapping

FRESHNESS_TELEGRAM_SECONDS: int = 30
FRESHNESS_WORKBENCH_LOCAL_SECONDS: int = 30
FRESHNESS_WORKBENCH_REMOTE_SECONDS: int = 60
FRESHNESS_HCHAT_SECONDS: int = 60
CONFIRMATION_EXPIRY_SECONDS: int = 15 * 60  # 15 minutes
FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS: float = 5.0

_RFC3339_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FreshnessResult:
    is_fresh: bool
    reason: str
    age_seconds: float
    threshold_seconds: int
    originated_at: datetime | None
    received_at: datetime
    expires_at: datetime

    @property
    def delay_seconds(self) -> float:
        return self.age_seconds

    @property
    def received_at_iso(self) -> str:
        return self.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def originated_at_iso(self) -> str | None:
        if self.originated_at is None:
            return None
        return self.originated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_fresh": self.is_fresh,
            "reason": self.reason,
            "age_seconds": round(self.age_seconds, 3),
            "threshold_seconds": self.threshold_seconds,
            "originated_at": (
                self.originated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if self.originated_at is not None
                else None
            ),
            "received_at": self.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def age_metadata(self) -> dict[str, Any]:
        """Describe the sender's time without inventing an unknown age."""
        return {
            "reason": self.reason,
            "originated_at": self.originated_at_iso,
            "received_at": self.received_at_iso,
            "age_seconds": (
                round(self.age_seconds, 3) if self.originated_at is not None else None
            ),
        }


def normalize_session_message_age_policy(value: Any) -> dict[str, Any]:
    """Normalize an explicitly selected Session API age policy.

    The default only reports message age. A selected expiry is a hard
    deadline, while a selected confirmation policy retains the old flow.
    This is independent of transport authentication and replay protection.
    """
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - {"mode", "max_age_seconds"}:
        raise ValueError("session_message_age_policy must contain only mode and max_age_seconds")
    raw_mode = value.get("mode", "observe")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise ValueError("session_message_age_policy.mode must be observe, confirm, or expire")
    mode = raw_mode.strip().lower()
    if mode not in {"observe", "confirm", "expire"}:
        raise ValueError("session_message_age_policy.mode must be observe, confirm, or expire")
    max_age = value.get("max_age_seconds")
    if max_age is not None and (type(max_age) is not int or max_age < 1):
        raise ValueError("session_message_age_policy.max_age_seconds must be a positive integer")
    if mode == "expire" and max_age is None:
        raise ValueError("session_message_age_policy.expire requires max_age_seconds")
    if mode == "observe" and max_age is not None:
        raise ValueError("session_message_age_policy.observe does not enforce max_age_seconds")
    return {"mode": mode, "max_age_seconds": max_age}


def normalize_hchat_request_age_policy(value: Any) -> dict[str, Any]:
    """Use the same optional age-policy shape for ordinary HChat requests."""
    try:
        return normalize_session_message_age_policy(value)
    except ValueError as exc:
        raise ValueError(str(exc).replace("session_message_age_policy", "hchat_request_age_policy")) from exc


def normalize_telegram_message_age_policy(value: Any) -> dict[str, Any]:
    """Normalize an optional policy for ordinary Telegram messages."""
    try:
        return normalize_session_message_age_policy(value)
    except ValueError as exc:
        raise ValueError(str(exc).replace("session_message_age_policy", "telegram_message_age_policy")) from exc


def parse_rfc3339_utc(value: str | datetime | None) -> datetime | None:
    """Normalize an aware RFC 3339 timestamp to UTC.

    Returns None if missing, malformed, or lacking an established offset.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not _RFC3339_UTC_PATTERN.match(text):
        return None
    try:
        # Standard fromisoformat handles 'Z' in Python 3.11+
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1].casefold() == "z" else text)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def resolve_freshness_threshold(
    entrypoint: str,
    *,
    is_remote: bool = False,
) -> int:
    norm = str(entrypoint or "").strip().lower()
    if norm in {"telegram", "telegram_bot"}:
        return FRESHNESS_TELEGRAM_SECONDS
    if norm in {"hchat", "hchat-exchange", "exchange", "lan", "remote_hchat"}:
        return FRESHNESS_HCHAT_SECONDS
    if norm in {"workbench", "session_api", "session-api", "api"}:
        return FRESHNESS_WORKBENCH_REMOTE_SECONDS if is_remote else FRESHNESS_WORKBENCH_LOCAL_SECONDS
    # Fallback to local default if unknown
    return FRESHNESS_WORKBENCH_REMOTE_SECONDS if is_remote else FRESHNESS_WORKBENCH_LOCAL_SECONDS


def classify_message_freshness(
    entrypoint: str | None = None,
    *,
    ingress_type: str | None = None,
    originated_at: str | datetime | None,
    is_remote: bool = False,
    received_at: datetime | None = None,
    clock: Callable[[], float | datetime] | None = None,
    threshold_seconds: int | None = None,
) -> FreshnessResult:
    """Classify the freshness of an ingress message.

    Thresholds:
    - Telegram: 30s
    - Workbench (Local): 30s
    - Workbench (Remote): 60s
    - HChat: 60s

    Boundary: age <= threshold is fresh, age > threshold is stale.
    Future skew: up to 5s future is allowed and treated as age 0.0 (fresh).
    > 5s future is flagged as timestamp_in_future.
    """
    if received_at is not None:
        rec_dt = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=timezone.utc)
        rec_dt = rec_dt.astimezone(timezone.utc)
    elif clock is not None:
        clk_val = clock()
        if isinstance(clk_val, datetime):
            rec_dt = clk_val if clk_val.tzinfo is not None else clk_val.replace(tzinfo=timezone.utc)
            rec_dt = rec_dt.astimezone(timezone.utc)
        else:
            rec_dt = datetime.fromtimestamp(float(clk_val), tz=timezone.utc)
    else:
        rec_dt = datetime.now(timezone.utc)

    if threshold_seconds is not None and (
        type(threshold_seconds) is not int or threshold_seconds < 1
    ):
        raise ValueError("threshold_seconds must be a positive integer")
    threshold = threshold_seconds or resolve_freshness_threshold(
        entrypoint or ingress_type or "workbench", is_remote=is_remote
    )
    expires_dt = rec_dt + timedelta(seconds=CONFIRMATION_EXPIRY_SECONDS)

    if originated_at is None or (isinstance(originated_at, str) and not originated_at.strip()):
        return FreshnessResult(
            is_fresh=False,
            reason="timestamp_missing",
            age_seconds=0.0,
            threshold_seconds=threshold,
            originated_at=None,
            received_at=rec_dt,
            expires_at=expires_dt,
        )

    orig_dt = parse_rfc3339_utc(originated_at)
    if orig_dt is None:
        return FreshnessResult(
            is_fresh=False,
            reason="timestamp_invalid",
            age_seconds=0.0,
            threshold_seconds=threshold,
            originated_at=None,
            received_at=rec_dt,
            expires_at=expires_dt,
        )

    delta_seconds = (rec_dt - orig_dt).total_seconds()

    # Check future skew
    if delta_seconds < -FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS:
        return FreshnessResult(
            is_fresh=False,
            reason="timestamp_in_future",
            age_seconds=delta_seconds,
            threshold_seconds=threshold,
            originated_at=orig_dt,
            received_at=rec_dt,
            expires_at=expires_dt,
        )

    # Within future skew tolerance (e.g. between -5.0 and 0.0)
    effective_age = max(0.0, delta_seconds)

    if effective_age <= float(threshold):
        return FreshnessResult(
            is_fresh=True,
            reason="fresh",
            age_seconds=effective_age,
            threshold_seconds=threshold,
            originated_at=orig_dt,
            received_at=rec_dt,
            expires_at=expires_dt,
        )

    return FreshnessResult(
        is_fresh=False,
        reason="stale",
        age_seconds=effective_age,
        threshold_seconds=threshold,
        originated_at=orig_dt,
        received_at=rec_dt,
        expires_at=expires_dt,
    )


classify_freshness = classify_message_freshness
