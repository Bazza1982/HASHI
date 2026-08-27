"""HASHI compatibility facade for the modular HER v2 orchestrator."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from adapters import her_persona
from adapters.base import BackendCapabilities, BackendResponse, BaseBackend
from adapters.her_habits import HabitMeditationConfig
from adapters.her_v2_provider import (
    HashiStageProvider,
    _AdapterDelivery,
    _backend_response_error,
    _ConfiguredPersonaPackager,
    _manager_authorises_profile,
    _provider_exception_error,
    _UnboundedToolRegistry,
)
from adapters.stream_events import StreamCallback
from orchestrator.her_v2.audit import AuditPersistenceError, DurableAuditLog
from orchestrator.her_v2.commentary import PersonaCommentaryPipeline
from orchestrator.her_v2.config import (
    HERv2Config,
    HERv2ConfigurationError,
    ProviderProfile,
)
from orchestrator.her_v2.interfaces import (
    ProviderFailureCode,
    StageInvocationError,
    StageProvider,
)
from orchestrator.her_v2.learning import HERv2Learning
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageRequest,
    StageResponse,
    TerminalState,
)
from orchestrator.her_v2.progress import ProviderActivityTracker
from orchestrator.her_v2.request_policy import resolve_request_effort
from orchestrator.her_v2.retry import (
    DEFAULT_PROVIDER_RETRY_POLICY,
    ProviderRetryPolicy,
)
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.her_v2.wip_journal import WIPJournal
from orchestrator.multimodal_contract import resolve_input_capability

HER_V2_DISPLAY_NAME = "HASHI Engine Runtime v2"
HER_V2_VERSION = "2.0.0-alpha.1"

__all__ = [
    "HER_V2_DISPLAY_NAME",
    "HER_V2_VERSION",
    "HERv2Adapter",
    "HashiStageProvider",
    "_AdapterDelivery",
    "_ConfiguredPersonaPackager",
    "_UnboundedToolRegistry",
    "_backend_response_error",
]


class _ExecutionStageCompactionProvider:
    """Fire one detached Compact trigger when main Execution first starts."""

    def __init__(self, base: StageProvider, on_execution: Callable[[], Any]) -> None:
        self._base = base
        self._on_execution = on_execution
        self._scheduled = False

    def tool_catalogue(
        self,
        *,
        allow_side_effects: bool,
        delegated_tools=None,
    ):
        resolver = getattr(self._base, "tool_catalogue", None)
        if not callable(resolver):
            return ()
        return resolver(
            allow_side_effects=allow_side_effects,
            delegated_tools=delegated_tools,
        )

    async def invoke(
        self,
        profile: ProviderProfile,
        request: StageRequest,
    ) -> StageResponse:
        if (
            not self._scheduled
            and request.stage is Stage.EXECUTION
            and not request.role.startswith("sub_agent:")
        ):
            self._scheduled = True
            # The callback only creates a background maintenance task.  Never
            # await it or couple its outcome to Execution.
            try:
                self._on_execution()
            except Exception:
                # Even a bug in trigger setup must not delay or fail Execution.
                pass
        return await self._base.invoke(profile, request)


class HERv2Adapter(BaseBackend):
    """HASHI facade for the provider-neutral, pure-Python HER v2 runtime."""

    DEFAULT_IDLE_TIMEOUT_SEC = 30 * 60
    habit_pipeline_owner = "her_v2_runtime"

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
            supports_commentary_stream=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=False,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.HERv2.{self.config.name}")
        self.tool_registry = None
        self._v2_config: HERv2Config | None = None
        self._ledger_store: LedgerStore | None = None
        self._audit_log: DurableAuditLog | None = None
        self._wip_journal: WIPJournal | None = None
        self._wip_active_request_refs: set[str] = set()
        self._learning: HERv2Learning | None = None
        self._active_runtimes: dict[str, HERv2Runtime] = {}
        self._pending_delivery_receipts: dict[str, dict[str, Any]] = {}
        self._recorded_delivery_ids: set[str] = set()
        self._initialized = False
        self.effort = "medium"

    @property
    def _extra(self) -> dict[str, Any]:
        raw = getattr(self.config, "extra", None) or {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _runtime_context(self) -> Any:
        return getattr(self.config, "_hashi_runtime", None)

    def _backend_manager(self) -> Any:
        runtime = self._runtime_context()
        return getattr(runtime, "backend_manager", None)

    def _observe_wip_audit(self, record: Mapping[str, Any]) -> None:
        request_ref = str(record.get("request_ref") or "")
        if (
            self._wip_journal is not None
            and request_ref in self._wip_active_request_refs
        ):
            self._wip_journal.append_audit(record)

    def accepts_media_input(self, modality: str) -> bool:
        """Prove ingress support from configured stage provider/model pairs.

        HER is an orchestrator, so its outer ``supports_files`` flag cannot
        establish what any foreground stage can actually consume.  This
        ingress check stays fail-closed and only admits a modality when at
        least one configured stage has an exact native transport.  Local tool
        fallbacks are evaluated separately by the runtime.
        """

        resolved = self._v2_config
        if resolved is None:
            raw = self._extra.get("her_v2")
            if not isinstance(raw, Mapping):
                return False
            try:
                resolved = HERv2Config.from_mapping(raw)
            except HERv2ConfigurationError:
                return False

        manager = self._backend_manager()
        select_backend = getattr(manager, "_select_backend_cfg", None)
        normalized_modality = str(modality or "").strip().casefold()
        for profile in resolved.all_provider_profiles():
            capability_config: dict[str, Any] = {}
            if callable(select_backend):
                try:
                    configured = select_backend(
                        profile.engine,
                        target_model=profile.model,
                    )
                except (TypeError, ValueError):
                    configured = None
                if isinstance(configured, Mapping):
                    nested_extra = configured.get("extra")
                    if isinstance(nested_extra, Mapping):
                        capability_config.update(dict(nested_extra))
                    capability_config.update(dict(configured))
            capability_config.update(dict(profile.options))
            capability = resolve_input_capability(
                profile.engine,
                profile.model,
                config=capability_config,
            )
            transports = capability.input_transports.get(
                normalized_modality,
                (),
            )
            if any(
                capability.supports(normalized_modality, transport)
                for transport in transports
            ):
                return True
        return False

    def _habit_meditation_config(self) -> HabitMeditationConfig:
        """Resolve V2 learning controls with the existing persisted override."""

        extra = self._extra
        raw_v2 = extra.get("her_v2")
        raw_v2 = dict(raw_v2) if isinstance(raw_v2, Mapping) else {}
        resolved_extra = dict(extra)
        nested = raw_v2.get("habit_meditation")
        if isinstance(nested, Mapping):
            resolved_extra["habit_meditation"] = dict(nested)
        if "meditation_enabled" in raw_v2:
            resolved_extra["habit_meditation_enabled"] = raw_v2["meditation_enabled"]
        # FlexibleBackendManager writes this top-level value for an explicit
        # agent-local /habit override. It must win over backend defaults.
        if "habit_meditation_enabled" in extra:
            resolved_extra["habit_meditation_enabled"] = extra[
                "habit_meditation_enabled"
            ]
        return HabitMeditationConfig.resolve(
            self.global_config,
            resolved_extra,
        )

    def _habit_request_eligible(self, request_id: str) -> bool:
        """Preserve the old HER request-scoped learning exclusion contract."""

        extra = self._extra
        if (
            bool(extra.get("ephemeral_session"))
            or extra.get("habit_learning_eligible") is False
        ):
            return False
        meta = self._runtime_request_meta(request_id)
        if not meta or "habit_learning_eligible" not in meta:
            return True
        return bool(meta.get("habit_learning_eligible"))

    def _direct_skill_catalogue(self) -> tuple[dict[str, Any], ...]:
        """Expose enabled task Skills as a bounded Direct-route index."""

        runtime = self._runtime_context()
        manager = getattr(runtime, "skill_manager", None)
        list_skills = getattr(manager, "list_skills", None)
        is_enabled = getattr(manager, "is_skill_enabled", None)
        if not callable(list_skills):
            return ()
        runtime_toggle_ids = set(getattr(manager, "RUNTIME_TOGGLE_IDS", ()))
        rows: list[dict[str, Any]] = []
        try:
            skills = list_skills()
        except Exception as exc:
            self.logger.warning(
                "HER v2 Direct Skill catalogue unavailable: %s", type(exc).__name__
            )
            return ()
        for skill in skills:
            skill_id = str(getattr(skill, "id", "") or "").strip()
            if not skill_id or skill_id in runtime_toggle_ids:
                continue
            if callable(is_enabled) and not is_enabled(
                Path(self.config.workspace_dir), skill_id
            ):
                continue
            skill_dir = Path(getattr(skill, "skill_dir", ""))
            rows.append(
                {
                    "id": skill_id,
                    "name": str(getattr(skill, "name", "") or skill_id),
                    "description": str(getattr(skill, "description", "") or ""),
                    "skill_md": str(skill_dir / "SKILL.md"),
                    "allowed_tools": getattr(skill, "allowed_tools", None),
                }
            )
        return tuple(rows)

    def _runtime_request_meta(self, request_id: str) -> dict[str, Any]:
        runtime = self._runtime_context()
        registry = getattr(runtime, "_request_meta_by_id", None)
        if isinstance(registry, Mapping):
            value = registry.get(str(request_id or ""))
            if isinstance(value, Mapping):
                return dict(value)
        current = getattr(runtime, "current_request_meta", None)
        if isinstance(current, Mapping) and str(current.get("request_id") or "") == str(
            request_id or ""
        ):
            return dict(current)
        return {}

    def _schedule_execution_stage_compaction(self, request_id: str) -> bool:
        runtime = self._runtime_context()
        if runtime is None:
            return False
        request_tokens = getattr(runtime, "_context_compaction_prompt_tokens", None)
        prompt_tokens = (
            int(request_tokens.get(request_id) or 0)
            if isinstance(request_tokens, Mapping)
            else 0
        )
        if prompt_tokens <= 0:
            return False
        meta = self._runtime_request_meta(request_id)
        try:
            from orchestrator.context_compaction import schedule_execution_stage

            return schedule_execution_stage(
                runtime,
                request_ref=request_id,
                prompt_tokens=prompt_tokens,
                chat_id=meta.get("chat_id"),
                deliver_to_telegram=bool(meta.get("deliver_to_telegram")),
            )
        except Exception as exc:  # scheduling must never enter HER control flow
            self.logger.warning(
                "Execution-stage Compact trigger failed safely request=%s error=%s",
                request_id,
                type(exc).__name__,
            )
            try:
                from orchestrator import runtime_pipeline

                runtime_pipeline.surface_context_compaction_warnings(
                    runtime,
                    SimpleNamespace(
                        request_id=request_id,
                        chat_id=meta.get("chat_id"),
                        deliver_to_telegram=bool(meta.get("deliver_to_telegram")),
                    ),
                    (
                        "⚠️ <b>HER v2 context compaction warning</b>\n\n"
                        "Automatic Compact could not be started, but Execution "
                        "continued without waiting for it.\n"
                        f"<b>Error type</b> · <code>{type(exc).__name__}</code>",
                    ),
                )
            except Exception:
                pass
            return False

    def _habit_notification_context(
        self, request_id: str, *, silent: bool
    ) -> dict[str, Any]:
        meta = self._runtime_request_meta(request_id)
        return {
            "chat_id": meta.get("chat_id"),
            "verbose_at_start": bool(meta.get("verbose_at_start")),
            "meter_at_start": bool(meta.get("meter_at_start")),
            "silent": bool(meta.get("silent", silent)),
            "deliver_to_telegram": bool(meta.get("deliver_to_telegram")),
            "request_source": meta.get("source"),
            "request_summary": meta.get("summary"),
        }

    async def _deliver_habit_notification(self, job: dict[str, Any]) -> bool | None:
        runtime = self._runtime_context()
        if runtime is None or not bool(getattr(runtime, "telegram_connected", False)):
            return None
        sender = getattr(runtime, "_deliver_her_habit_notification", None)
        if not callable(sender):
            return None
        return await sender(job)

    async def _deliver_meditation_cost_tail(self, job: dict[str, Any]) -> bool | None:
        """Forward the async Meditation cost tail to the HASHI runtime.

        Only the runtime knows the frozen ``meter_at_start`` and the per-turn
        foreground receipt, so the tail is rendered and sent there.
        """
        runtime = self._runtime_context()
        if runtime is None or not bool(getattr(runtime, "telegram_connected", False)):
            return None
        sender = getattr(runtime, "_send_meditation_cost_tail", None)
        if callable(sender):
            return await sender(job)
        return None

    async def initialize(self) -> bool:
        try:
            raw = self._extra.get("her_v2")
            if not isinstance(raw, Mapping):
                raise HERv2ConfigurationError(
                    "HER v2 requires a her_v2 object containing provider profiles"
                )
            self._v2_config = HERv2Config.from_mapping(raw)
            requested_effort = (
                str(self._extra.get("effort") or raw.get("effort") or "medium")
                .strip()
                .lower()
            )
            from orchestrator.her_v2.models import parse_effort

            self.effort = parse_effort(requested_effort).value
            injected = getattr(self.config, "_her_v2_stage_provider", None)
            if injected is None and self._backend_manager() is None:
                raise HERv2ConfigurationError(
                    "HER v2 requires a HASHI backend manager for provider-role invocation"
                )
            if injected is None:
                manager = self._backend_manager()
                for profile in self._v2_config.all_provider_profiles():
                    if not _manager_authorises_profile(manager, profile):
                        raise HERv2ConfigurationError(
                            f"profile {profile.name!r} provider/model is not configured on this HASHI instance"
                        )

            state_root = Path(self.config.workspace_dir) / "backend_state" / "her_v2"
            self._ledger_store = LedgerStore(state_root / "ledgers")
            self._wip_journal = WIPJournal(state_root / "wip_journal.jsonl")
            base_logs = getattr(self.global_config, "base_logs_dir", None)
            primary_root = (
                Path(base_logs) / str(self.config.name) if base_logs else state_root
            )
            self._audit_log = DurableAuditLog(
                primary_root / "her_v2_audit.jsonl",
                state_root / "audit_fallback.jsonl",
                observer=self._observe_wip_audit,
            )
            self._audit_log.replay_fallback()
            reconciled = self._ledger_store.reconcile_interrupted()
            for ledger in reconciled:
                self._audit_log.append(
                    event_id=f"{ledger.turn_id}:restart-reconciliation",
                    turn_id=ledger.turn_id,
                    request_ref=ledger.request_ref,
                    stage="recovery",
                    role="hashi_process",
                    event="interrupted_turn_reconciled",
                    payload={
                        "terminal_state": TerminalState.ERROR.value,
                        "reason": "unexpected_process_interruption",
                        "execution_resumed": False,
                    },
                )
            self._learning = HERv2Learning(
                workspace_dir=Path(self.config.workspace_dir),
                agent_name=str(self.config.name),
                config_getter=self._habit_meditation_config,
                invoke_model=self._invoke_maintenance_model,
                audit_log=self._audit_log,
                notification_sender=self._deliver_habit_notification,
                meditation_cost_sender=self._deliver_meditation_cost_tail,
                logger=self.logger,
            )
            # Compatibility attributes are used by the existing /habit and
            # /dream HASHI command surfaces. Their owner is now HER v2.
            self._habit_execution_lock = self._learning.habit_execution_lock
            self._habit_meditation_execution_lock = (
                self._learning.meditation_execution_lock
            )
            self._habit_dream_execution_lock = self._learning.dream_execution_lock
            self._habit_dream_run_lock = self._learning.dream_run_lock
            self._habit_meditation_tasks = self._learning.meditation_tasks
            self._habit_notification_tasks = self._learning.notification_tasks
            self._meter_notification_tasks = self._learning.meter_notification_tasks
            self._habit_dream_tasks = self._learning.dream_tasks
            recovery = self._learning.recover()
            if any(
                (
                    recovery.interrupted_meditations,
                    recovery.resumed_meditations,
                    recovery.recovered_dreams,
                    recovery.resumed_notifications,
                    recovery.resumed_meter_notifications,
                )
            ):
                self.logger.info("HER v2 learning recovery: %s", recovery)
            self._initialized = True
            return True
        except (
            HERv2ConfigurationError,
            AuditPersistenceError,
            OSError,
            ValueError,
        ) as exc:
            self.logger.error("HER v2 initialization failed: %s", exc)
            self._initialized = False
            return False

    def _new_stage_provider(
        self, *, on_stream_event: StreamCallback, silent: bool
    ) -> StageProvider:
        injected = getattr(self.config, "_her_v2_stage_provider", None)
        if injected is not None:
            return injected
        return HashiStageProvider(
            backend_manager=self._backend_manager(),
            tool_registry=self.tool_registry,
            on_stream_event=on_stream_event,
            silent=silent,
            retry_policy=self._provider_retry_policy(),
            audit_log=self._audit_log,
            workzone_ref=str(self.config.workspace_dir.resolve()),
        )

    def _provider_retry_policy(self) -> ProviderRetryPolicy:
        injected = getattr(self.config, "_her_v2_retry_policy", None)
        return (
            injected
            if isinstance(injected, ProviderRetryPolicy)
            else DEFAULT_PROVIDER_RETRY_POLICY
        )

    async def _invoke_maintenance_model(
        self,
        stage: Stage,
        prompt: str,
        turn_id: str,
        request_id: str,
        timeout_s: float | None,
        json_repair_source_stage: Stage | None = None,
    ) -> StageResponse:
        # ``timeout_s`` remains in the legacy callback signature.  HER v2 does
        # not turn it into a provider-attempt or maintenance-stage deadline.
        del timeout_s
        if self._v2_config is None:
            raise StageInvocationError(
                "HER v2 is not initialized",
                retryable=False,
                code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR,
                human_description="HER v2 learning services are not initialized.",
            )
        routing_stage = (
            json_repair_source_stage or Stage.MEDITATION
            if stage is Stage.JSON_REPAIR
            else stage
        )
        profile = self._v2_config.profile_for(routing_stage)
        if routing_stage is Stage.MEDITATION and self._learning is not None:
            job_id = str(request_id).split(":attempt:", 1)[0]
            job = (
                self._learning.meditation_journal.get(job_id)
                if len(job_id) == 32
                and all(char in "0123456789abcdef" for char in job_id)
                else None
            )
            frozen = job.get("routing_target") if isinstance(job, Mapping) else None
            if isinstance(frozen, Mapping):
                profile = ProviderProfile(
                    name=profile.name,
                    engine=str(frozen.get("provider") or "").strip(),
                    model=str(frozen.get("model") or "").strip(),
                    reasoning=(
                        str(frozen.get("reasoning")).strip()
                        if frozen.get("reasoning") is not None
                        else None
                    ),
                    options=profile.options,
                )
        policy = self._provider_retry_policy()
        context = {
            "authority": "background_advisory_maintenance",
            "may_contact_user": False,
            "may_enter_live_lifecycle": False,
        }
        if stage is Stage.MEDITATION:
            context["meditation_input"] = prompt
        elif stage is Stage.JSON_REPAIR:
            context = {
                "json_repair_input": prompt,
                "source_stage": routing_stage.value,
            }
        elif stage is Stage.DREAM:
            context["dream_input"] = prompt
            try:
                dream_payload = json.loads(prompt)
            except (TypeError, ValueError, json.JSONDecodeError):
                dream_payload = {}
            context["dream_role"] = (
                "report"
                if isinstance(dream_payload, Mapping)
                and dream_payload.get("mode") == "persona_report"
                else "maintenance"
            )
        invariant_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "provider": profile.engine,
                        "model": profile.model,
                        "goal": "Agent-local background learning maintenance",
                        "classification": None,
                        "allow_tools": False,
                        "allow_side_effects": False,
                        "workzone": str(self.config.workspace_dir.resolve()),
                        "prompt_sha256": hashlib.sha256(
                            prompt.encode("utf-8")
                        ).hexdigest(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        role = (
            "json_repair_specialist"
            if stage is Stage.JSON_REPAIR
            else self._v2_config.stage_roles.get(stage, profile.name)
        )
        last_error: StageInvocationError | None = None
        for attempt in range(1, policy.max_provider_retries + 2):
            activity = ProviderActivityTracker()
            provider = self._new_stage_provider(on_stream_event=None, silent=True)
            invocation_id = f"{turn_id}:{stage.value}:maintenance:{request_id}"
            request = StageRequest(
                turn_id=turn_id,
                request_ref=f"hashi-background:{request_id}",
                stage=stage,
                role=role,
                attempt=attempt,
                goal=(
                    prompt
                    if stage is Stage.JSON_REPAIR
                    else "Agent-local background learning maintenance"
                ),
                classification=None,
                effort=Effort.LOW,
                context=copy.deepcopy(context),
                allow_tools=False,
                allow_side_effects=False,
                invocation_id=invocation_id,
                retry_invariant_hash=invariant_hash,
                provider_activity_callback=activity.record,
            )
            try:
                response = await provider.invoke(profile, request)
                return replace(response, provider_attempt=attempt)
            except asyncio.CancelledError:
                raise
            except StageInvocationError as exc:
                last_error = exc
            except Exception as exc:
                last_error = _provider_exception_error(
                    exc,
                    label=f"{stage.value} maintenance provider failed",
                )

            assert last_error is not None
            will_retry = bool(
                last_error.retryable and attempt <= policy.max_provider_retries
            )
            retry_reason = (
                "eligible"
                if will_retry
                else (
                    "failure_non_retryable"
                    if not last_error.retryable
                    else "provider_recovery_already_used"
                )
            )
            retry_delay = (
                last_error.retry_after_s
                if last_error.retry_after_s is not None
                else 0.25
            )
            self._audit_log.append(
                event_id=f"{invocation_id}:attempt:{attempt}:failed",
                turn_id=turn_id,
                request_ref=f"hashi-background:{request_id}",
                stage=stage.value,
                role=role,
                event="maintenance_provider_attempt_failed",
                provider=profile.engine,
                model=profile.model,
                attempt=attempt,
                payload={
                    **last_error.audit_payload(),
                    "provider_activity": activity.snapshot(),
                    "will_retry": will_retry,
                    "retry_reason": retry_reason,
                    "retry_delay_s": retry_delay if will_retry else None,
                    "fresh_connection_on_retry": will_retry,
                    "retry_invariant_hash": invariant_hash,
                },
            )
            if not will_retry:
                raise last_error.terminal_copy(
                    f"{stage.value} maintenance failed after {attempt} attempt(s): "
                    f"{last_error}",
                    attempts=attempt,
                ) from last_error
            self._audit_log.append(
                event_id=f"{invocation_id}:attempt:{attempt}:retry-scheduled",
                turn_id=turn_id,
                request_ref=f"hashi-background:{request_id}",
                stage=stage.value,
                role=role,
                event="maintenance_provider_retry_scheduled",
                provider=profile.engine,
                model=profile.model,
                attempt=attempt,
                payload={
                    "retry_delay_s": retry_delay,
                    "next_attempt": attempt + 1,
                    "fresh_connection": True,
                    "same_provider": True,
                    "same_model": True,
                    "same_goal": True,
                    "same_classification": True,
                    "same_permissions": True,
                    "same_workzone": True,
                    "retry_invariant_hash": invariant_hash,
                },
            )
            await asyncio.sleep(retry_delay)
        raise AssertionError("unreachable maintenance retry state")

    def _her_habit_store(self):
        if self._learning is None:
            raise RuntimeError("HER v2 learning services are not initialized")
        return self._learning.store

    def _her_meditation_journal(self):
        if self._learning is None:
            raise RuntimeError("HER v2 learning services are not initialized")
        return self._learning.meditation_journal

    def _her_dream_journal(self):
        if self._learning is None:
            raise RuntimeError("HER v2 learning services are not initialized")
        return self._learning.dream_journal

    def _record_learning_audit(
        self,
        event: str,
        *,
        identity: str | None = None,
        stage: str = "habit_command",
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        if self._audit_log is None:
            raise AuditPersistenceError("HER v2 audit log is unavailable")
        correlation = str(identity or uuid.uuid4().hex)
        turn_id = f"learning:{correlation}"
        return self._audit_log.append(
            event_id=f"{turn_id}:{stage}:{event}",
            turn_id=turn_id,
            request_ref=f"hashi-learning:{correlation}",
            stage=stage,
            role="her_v2_learning",
            event=event,
            payload=dict(payload or {}),
        )

    def _resume_pending_habit_meditations(self) -> int:
        if self._learning is None or not self._habit_meditation_config().enabled:
            return 0
        return sum(
            self._learning.spawn_meditation(job["job_id"])
            for job in self._learning.meditation_journal.pending_jobs(limit=16)
        )

    def _resume_pending_habit_notifications(self) -> int:
        if self._learning is None:
            return 0
        return (
            self._learning.resume_notifications()
            + self._learning.resume_meter_notifications()
        )

    async def _run_habit_meditation(
        self, *, job_id: str, config: HabitMeditationConfig
    ) -> None:
        if self._learning is None:
            raise RuntimeError("HER v2 learning services are not initialized")
        await self._learning._run_meditation(job_id, config)

    async def _run_habit_notification(self, job_id: str) -> None:
        if self._learning is None:
            raise RuntimeError("HER v2 learning services are not initialized")
        await self._learning._run_notification(job_id)

    async def run_habit_dream_model(
        self,
        prompt: str,
        *,
        request_id: str,
    ) -> StageResponse:
        return await self._run_habit_dream_stage(
            Stage.DREAM,
            prompt,
            request_id=request_id,
        )

    async def run_habit_dream_json_repair_model(
        self,
        prompt: str,
        *,
        request_id: str,
    ) -> StageResponse:
        """Repair rejected Dream JSON without rerunning Dream maintenance."""

        return await self._run_habit_dream_stage(
            Stage.JSON_REPAIR,
            prompt,
            request_id=request_id,
        )

    async def _run_habit_dream_stage(
        self,
        stage: Stage,
        prompt: str,
        *,
        request_id: str,
    ) -> StageResponse:
        if self._learning is None or self._audit_log is None or self._v2_config is None:
            raise RuntimeError("HER v2 Dream services are not initialized")
        turn_id = f"dream:{request_id}"
        profile = self._v2_config.profile_for(Stage.DREAM)
        role = (
            "json_repair_specialist"
            if stage is Stage.JSON_REPAIR
            else self._v2_config.stage_roles.get(Stage.DREAM, profile.name)
        )
        self._audit_log.append(
            event_id=f"{turn_id}:start",
            turn_id=turn_id,
            request_ref=f"hashi-background:{request_id}",
            stage=stage.value,
            role=role,
            event="stage_started",
            provider=profile.engine,
            model=profile.model,
            payload={"allow_tools": False, "allow_side_effects": False},
        )
        async with self._learning.dream_execution_lock:
            response = await self._invoke_maintenance_model(
                stage,
                prompt,
                turn_id,
                request_id,
                None,
                Stage.DREAM if stage is Stage.JSON_REPAIR else None,
            )
        self._audit_log.record_reasoning(
            event_id=f"{turn_id}:reasoning",
            turn_id=turn_id,
            request_ref=f"hashi-background:{request_id}",
            stage=stage.value,
            role=role,
            provider=response.provider or profile.engine,
            model=response.model or profile.model,
            attempt=response.provider_attempt,
            plan_id=None,
            trace=response.reasoning_trace,
        )
        self._audit_log.append(
            event_id=f"{turn_id}:complete",
            turn_id=turn_id,
            request_ref=f"hashi-background:{request_id}",
            stage=stage.value,
            role=role,
            event="stage_completed",
            provider=response.provider or profile.engine,
            model=response.model or profile.model,
            payload={"output": response.text},
        )
        return response

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
    ) -> BackendResponse:
        del is_retry
        started = time.perf_counter()
        if (
            not self._initialized
            or not self._v2_config
            or not self._ledger_store
            or not self._audit_log
        ):
            return BackendResponse(
                text="",
                duration_ms=0,
                error="HER v2 is not initialized",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_CONFIGURATION_ERROR.value,
                error_retryable=False,
                stream_metadata={
                    "provider_failure_description": "HER v2 is not initialized."
                },
            )
        try:
            effort_resolution = resolve_request_effort(
                self.effort,
                self._runtime_request_meta(request_id),
            )
        except ValueError as exc:
            return BackendResponse(
                text="",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=f"Invalid HER v2 request effort policy: {exc}",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_BAD_REQUEST.value,
                error_retryable=False,
                stream_metadata={
                    "provider_failure_description": (
                        "The request selected an invalid HER v2 effort policy."
                    )
                },
            )
        self.logger.info(
            "HER v2 effort resolved request=%s configured=%s effective=%s "
            "reason=%s scheduler_kind=%s scheduler_task_id=%s trigger=%s",
            request_id,
            effort_resolution.configured.value,
            effort_resolution.effective.value,
            effort_resolution.reason,
            effort_resolution.scheduler_kind or "none",
            effort_resolution.scheduler_task_id or "none",
            effort_resolution.scheduler_trigger or "none",
        )
        request_ref = f"hashi-request:{request_id}"
        prior_wip = (
            self._wip_journal.begin_turn(
                request_id=request_id,
                prompt=prompt,
            )
            if self._wip_journal is not None
            else ""
        )
        if prior_wip:
            prompt = f"{prompt}\n\n{prior_wip}"
        provider = self._new_stage_provider(
            on_stream_event=on_stream_event, silent=silent
        )
        habit_config = self._habit_meditation_config()
        habit_request_eligible = self._habit_request_eligible(request_id)
        if habit_config.enabled and not habit_request_eligible:
            self.logger.info(
                "HER v2 Habit pipeline skipped by request eligibility: request=%s",
                request_id,
            )
        runtime_config = replace(
            self._v2_config,
            # The shared /timeout command is idle-only.  Bind its live value
            # into the actual HER v2 runtime instead of leaving an unrelated
            # outer adapter setting that cannot affect execution.
            user_idle_timeout_s=float(self.IDLE_TIMEOUT_SEC),
            meditation_enabled=(
                habit_config.enabled
                and habit_request_eligible
                and not self._v2_config.shadow_mode
            ),
        )
        meditation_routing_target: dict[str, str | None] = {}
        if runtime_config.meditation_enabled:
            meditation_profile = runtime_config.profile_for(Stage.MEDITATION)
            meditation_routing_target = {
                "provider": meditation_profile.engine,
                "model": meditation_profile.model,
                "reasoning": meditation_profile.reasoning,
            }
        turn_learning = (
            self._learning.bind_turn(
                learning_eligible=habit_request_eligible,
                notification_context=self._habit_notification_context(
                    request_id, silent=silent
                ),
                routing_target=meditation_routing_target or None,
            )
            if self._learning is not None
            else None
        )
        delivery = _AdapterDelivery(
            on_stream_event,
            allow_immediate_response=(
                not silent
                # The state loader normalises HER v2's retired ``fixed`` value
                # to ``flex``. Other working modes own their final presentation,
                # so only plain Flex may publish a pre-Triage direct response.
                and str(getattr(self._backend_manager(), "agent_mode", "flex"))
                .strip()
                .lower()
                == "flex"
                # A callback must explicitly prove it can promote, replace, or
                # discard the provisional message after authoritative Triage.
                # Ordinary Telegram callbacks therefore stay on the single
                # final-response path and cannot duplicate a direct answer.
                and bool(
                    getattr(
                        on_stream_event,
                        "supports_initial_resolution",
                        False,
                    )
                )
            ),
        )
        configured_packager = None
        if isinstance(provider, HashiStageProvider):
            configured_packager = _ConfiguredPersonaPackager(
                provider=provider,
                profile=runtime_config.profile_for(Stage.IMMEDIATE_RESPONSE),
                source=her_persona.load_persona_packaging_source(
                    self.config.system_md,
                    display_name=(self._extra.get("display_name") or self.config.name),
                ),
                request_id=request_id,
                logger=self.logger,
            )

        commentary = getattr(self.config, "_her_v2_commentary_port", None)
        commentary_packager = getattr(self.config, "_her_v2_persona_packager", None)
        if commentary_packager is None:
            commentary_packager = configured_packager
        if commentary is None and commentary_packager is not None:
            commentary = PersonaCommentaryPipeline(
                packager=commentary_packager,
                delivery=delivery,
            )

        required_persona = getattr(
            self.config, "_her_v2_required_persona_renderer", None
        )
        if required_persona is None and callable(
            getattr(commentary_packager, "render", None)
        ):
            required_persona = commentary_packager
        if required_persona is None:
            required_persona = configured_packager
        execution_provider = _ExecutionStageCompactionProvider(
            provider,
            lambda: self._schedule_execution_stage_compaction(request_id),
        )
        habit_advisor = (
            turn_learning
            if not habit_request_eligible
            else (getattr(self.config, "_her_v2_habit_advisor", None) or turn_learning)
        )
        from orchestrator.fresh_context import habit_context_suppressed

        if habit_context_suppressed(self._runtime_context()):
            habit_advisor = None
        runtime = HERv2Runtime(
            config=runtime_config,
            provider=execution_provider,
            ledger_store=self._ledger_store,
            audit_log=self._audit_log,
            delivery=delivery,
            commentary=commentary,
            required_persona=required_persona,
            habits=habit_advisor,
            meditation=(
                turn_learning
                if not habit_request_eligible
                else (
                    getattr(self.config, "_her_v2_meditation_runner", None)
                    or turn_learning
                )
            ),
            dream=getattr(self.config, "_her_v2_dream_maintainer", None),
            logger=self.logger,
            retry_policy=self._provider_retry_policy(),
            workzone_ref=str(self.config.workspace_dir.resolve()),
            skills_catalogue=self._direct_skill_catalogue(),
        )
        self._wip_active_request_refs.add(request_ref)
        self._active_runtimes[request_id] = runtime
        try:
            result = await runtime.run_turn(
                prompt,
                request_id,
                effort=effort_resolution.effective,
                request_content=request_content,
            )
        finally:
            self._active_runtimes.pop(request_id, None)
            self._wip_active_request_refs.discard(request_ref)
        if (
            self._wip_journal is not None
            and str(result.ledger.get("status") or "").upper() == "COMPLETED"
        ):
            self._wip_journal.clear_completed()
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        metadata = {
            "her_v2": {
                "version": HER_V2_VERSION,
                "turn_id": result.turn_id,
                "classification": (
                    result.classification.value if result.classification else None
                ),
                "terminal_state": result.terminal_state.value,
                "plan_id": result.ledger.get("plan_id"),
                "review_count": result.review_count,
                "replan_count": result.replan_count,
                "checkpoint_count": result.checkpoint_count,
                "final_was_immediate": result.final_was_immediate,
                "final_already_delivered": result.final_already_delivered,
                "delivery": {
                    "delivery_id": result.delivery_id,
                    "kind": result.delivery_kind,
                    "event_id": result.delivery_event_id,
                },
                "evidence_refs": list(result.evidence_refs),
                "limitations": list(result.limitations),
                "shadow_mode": self._v2_config.shadow_mode,
                "effort": effort_resolution.metadata(),
            }
        }
        if result.foreground_cleanup:
            metadata["her_v2"]["foreground_cleanup"] = dict(result.foreground_cleanup)
        if result.primary_failure:
            metadata["her_v2"]["failure_chain"] = {
                "primary_failure": dict(result.primary_failure),
                "recovery_decision": (
                    dict(result.recovery_decision) if result.recovery_decision else None
                ),
                "foreground_cleanup": (
                    dict(result.foreground_cleanup)
                    if result.foreground_cleanup
                    else None
                ),
                "turn_id": result.turn_id,
            }
        if result.delivery_id:
            self._pending_delivery_receipts[result.delivery_id] = {
                "request_id": str(request_id),
                "turn_id": result.turn_id,
                "request_ref": str(result.ledger.get("request_ref") or ""),
                "kind": result.delivery_kind,
                "event_id": result.delivery_event_id,
                "text_sha256": "sha256:"
                + hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            }
            while len(self._pending_delivery_receipts) > 512:
                oldest = next(iter(self._pending_delivery_receipts))
                self._pending_delivery_receipts.pop(oldest, None)
        technical_error = result.terminal_state is TerminalState.ERROR
        stopped = result.terminal_state is TerminalState.STOPPED
        error = result.error
        terminal_error_code = str(result.primary_failure.get("code") or "")
        if error.startswith("[") and "]" in error:
            terminal_error_code = (
                terminal_error_code or error[1 : error.index("]")].strip()
            )
        if terminal_error_code:
            metadata["her_v2"]["error"] = {
                "code": terminal_error_code,
                "description": str(
                    result.primary_failure.get("description")
                    or (
                        error[error.index("]") + 1 :].strip() if "]" in error else error
                    )
                ),
            }
        if stopped and not error:
            error = "HER v2 turn was stopped by an authorised control path."
        # Expose per-stage cost line items for the /meter tail.  ``metadata``
        # is already a plain dict carried on ``stream_metadata``.
        line_items = getattr(provider, "usage_line_items", None) or []
        if line_items:
            metadata.setdefault("meter", {})["line_items"] = [
                item.to_dict() for item in line_items
            ]
        return BackendResponse(
            text=result.text,
            duration_ms=duration_ms,
            error=error or None,
            is_success=not (technical_error or stopped),
            stop_reason=result.terminal_state.value.lower(),
            usage=getattr(provider, "usage", None),
            cost_usd=(float(getattr(provider, "cost_usd", 0.0) or 0.0) or None),
            tool_call_count=int(getattr(provider, "tool_call_count", 0) or 0),
            tool_loop_count=int(getattr(provider, "tool_loop_count", 0) or 0),
            stream_metadata=metadata,
            error_code=terminal_error_code or None,
            error_retryable=False if technical_error else None,
        )

    def record_transport_delivery_receipt(
        self,
        *,
        request_id: str,
        delivery_id: str,
        delivered: bool,
        disposition: str,
        transport: str = "telegram",
        chunk_count: int = 0,
        completion_path: str = "foreground",
        error_type: str = "",
    ) -> bool:
        """Correlate the ordinary HASHI send result with the HER v2 audit trail."""

        identifier = str(delivery_id or "").strip()
        if not identifier or self._audit_log is None:
            return False
        if identifier in self._recorded_delivery_ids:
            return True
        pending = self._pending_delivery_receipts.get(identifier)
        if not isinstance(pending, Mapping):
            return False
        if str(pending.get("request_id") or "") != str(request_id or ""):
            return False
        self._audit_log.append(
            event_id=f"{identifier}:transport-receipt",
            turn_id=str(pending.get("turn_id") or ""),
            request_ref=str(pending.get("request_ref") or ""),
            stage="delivery",
            role="hashi_transport",
            event="transport_delivery_receipt",
            payload={
                "delivery_id": identifier,
                "message_event_id": str(pending.get("event_id") or ""),
                "kind": str(pending.get("kind") or ""),
                "transport": str(transport or "unknown"),
                "delivered": bool(delivered),
                "disposition": str(disposition or "unknown"),
                "chunk_count": max(0, int(chunk_count or 0)),
                "completion_path": str(completion_path or "foreground"),
                "text_sha256": str(pending.get("text_sha256") or ""),
                "error_type": str(error_type or "") or None,
            },
        )
        self._pending_delivery_receipts.pop(identifier, None)
        self._recorded_delivery_ids.add(identifier)
        if len(self._recorded_delivery_ids) > 1024:
            self._recorded_delivery_ids.clear()
            self._recorded_delivery_ids.add(identifier)
        return True

    async def shutdown(self):
        runtime_context = self._runtime_context()
        interrupt = getattr(runtime_context, "_user_interrupt", None)
        raw_reason = (
            str(interrupt.get("reason") or "") if isinstance(interrupt, Mapping) else ""
        )
        reason = {
            "user_steer": "STEERED",
            "user_focus": "STEERED",
            "user_stop": "USER_STOP",
            "user_retry": "USER_STOP",
        }.get(raw_reason, "RUNTIME_SHUTDOWN")
        active = tuple(self._active_runtimes.values())
        if active:
            await asyncio.gather(
                *(runtime.shutdown(reason=reason) for runtime in active),
                return_exceptions=True,
            )
        if self._learning is not None:
            await self._learning.shutdown()

    async def handle_new_session(self) -> bool:
        # HER v2 never revives an execution stack.  HASHI conversation context
        # naturally supplies the next newly triaged turn.
        return True
