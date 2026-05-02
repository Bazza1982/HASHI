from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

main_logger = logging.getLogger("BridgeU.Orchestrator")


class LifecycleState:
    """Persist and format orchestrator process lifecycle state."""

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path

    def load(self) -> dict:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, state: dict) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            main_logger.warning("Failed to save orchestrator state: %s", e)

    @staticmethod
    def shutdown_meta_text(shutdown_request: dict) -> str:
        req = shutdown_request or {}
        reason = req.get("reason") or "external"
        source = req.get("source") or "unknown"
        detail = req.get("detail") or "-"
        requested_at = req.get("requested_at") or "-"
        return (
            f"reason={reason} source={source} detail={detail} "
            f"requested_at={requested_at}"
        )

    def mark_started(self, pid: int) -> tuple[dict, bool]:
        previous = self.load()
        unexpected_previous_exit = bool(previous) and not previous.get("clean_shutdown", True)
        state = dict(previous)
        state.update({
            "pid": pid,
            "last_started_at": datetime.now().isoformat(),
            "clean_shutdown": False,
            "pending_shutdown_reason": None,
            "pending_shutdown_source": None,
            "pending_shutdown_detail": None,
            "pending_shutdown_requested_at": None,
        })
        self.save(state)
        return previous, unexpected_previous_exit

    def record_shutdown_request(self, shutdown_request: dict) -> None:
        state = self.load()
        state["pending_shutdown_reason"] = shutdown_request.get("reason")
        state["pending_shutdown_source"] = shutdown_request.get("source")
        state["pending_shutdown_detail"] = shutdown_request.get("detail")
        state["pending_shutdown_requested_at"] = shutdown_request.get("requested_at")
        self.save(state)

    def mark_shutdown(self, shutdown_request: dict, clean: bool, phase: str) -> None:
        state = self.load()
        state["last_stopped_at"] = datetime.now().isoformat()
        state["clean_shutdown"] = bool(clean)
        state["last_shutdown_reason"] = shutdown_request.get("reason")
        state["last_shutdown_source"] = shutdown_request.get("source")
        state["last_shutdown_detail"] = shutdown_request.get("detail")
        state["last_shutdown_requested_at"] = shutdown_request.get("requested_at")
        state["last_exit_phase"] = phase
        state["pending_shutdown_reason"] = None
        state["pending_shutdown_source"] = None
        state["pending_shutdown_detail"] = None
        state["pending_shutdown_requested_at"] = None
        self.save(state)
