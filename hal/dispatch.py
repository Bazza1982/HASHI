"""HAL v0.1 dispatch adapters.

The only execution paths HAL uses are the SAME authenticated paths a connected
user uses: prompt dispatch goes through the agent runtime's existing
``enqueue_api_text`` admission (idempotent via the run idempotency key), and
native commands go through ``try_execute_slash_command_text``. HAL never
implements a second executor, and never dispatches when the adapter cannot
prove the operation was accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchResult:
    accepted: bool
    # Transient failures may be retried with the same idempotency key.
    transient: bool
    detail: str
    evidence: dict[str, Any] | None = None


class DispatchAdapter:
    """Abstract adapter; the service only depends on this surface."""

    async def dispatch_prompt(
        self,
        *,
        target_agent: str,
        text: str,
        idempotency_key: str,
        exec_id: str,
        stage: int,
    ) -> DispatchResult:
        raise NotImplementedError

    async def dispatch_command(
        self, *, command_text: str, trigger_agent: str | None
    ) -> DispatchResult:
        raise NotImplementedError


def split_agent_spec(target: str | None) -> tuple[str | None, str | None]:
    """Parse '@name@instance' | '@name' | 'name' into (name, instance|None).

    The instance qualifier is returned so callers must handle it explicitly;
    it is never silently discarded here.
    """
    if not target:
        return None, None
    value = target.strip()
    if value.startswith("@"):
        value = value[1:]
    name, sep, instance = value.partition("@")
    name = name.strip().casefold() or None
    instance = instance.strip().casefold() if sep and instance.strip() else None
    return name, instance


def resolve_agent_target(
    target: str | None, current_instance: str | None
) -> tuple[str | None, str | None]:
    """Resolve a HAL agent spec to a plain runtime name, or return an error.

    Returns ``(name, error)`` — exactly one is ``None``. ``@name@instance`` is
    only accepted when ``instance`` matches ``current_instance``; the suffix is
    NEVER dropped to dispatch to a same-named agent on another instance.
    """
    name, instance = split_agent_spec(target)
    if name is None:
        return None, "empty agent target"
    if instance:
        current = (current_instance or "").strip().casefold()
        if not current or instance != current:
            return None, (
                f"target instance @{instance} is not this instance "
                f"(@{current_instance or 'unknown'}); refusing cross-instance dispatch"
            )
    return name, None


class GatewayDispatchAdapter(DispatchAdapter):
    """Executes through the live Workbench API server (real runtimes only)."""

    def __init__(self, server):
        self._server = server

    def _runtime_for(self, agent_name: str | None):
        runtimes = self._server._runtime_map()
        if not runtimes:
            return None
        if agent_name:
            key = agent_name.strip().lstrip("@").casefold()
            runtime = runtimes.get(key)
            if runtime is not None:
                return runtime
            return None
        # System events have no trigger agent: native commands run through the
        # deterministically first agent runtime (sorted by name).
        return runtimes[sorted(runtimes)[0]]

    async def dispatch_prompt(
        self,
        *,
        target_agent: str,
        text: str,
        idempotency_key: str,
        exec_id: str,
        stage: int,
    ) -> DispatchResult:
        runtime = self._runtime_for(target_agent)
        if runtime is None:
            return DispatchResult(
                accepted=False,
                transient=False,
                detail=f"prompt target @{target_agent} has no live runtime on this instance",
            )
        try:
            await runtime.enqueue_api_text(
                text,
                source="hal",
                deliver_to_telegram=True,
                idempotency_key=idempotency_key,
                request_metadata={
                    "hal_exec_id": exec_id,
                    "hal_stage": stage,
                    "origin": "hal",
                },
            )
            return DispatchResult(
                accepted=True,
                transient=False,
                detail="prompt accepted",
                evidence={"run_idempotency_key": idempotency_key},
            )
        except Exception as exc:  # noqa: BLE001 — adapter must never raise
            return DispatchResult(
                accepted=False,
                transient=True,
                detail=f"prompt dispatch failed transiently: {exc}",
            )

    async def dispatch_command(
        self, *, command_text: str, trigger_agent: str | None
    ) -> DispatchResult:
        from orchestrator.admin_local_testing import try_execute_slash_command_text

        runtime = self._runtime_for(trigger_agent)
        if runtime is None:
            return DispatchResult(
                accepted=False,
                transient=False,
                detail="no live runtime available to execute the native command",
            )
        try:
            result = await try_execute_slash_command_text(
                runtime, command_text, source_channel="hal"
            )
        except Exception as exc:  # noqa: BLE001
            # Native commands have no idempotency key: never auto-retry.
            return DispatchResult(
                accepted=False,
                transient=False,
                detail=f"command outcome unknown ({exc}); manual verification advised",
            )
        if result is None:
            return DispatchResult(
                accepted=False,
                transient=False,
                detail="command not supported or disabled on this instance",
            )
        if isinstance(result, dict) and result.get("ok") is False:
            error = result.get("error") or result.get("message") or "command rejected"
            return DispatchResult(
                accepted=False,
                transient=False,
                detail=f"command rejected: {error}",
                evidence={"result": result},
            )
        # Accepted means the command handler ran and returned ok — it does NOT
        # mean a reboot/deploy side effect has completed.
        return DispatchResult(
            accepted=True,
            transient=False,
            detail="command accepted (accepted != completed)",
            evidence={"result": result},
        )


class RecordingDispatchAdapter(DispatchAdapter):
    """Deterministic adapter for tests; records every call."""

    def __init__(
        self,
        *,
        prompts: dict | None = None,
        commands: dict | None = None,
        prompt_script=None,
    ):
        self.prompt_calls: list[dict[str, Any]] = []
        self.command_calls: list[dict[str, Any]] = []
        self._prompt_results = prompts or {}
        self._command_results = commands or {}
        self._prompt_script = prompt_script

    async def dispatch_prompt(
        self,
        *,
        target_agent: str,
        text: str,
        idempotency_key: str,
        exec_id: str,
        stage: int,
    ) -> DispatchResult:
        self.prompt_calls.append(
            {
                "target_agent": target_agent,
                "text": text,
                "idempotency_key": idempotency_key,
                "exec_id": exec_id,
                "stage": stage,
            }
        )
        if self._prompt_script is not None:
            scripted = self._prompt_script(idempotency_key)
            if scripted is not None:
                return scripted
        default = DispatchResult(accepted=True, transient=False, detail="recorded")
        return self._prompt_results.get(idempotency_key, default)

    async def dispatch_command(
        self, *, command_text: str, trigger_agent: str | None
    ) -> DispatchResult:
        self.command_calls.append(
            {"command_text": command_text, "trigger_agent": trigger_agent}
        )
        default = DispatchResult(accepted=True, transient=False, detail="recorded")
        return self._command_results.get(command_text, default)
# HASHI-Fingerprint: hfp_ce39d5e97777466b84a15f2f75b9487c
