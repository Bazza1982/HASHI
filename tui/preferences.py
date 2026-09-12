"""Revision-safe persistence for local TUI client preferences."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from orchestrator.config_json import (
    ConfigConflictError,
    read_config_json,
    write_config_json,
)


class TuiPreferenceError(RuntimeError):
    """The preference document could not be read or safely published."""


class TuiPreferenceStore:
    """Keep unrelated preferences while fencing concurrent client writers."""

    def __init__(self, path: str | Path, *, retries: int = 4):
        self.path = Path(path)
        # Retain the keyword for source compatibility.  A stale user action is
        # never replayed against a newer document, regardless of this legacy
        # value.
        self.retries = max(1, int(retries))

    def read(self) -> dict:
        try:
            return dict(read_config_json(self.path))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise TuiPreferenceError(f"TUI preference file is unreadable: {exc}") from exc

    def update(self, mutate: Callable[[dict], None]) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                document = read_config_json(self.path)
                expected_revision: str | None = document.revision
                payload = dict(document)
            except FileNotFoundError:
                expected_revision = None
                payload = {}
            mutate(payload)
            write_config_json(
                self.path,
                payload,
                expected_revision=expected_revision,
            )
            return payload
        except ConfigConflictError as exc:
            raise TuiPreferenceError(
                "TUI preferences changed since they were opened; reopen the setting and try again"
            ) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise TuiPreferenceError(f"TUI preferences were not saved: {exc}") from exc


__all__ = ["TuiPreferenceError", "TuiPreferenceStore"]
