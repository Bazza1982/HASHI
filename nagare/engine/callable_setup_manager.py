"""
Nagare callable auto-setup manager.

When a workflow step targets a callable (backend: callable) that has no Python
function registered, this manager drives an automated AI-implementation loop:

  1. Sends step context to an AI agent via the notifier (HChat)
  2. Blocks the runner thread on a threading.Event with a configurable timeout
  3. When the AI POSTs code to POST /runs/{run_id}/callables/{agent_id}, the
     manager exec()s it, stores the compiled function, and fires the event
  4. The blocked thread wakes, registers the function, and retries execution
  5. Retries up to MAX_RETRIES (default 3) times before escalating to human

State (retry counts, outcomes) is persisted to:
    flow/runs/{run_id}/callable_setup.json

Implemented callables are saved to:
    flow/callables/{agent_id}.py  (auto-loaded on future runs)
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nagare.protocols.notifier import Notifier, NullNotifier


MAX_RETRIES = 3
WAIT_TIMEOUT_SECONDS = 300  # per attempt (5 minutes)

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallableSetupManager:
    """
    Thread-safe manager for the callable auto-setup loop.
    One instance per workflow run.
    """

    def __init__(
        self,
        run_id: str,
        *,
        runs_root: str | Path = "flow/runs",
        callables_root: str | Path = "flow/callables",
        notifier: Notifier | None = None,
        ai_agent_id: str = "akane",
        api_base_url: str = "http://127.0.0.1:8787",
    ) -> None:
        self.run_id = run_id
        self.runs_root = Path(runs_root)
        self.callables_root = Path(callables_root)
        self.notifier = notifier or NullNotifier()
        self.ai_agent_id = ai_agent_id
        self.api_base_url = api_base_url.rstrip("/")

        self._lock = threading.Lock()
        # agent_id → {"retry_count": int, "event": threading.Event}
        self._entries: dict[str, dict] = {}
        # agent_id → compiled callable (delivered via deliver_code, not yet consumed)
        self._pending_callables: dict[str, Any] = {}
        self._state_path = self.runs_root / run_id / "callable_setup.json"

    # -------------------------------------------------------------------------
    # Called by callable_handler — runner thread blocks here
    # -------------------------------------------------------------------------

    def request_setup(
        self,
        agent_id: str,
        task_message: dict,
        *,
        attempt: int,
    ) -> threading.Event:
        """
        Send an AI implementation request for agent_id and return a gate Event
        that will be set when code is delivered via deliver_code().

        Call this BEFORE blocking on the returned event.
        `attempt` is 1-indexed (1 = first attempt, 2 = first retry, …).
        """
        event = self._get_fresh_event(agent_id)
        self._send_ai_prompt(agent_id, task_message, attempt=attempt)
        return event

    def wait_for_setup(
        self,
        agent_id: str,
        event: threading.Event,
    ) -> bool:
        """
        Block until code is delivered or timeout expires.
        Returns True if code was delivered, False on timeout.
        """
        delivered = event.wait(timeout=WAIT_TIMEOUT_SECONDS)
        if not delivered:
            logger.warning(
                "Callable setup timed out after %ds for agent_id='%s'",
                WAIT_TIMEOUT_SECONDS,
                agent_id,
            )
        return delivered

    def escalate_to_human(self, agent_id: str, task_message: dict) -> None:
        """
        Called after MAX_RETRIES exhausted — notify human operator.
        """
        step_id = task_message.get("payload", {}).get("step_id", "?")
        step_prompt = task_message.get("payload", {}).get("prompt", "")
        msg = (
            f"🚨 Callable auto-setup FAILED after {MAX_RETRIES} attempts\n\n"
            f"agent_id: {agent_id}\n"
            f"step_id: {step_id}\n"
            f"run_id: {self.run_id}\n"
            f"step prompt:\n{step_prompt[:500]}\n\n"
            f"The AI agent was unable to implement a working callable in {MAX_RETRIES} tries.\n"
            f"Please implement manually and POST to:\n"
            f"  {self.api_base_url}/runs/{self.run_id}/callables/{agent_id}\n"
            f"  Body: {{\"code\": \"def run(task_message): ...\"}}"
        )
        try:
            self.notifier.send(
                agent_id=self.ai_agent_id,
                text=msg,
                run_id=self.run_id,
            )
        except Exception as exc:
            logger.warning("Failed to send escalation notification: %s", exc)

    # -------------------------------------------------------------------------
    # Called by API endpoint — delivers AI-written code
    # -------------------------------------------------------------------------

    def deliver_code(self, agent_id: str, code: str) -> dict:
        """
        Exec and validate AI-written Python code.

        On success: stores the compiled callable, fires the gate event, and
        persists the code to flow/callables/{agent_id}.py.

        Returns {"ok": True} or {"ok": False, "error": "<reason>"}.
        """
        namespace: dict = {}
        try:
            compiled = compile(code, f"<callable:{agent_id}>", "exec")
            exec(compiled, namespace)  # noqa: S102
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("exec() failed for agent_id='%s': %s\n%s", agent_id, exc, tb)
            return {"ok": False, "error": f"compile/exec error: {exc}"}

        fn = namespace.get("run")
        if fn is None:
            return {"ok": False, "error": "code must define a top-level function named 'run'"}
        if not callable(fn):
            return {"ok": False, "error": "'run' is defined but is not callable"}

        # Persist to disk for future run auto-loading
        try:
            self.callables_root.mkdir(parents=True, exist_ok=True)
            (self.callables_root / f"{agent_id}.py").write_text(code, encoding="utf-8")
            logger.info("Callable '%s' persisted to %s", agent_id, self.callables_root / f"{agent_id}.py")
        except Exception as exc:
            logger.warning("Failed to persist callable '%s': %s", agent_id, exc)

        with self._lock:
            self._pending_callables[agent_id] = fn
            entry = self._entries.get(agent_id)
            if entry:
                entry["event"].set()
            self._persist_state()

        logger.info("Callable for agent_id='%s' delivered and compiled OK", agent_id)
        return {"ok": True}

    def pop_pending_callable(self, agent_id: str) -> Any | None:
        """
        Retrieve (and clear) a compiled callable that was delivered.
        Returns None if nothing is waiting.
        """
        with self._lock:
            return self._pending_callables.pop(agent_id, None)

    # -------------------------------------------------------------------------
    # Retry accounting
    # -------------------------------------------------------------------------

    def get_retry_count(self, agent_id: str) -> int:
        with self._lock:
            return self._entries.get(agent_id, {}).get("retry_count", 0)

    def increment_retry(self, agent_id: str) -> int:
        with self._lock:
            entry = self._entries.setdefault(agent_id, {"retry_count": 0, "event": threading.Event()})
            entry["retry_count"] += 1
            count = entry["retry_count"]
            self._persist_state()
        return count

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_fresh_event(self, agent_id: str) -> threading.Event:
        """Return a new, unset Event for the next wait cycle."""
        with self._lock:
            entry = self._entries.setdefault(agent_id, {"retry_count": 0, "event": threading.Event()})
            # Always create a fresh event for each attempt
            entry["event"] = threading.Event()
            return entry["event"]

    def _send_ai_prompt(
        self,
        agent_id: str,
        task_message: dict,
        *,
        attempt: int,
    ) -> None:
        step_id = task_message.get("payload", {}).get("step_id", "?")
        step_prompt = task_message.get("payload", {}).get("prompt", "")
        workflow_id = task_message.get("workflow_id", "?")
        params = task_message.get("payload", {}).get("params", {})
        input_artifacts = task_message.get("payload", {}).get("input_artifacts", {})
        output_spec = task_message.get("payload", {}).get("output_spec", [])

        retry_note = f" (attempt {attempt}/{MAX_RETRIES})" if attempt > 1 else ""

        prompt = (
            f"🤖 Callable auto-setup needed{retry_note}\n\n"
            f"Workflow `{workflow_id}` step `{step_id}` is configured as "
            f"`backend: callable` but no Python function is registered for "
            f"`agent_id='{agent_id}'`.\n\n"
            f"**Step intent (from workflow):**\n{step_prompt}\n\n"
            f"**Input available:**\n{json.dumps(input_artifacts, indent=2)}\n\n"
            f"**Params:**\n{json.dumps(params, indent=2)}\n\n"
            f"**Output spec:**\n{json.dumps(output_spec, indent=2)}\n\n"
            f"**Task:**\n"
            f"Implement a Python function `run(task_message: dict) -> dict` that "
            f"fulfills the step intent above. The function receives the full "
            f"task_message dict and must return a dict with at minimum "
            f"`{{\"status\": \"completed\"}}` on success or "
            f"`{{\"status\": \"failed\", \"error\": \"...\"}}` on failure.\n\n"
            f"POST the code to:\n"
            f"  {self.api_base_url}/runs/{self.run_id}/callables/{agent_id}\n"
            f"  Body: {{\"code\": \"<your Python code>\"}}\n\n"
            f"The runner thread is blocked waiting for your implementation "
            f"(timeout: {WAIT_TIMEOUT_SECONDS}s).\n"
            f"run_id: {self.run_id}"
        )

        try:
            self.notifier.send(
                agent_id=self.ai_agent_id,
                text=prompt,
                run_id=self.run_id,
            )
            logger.info(
                "Callable setup prompt sent to '%s' for agent_id='%s' (attempt %d)",
                self.ai_agent_id,
                agent_id,
                attempt,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send callable setup prompt for '%s': %s",
                agent_id,
                exc,
            )

    def _persist_state(self) -> None:
        """Write retry counts to disk (Events are not JSON-serialisable)."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                agent_id: {
                    "retry_count": entry.get("retry_count", 0),
                    "updated_at": utc_now(),
                }
                for agent_id, entry in self._entries.items()
            }
            self._state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to persist callable_setup state: %s", exc)
