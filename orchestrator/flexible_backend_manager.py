import inspect
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from adapters.timeout_policy import (
    HARD_TIMEOUT_KEY,
    IDLE_TIMEOUT_KEY,
    LEGACY_TIMEOUT_KEY,
    TIMEOUT_POLICY_META_KEY,
    TimeoutPolicySnapshot,
    apply_timeout_layers,
    timeout_policy_snapshot,
)
from orchestrator.backend_timeout import (
    clear_timeout_override,
    read_timeout_override,
    set_timeout_override,
)
from orchestrator.config import AgentConfig, FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_registry import (
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_secret_lookup_order,
)
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.runtime_configuration import (
    HER_V2_CONFIGURATION_DRAFT_STATE_KEY,
    HER_V2_CONFIGURATION_PRESETS_STATE_KEY,
    HER_V2_CONFIGURATION_STATE_KEY,
    HERv2RuntimeConfiguration,
    ProviderModelTarget,
    apply_her_v2_runtime_configuration,
    build_her_v2_provider_options,
    resolve_her_v2_configuration,
    select_her_v2_hybrid,
    select_her_v2_provider,
    set_her_v2_route_model_slot,
    set_her_v2_route_reasoning,
    set_her_v2_route_target,
    set_her_v2_slot_model,
)
from orchestrator.privacy_levels import (
    PrivacyLevel,
    PrivacyPolicyError,
    parse_privacy_level,
    require_backend_compatibility,
    require_level_available,
)
from orchestrator.workspace_state import WorkspaceStateStore
from orchestrator.workzone import access_root_for_workzone

HER_HABIT_MEDITATION_STATE_KEY = "her_habit_meditation"


class FlexibleBackendManager:
    def __init__(self, config: FlexibleAgentConfig, global_config: GlobalConfig, secrets: dict):
        self.config = config
        self.global_config = global_config
        self.secrets = secrets
        self.logger = logging.getLogger(f"BackendMgr.{config.name}")
        self.current_backend = None
        self.runtime = None
        self.state_file = self.config.workspace_dir / "state.json"
        self.state_store = WorkspaceStateStore(self.config.workspace_dir)
        self._agents_json_global = self._load_agents_json_global()
        self._load_state()

    def _load_agents_json_global(self) -> dict:
        """Load the 'global' section from agents.json for default_tools etc."""
        try:
            cfg_path = getattr(self.global_config, 'config_path', None)
            if cfg_path and Path(cfg_path).exists():
                # ConfigManager accepts the UTF-8 BOM used by the live
                # agents.json. Keep this secondary read consistent; otherwise
                # global default_tools silently disappear after agent startup.
                raw = json.loads(Path(cfg_path).read_text(encoding="utf-8-sig"))
                return raw.get("global", {})
        except Exception:
            pass
        return {}

    def _load_state(self):
        self._active_model_override = None
        self._her_v2_configuration_override: dict[str, Any] | None = None
        self._her_v2_configuration_draft: dict[str, Any] | None = None
        self.agent_mode = self.config.default_mode
        self.privacy_level = PrivacyLevel.PROVIDER_TRUST
        configured_backend = self.config.active_backend
        allowed_engines = {
            canonical_backend_engine(backend_cfg.get("engine"))
            for backend_cfg in self.config.allowed_backends
            if backend_cfg.get("engine")
        }
        if self.state_file.exists():
            try:
                state = self.state_store.read()
                restore_backend_overrides = True
                state_needs_repair = False
                if "active_backend" in state:
                    persisted_backend = canonical_backend_engine(state["active_backend"])
                    if persisted_backend in allowed_engines:
                        self.config.active_backend = persisted_backend
                        if state.get("active_backend") != persisted_backend:
                            state["active_backend"] = persisted_backend
                            state_needs_repair = True
                    else:
                        restore_backend_overrides = False
                        self.logger.warning(
                            "Ignoring persisted active backend %r because it is not "
                            "present in allowed_backends; using configured backend %r.",
                            state.get("active_backend"),
                            configured_backend,
                        )
                        state["active_backend"] = configured_backend
                        state_needs_repair = True
                        if "active_model" in state or "active_provider" in state:
                            state.pop("active_model", None)
                            state.pop("active_provider", None)
                persisted_her_v2 = state.get(HER_V2_CONFIGURATION_STATE_KEY)
                if isinstance(persisted_her_v2, dict):
                    self._her_v2_configuration_override = dict(persisted_her_v2)
                persisted_her_v2_draft = state.get(
                    HER_V2_CONFIGURATION_DRAFT_STATE_KEY
                )
                if isinstance(persisted_her_v2_draft, dict):
                    self._her_v2_configuration_draft = dict(
                        persisted_her_v2_draft
                    )
                if (
                    restore_backend_overrides
                    and self.config.active_backend != HER_V2_ENGINE
                    and "active_model" in state
                ):
                    self._active_model_override = state["active_model"]
                if restore_backend_overrides and self.config.active_backend == HER_V2_ENGINE:
                    legacy_provider = str(state.get("active_provider") or "").strip()
                    legacy_model = str(state.get("active_model") or "").strip()
                    if self._her_v2_configuration_override is None and legacy_provider:
                        try:
                            migrated = self.prepare_her_v2_provider(legacy_provider)
                            option = self._her_v2_provider_option(migrated.provider)
                            if (
                                legacy_model
                                and legacy_model != "role-configured"
                                and option
                                and legacy_model in option["models"]
                            ):
                                migrated = set_her_v2_slot_model(
                                    migrated,
                                    "fast",
                                    legacy_model,
                                    allowed_models=option["models"],
                                )
                                migrated = set_her_v2_slot_model(
                                    migrated,
                                    "pro",
                                    legacy_model,
                                    allowed_models=option["models"],
                                )
                            self._her_v2_configuration_override = migrated.to_dict()
                            state[HER_V2_CONFIGURATION_STATE_KEY] = migrated.to_dict()
                        except (TypeError, ValueError) as exc:
                            self.logger.warning(
                                "Ignoring legacy HER provider/model state during v2 migration: %s",
                                exc,
                            )
                    if "active_model" in state or "active_provider" in state:
                        state.pop("active_model", None)
                        state.pop("active_provider", None)
                        state_needs_repair = True
                if "agent_mode" in state:
                    persisted_mode = state["agent_mode"]
                    if (
                        self.config.active_backend == HER_V2_ENGINE
                        and persisted_mode == "fixed"
                    ):
                        # HER v2 has always been stateless.  Older ``her`` state
                        # could retain its former CLI session mode when the
                        # backend ID was upgraded, so repair only that legacy
                        # combination at the same boundary that upgrades the ID.
                        self.agent_mode = "flex"
                        state["agent_mode"] = "flex"
                        state_needs_repair = True
                        self.logger.warning(
                            "Migrated stale HER v2 agent_mode=fixed to flex."
                        )
                    else:
                        self.agent_mode = persisted_mode
                if "privacy_level" in state:
                    try:
                        self.privacy_level = parse_privacy_level(state["privacy_level"])
                    except PrivacyPolicyError as exc:
                        self.logger.error(
                            "Ignoring invalid privacy_level in state.json: %s",
                            exc,
                        )
                backend_efforts = state.get("backend_efforts")
                if isinstance(backend_efforts, dict):
                    for backend_cfg in self.config.allowed_backends:
                        engine = backend_cfg.get("engine")
                        effort = backend_efforts.get(engine)
                        if effort is None and engine == HER_V2_ENGINE:
                            effort = backend_efforts.get("her")
                        if isinstance(effort, str) and effort.strip():
                            backend_cfg["effort"] = effort.strip().lower()
                if state_needs_repair:
                    self.state_store.replace(state)
            except Exception as e:
                self.logger.error(f"Failed to load state.json: {e}")

    def _read_state_dict(self) -> dict[str, Any]:
        try:
            return self.state_store.read()
        except Exception as e:
            self.logger.error(f"Failed to read state.json: {e}")
        return {}

    def _apply_managed_state_fields(self, state: dict[str, Any]) -> None:
        state["active_backend"] = self.config.active_backend
        state["agent_mode"] = self.agent_mode
        state["privacy_level"] = int(self.privacy_level)
        if (
            self.config.active_backend != HER_V2_ENGINE
            and getattr(self, "_active_model_override", None)
        ):
            state["active_model"] = self._active_model_override
        else:
            state.pop("active_model", None)
        state.pop("active_provider", None)
        backend_efforts = {
            backend_cfg["engine"]: backend_cfg["effort"]
            for backend_cfg in self.config.allowed_backends
            if backend_cfg.get("engine") and backend_cfg.get("effort")
        }
        if backend_efforts:
            state["backend_efforts"] = backend_efforts
        else:
            state.pop("backend_efforts", None)

    def _write_state_dict(self, state: dict[str, Any]) -> None:
        self._apply_managed_state_fields(state)
        try:
            self.state_store.replace(state)
        except Exception as e:
            self.logger.error(f"Failed to save state.json: {e}")

    def _save_state(
        self,
        active_model: str | None = None,
    ):
        if active_model is not None:
            self._active_model_override = active_model
        # Preserve state blocks owned by newer/optional features. This method is
        # called from the runtime event loop and is expected to stay serialized.
        self._write_state_dict(self._read_state_dict())

    def persist_state(
        self,
        active_model: str | None = None,
    ):
        self._save_state(active_model=active_model)

    def set_privacy_level(self, level: int | str | PrivacyLevel) -> PrivacyLevel:
        parsed = require_level_available(level)
        require_backend_compatibility(self.config.active_backend, parsed)
        self.privacy_level = parsed
        if self.current_backend is not None:
            self.current_backend.privacy_level = parsed
        self._save_state()
        return parsed

    def get_state_snapshot(self) -> dict[str, Any]:
        state = self._read_state_dict()
        self._apply_managed_state_fields(state)
        return state

    def get_habit_meditation_override(self) -> bool | None:
        raw = self._read_state_dict().get(HER_HABIT_MEDITATION_STATE_KEY)
        if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
            return None
        return bool(raw["enabled"])

    def set_habit_meditation_override(self, enabled: bool | None) -> None:
        """Persist an agent-local HER Habit switch and refresh the live adapter."""

        state = self._read_state_dict()
        if enabled is None:
            state.pop(HER_HABIT_MEDITATION_STATE_KEY, None)
        else:
            state[HER_HABIT_MEDITATION_STATE_KEY] = {
                "enabled": bool(enabled),
            }
        self._write_state_dict(state)
        self._refresh_current_habit_meditation_config()

    def _refresh_current_habit_meditation_config(self) -> None:
        backend = self.current_backend
        engine = str(
            getattr(getattr(backend, "config", None), "engine", "") or ""
        )
        if backend is None or engine != "her-v2":
            return
        provider = None
        backend_cfg_raw = self._select_backend_cfg(
            engine,
            target_model=getattr(backend.config, "model", None),
            target_provider=provider,
        )
        if backend_cfg_raw is None:
            raise ValueError(f"{engine} backend configuration is unavailable.")
        rebuilt = self._build_adapter_config(
            engine,
            backend_cfg_raw,
            target_model=getattr(backend.config, "model", None),
            target_provider=provider,
        )
        if backend.config.extra is None:
            backend.config.extra = {}
        for key in (
            "habit_meditation",
            "habit_meditation_enabled",
            "habit_learning_eligible",
        ):
            if key in rebuilt.extra:
                backend.config.extra[key] = rebuilt.extra[key]
            else:
                backend.config.extra.pop(key, None)

    def update_wrapper_blocks(
        self,
        *,
        core: dict[str, Any] | None = None,
        wrapper: dict[str, Any] | None = None,
        wrapper_slots: dict[str, Any] | None = None,
    ) -> None:
        state = self._read_state_dict()
        if core is not None:
            state["core"] = dict(core)
        if wrapper is not None:
            state["wrapper"] = dict(wrapper)
        if wrapper_slots is not None:
            state["wrapper_slots"] = dict(wrapper_slots)
        self._write_state_dict(state)

    def update_audit_blocks(
        self,
        *,
        core: dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
        audit_criteria: dict[str, Any] | None = None,
    ) -> None:
        state = self._read_state_dict()
        if core is not None:
            state["core"] = dict(core)
        if audit is not None:
            state["audit"] = dict(audit)
        if audit_criteria is not None:
            state["audit_criteria"] = dict(audit_criteria)
        self._write_state_dict(state)

    def update_dual_brain_block(self, dual_brain: dict[str, Any]) -> None:
        state = self._read_state_dict()
        state["dual_brain"] = dict(dual_brain)
        self._write_state_dict(state)

    def _timeout_override_for(self, engine: str) -> dict[str, int]:
        try:
            return read_timeout_override(self.state_store, engine)
        except ValueError as exc:
            self.logger.error(
                "Ignoring invalid persisted timeout override for %s: %s",
                engine,
                exc,
            )
            return {}

    @staticmethod
    def _unique_strings(values) -> list[str]:
        result: list[str] = []
        for value in values or []:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def _her_v2_backend_config(self) -> dict[str, Any] | None:
        return next(
            (
                backend
                for backend in self.config.allowed_backends
                if canonical_backend_engine(backend.get("engine")) == HER_V2_ENGINE
            ),
            None,
        )

    def _her_v2_base_config(self) -> dict[str, Any]:
        backend = self._her_v2_backend_config() or {}
        raw = backend.get("her_v2")
        if not isinstance(raw, dict):
            raise ValueError("HER v2 backend has no her_v2 provider configuration")
        return raw

    def get_her_v2_provider_options(self) -> list[dict[str, Any]]:
        options = build_her_v2_provider_options(
            self.config.allowed_backends,
            self._her_provider_profiles(),
            self._her_v2_base_config(),
        )
        for option in options:
            if not option.get("available"):
                continue
            try:
                require_level_available(self.privacy_level)
                require_backend_compatibility(
                    str(option.get("engine") or ""),
                    self.privacy_level,
                )
            except PrivacyPolicyError as exc:
                option["available"] = False
                option["reason"] = str(exc)
        return options

    def _her_v2_provider_option(self, requested: str) -> dict[str, Any] | None:
        value = str(requested or "").strip().casefold()
        return next(
            (
                option
                for option in self.get_her_v2_provider_options()
                if value
                in {
                    str(option.get("name") or "").casefold(),
                    str(option.get("engine") or "").casefold(),
                    str(option.get("label") or "").casefold(),
                }
            ),
            None,
        )

    def get_her_v2_configuration(self) -> HERv2RuntimeConfiguration:
        try:
            selected = resolve_her_v2_configuration(
                self._her_v2_base_config(),
                self._her_v2_configuration_override,
            )
            selected = self._normalise_her_v2_target_providers(selected)
            self._validate_her_v2_selection(selected)
            return selected
        except (TypeError, ValueError) as exc:
            if self._her_v2_configuration_override is None:
                raise
            self.logger.warning(
                "Ignoring invalid persisted HER v2 runtime configuration: %s",
                exc,
            )
            self._her_v2_configuration_override = None
            fallback = resolve_her_v2_configuration(self._her_v2_base_config())
            fallback = self._normalise_her_v2_target_providers(fallback)
            self._validate_her_v2_selection(fallback)
            return fallback

    def get_her_v2_draft_configuration(
        self,
    ) -> HERv2RuntimeConfiguration | None:
        raw = self._her_v2_configuration_draft
        if not isinstance(raw, dict):
            return None
        try:
            selected = resolve_her_v2_configuration(
                self._her_v2_base_config(),
                raw,
            )
            selected = self._normalise_her_v2_target_providers(selected)
            self._validate_her_v2_selection(selected)
            return selected
        except (TypeError, ValueError) as exc:
            self.logger.warning(
                "Ignoring invalid persisted HER v2 configuration draft: %s",
                exc,
            )
            self._her_v2_configuration_draft = None
            return None

    def get_her_v2_edit_configuration(self) -> HERv2RuntimeConfiguration:
        return self.get_her_v2_draft_configuration() or self.get_her_v2_configuration()

    def has_her_v2_configuration_draft(self) -> bool:
        return self.get_her_v2_draft_configuration() is not None

    def _normalise_her_v2_target_providers(
        self,
        selected: HERv2RuntimeConfiguration,
    ) -> HERv2RuntimeConfiguration:
        def normalize(target: ProviderModelTarget) -> ProviderModelTarget:
            option = self._her_v2_provider_option(target.provider)
            if option is None:
                return target
            return ProviderModelTarget(str(option["engine"]), target.model)

        fast = normalize(selected.target_for_slot("fast"))
        pro = normalize(selected.target_for_slot("pro"))
        routes = {
            route: normalize(target)
            for route, target in selected.route_targets.items()
        }
        return replace(
            selected,
            fast_provider=fast.provider,
            fast_model=fast.model,
            pro_provider=pro.provider,
            pro_model=pro.model,
            route_targets=routes,
        )

    def _validate_her_v2_selection(
        self,
        selected: HERv2RuntimeConfiguration,
    ) -> None:
        require_level_available(self.privacy_level)
        for target in selected.all_targets():
            require_backend_compatibility(target.provider, self.privacy_level)
            option = self._her_v2_provider_option(target.provider)
            if option is None or not option.get("available"):
                raise ValueError(
                    f"HER v2 provider {target.provider!r} is not configured on this instance"
                )
            if target.model not in option["models"]:
                raise ValueError(
                    f"model {target.model!r} is not allowed by configured provider {target.provider!r}"
                )

    def prepare_her_v2_provider(
        self,
        provider: str,
        *,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        option = self._her_v2_provider_option(provider)
        if option is None:
            raise ValueError(f"unknown HER v2 provider: {provider}")
        selected = current or self.get_her_v2_configuration()
        if (
            selected.routing_mode == "single"
            and option.get("engine") == selected.provider
        ):
            option = dict(option)
            models = option.get("models") or []
            if selected.fast_model in models:
                option["fast_model"] = selected.fast_model
            if selected.pro_model in models:
                option["pro_model"] = selected.pro_model
        return select_her_v2_provider(
            selected,
            option,
        )

    def prepare_her_v2_hybrid(
        self,
        *,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        return select_her_v2_hybrid(current or self.get_her_v2_configuration())

    def prepare_her_v2_model(
        self,
        slot: str,
        model: str,
        *,
        provider: str | None = None,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        selected = current or self.get_her_v2_configuration()
        requested_provider = (
            provider if provider else selected.target_for_slot(slot).provider
        )
        option = self._her_v2_provider_option(requested_provider)
        if option is None or not option.get("available"):
            raise ValueError(
                f"HER v2 provider {requested_provider!r} is not configured on this instance"
            )
        selected_provider = str(option["engine"])
        return set_her_v2_slot_model(
            selected,
            slot,
            model,
            provider=selected_provider,
            allowed_models=option["models"],
        )

    def prepare_her_v2_route_model_slot(
        self,
        route: str,
        slot: str,
        *,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        return set_her_v2_route_model_slot(
            current or self.get_her_v2_configuration(),
            route,
            slot,
        )

    def prepare_her_v2_route_reasoning(
        self,
        route: str,
        reasoning: str | None,
        *,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        return set_her_v2_route_reasoning(
            current or self.get_her_v2_configuration(),
            route,
            reasoning,
        )

    def prepare_her_v2_route_target(
        self,
        route: str,
        provider: str,
        model: str,
        *,
        current: HERv2RuntimeConfiguration | None = None,
    ) -> HERv2RuntimeConfiguration:
        option = self._her_v2_provider_option(provider)
        if option is None or not option.get("available"):
            raise ValueError(
                f"HER v2 provider {provider!r} is not configured on this instance"
            )
        selected_provider = str(option["engine"])
        return set_her_v2_route_target(
            current or self.get_her_v2_configuration(),
            route,
            provider=selected_provider,
            model=model,
            allowed_models=option["models"],
        )

    def _effective_her_v2_config(
        self,
        selected: HERv2RuntimeConfiguration | None = None,
    ) -> dict[str, Any]:
        return apply_her_v2_runtime_configuration(
            self._her_v2_base_config(),
            selected or self.get_her_v2_configuration(),
        )

    def apply_her_v2_configuration(
        self,
        selected: HERv2RuntimeConfiguration,
    ) -> None:
        if self.config.active_backend != HER_V2_ENGINE:
            raise ValueError("HER v2 configuration is available only while HER v2 is active")
        selected = self._normalise_her_v2_target_providers(selected)
        self._validate_her_v2_selection(selected)

        effective = self._effective_her_v2_config(selected)
        parsed = HERv2Config.from_mapping(effective)
        serialized = selected.to_dict()
        current = self.get_her_v2_configuration()
        draft = self.get_her_v2_draft_configuration()

        def update_state(state: dict[str, Any]) -> dict[str, Any]:
            state[HER_V2_CONFIGURATION_STATE_KEY] = serialized
            presets = state.get(HER_V2_CONFIGURATION_PRESETS_STATE_KEY)
            presets = dict(presets) if isinstance(presets, dict) else {}
            presets[current.routing_mode] = current.to_dict()
            if draft is not None:
                presets[draft.routing_mode] = draft.to_dict()
            presets[selected.routing_mode] = serialized
            state[HER_V2_CONFIGURATION_PRESETS_STATE_KEY] = presets
            state.pop(HER_V2_CONFIGURATION_DRAFT_STATE_KEY, None)
            self._apply_managed_state_fields(state)
            state.pop("active_model", None)
            state.pop("active_provider", None)
            return state

        try:
            self.state_store.update(update_state)
        except Exception as exc:
            raise OSError(f"failed to persist HER v2 configuration: {exc}") from exc
        self._active_model_override = None
        self._her_v2_configuration_override = serialized
        self._her_v2_configuration_draft = None

        self._refresh_live_her_v2_configuration(effective, parsed)

    def _refresh_live_her_v2_configuration(
        self,
        effective: dict[str, Any],
        parsed: HERv2Config,
    ) -> None:
        """Refresh only future turns; each in-flight runtime owns its snapshot."""

        backend = self.current_backend
        backend_engine = str(
            getattr(getattr(backend, "config", None), "engine", "") or ""
        )
        if backend is not None and backend_engine == HER_V2_ENGINE:
            extra = dict(getattr(backend.config, "extra", None) or {})
            extra["her_v2"] = effective
            backend.config.extra = extra
            if hasattr(backend, "_v2_config"):
                backend._v2_config = parsed

    def stage_her_v2_configuration(
        self,
        selected: HERv2RuntimeConfiguration,
    ) -> HERv2RuntimeConfiguration:
        if self.config.active_backend != HER_V2_ENGINE:
            raise ValueError(
                "HER v2 configuration is available only while HER v2 is active"
            )
        selected = self._normalise_her_v2_target_providers(selected)
        self._validate_her_v2_selection(selected)
        HERv2Config.from_mapping(self._effective_her_v2_config(selected))
        serialized = selected.to_dict()

        def update_state(state: dict[str, Any]) -> dict[str, Any]:
            state[HER_V2_CONFIGURATION_DRAFT_STATE_KEY] = serialized
            self._apply_managed_state_fields(state)
            return state

        try:
            self.state_store.update(update_state)
        except Exception as exc:
            raise OSError(f"failed to persist HER v2 configuration draft: {exc}") from exc
        self._her_v2_configuration_draft = serialized
        return selected

    def begin_her_v2_hybrid_draft(self) -> HERv2RuntimeConfiguration:
        if self.config.active_backend != HER_V2_ENGINE:
            raise ValueError(
                "HER v2 configuration is available only while HER v2 is active"
            )
        existing = self.get_her_v2_draft_configuration()
        if existing is not None and existing.routing_mode == "hybrid":
            return existing
        current = self.get_her_v2_configuration()
        state = self._read_state_dict()
        presets = state.get(HER_V2_CONFIGURATION_PRESETS_STATE_KEY)
        preset = presets.get("hybrid") if isinstance(presets, dict) else None
        candidate: HERv2RuntimeConfiguration | None = None
        if isinstance(preset, dict):
            try:
                candidate = resolve_her_v2_configuration(
                    self._her_v2_base_config(),
                    preset,
                )
                candidate = self._normalise_her_v2_target_providers(candidate)
                candidate = select_her_v2_hybrid(candidate)
                self._validate_her_v2_selection(candidate)
            except (TypeError, ValueError) as exc:
                self.logger.warning(
                    "Ignoring invalid saved HER v2 Hybrid preset: %s",
                    exc,
                )
        if candidate is None:
            candidate = select_her_v2_hybrid(current)

        serialized = candidate.to_dict()

        def update_state(state: dict[str, Any]) -> dict[str, Any]:
            saved = state.get(HER_V2_CONFIGURATION_PRESETS_STATE_KEY)
            saved = dict(saved) if isinstance(saved, dict) else {}
            saved[current.routing_mode] = current.to_dict()
            state[HER_V2_CONFIGURATION_PRESETS_STATE_KEY] = saved
            state[HER_V2_CONFIGURATION_DRAFT_STATE_KEY] = serialized
            self._apply_managed_state_fields(state)
            return state

        try:
            self.state_store.update(update_state)
        except Exception as exc:
            raise OSError(f"failed to start HER v2 Hybrid draft: {exc}") from exc
        self._her_v2_configuration_draft = serialized
        return candidate

    def discard_her_v2_configuration_draft(self) -> None:
        def update_state(state: dict[str, Any]) -> dict[str, Any]:
            state.pop(HER_V2_CONFIGURATION_DRAFT_STATE_KEY, None)
            self._apply_managed_state_fields(state)
            return state

        try:
            self.state_store.update(update_state)
        except Exception as exc:
            raise OSError(f"failed to discard HER v2 configuration draft: {exc}") from exc
        self._her_v2_configuration_draft = None

    def apply_her_v2_configuration_draft(self) -> HERv2RuntimeConfiguration:
        selected = self.get_her_v2_draft_configuration()
        if selected is None:
            raise ValueError("there is no HER v2 configuration draft to apply")
        self.apply_her_v2_configuration(selected)
        return selected

    def _her_provider_profiles(self) -> dict[str, dict[str, Any]]:
        raw = getattr(self.global_config, "her_providers", None) or {}
        providers = raw.get("providers") if isinstance(raw, dict) else {}
        if not isinstance(providers, dict):
            return {}
        return {
            str(name).strip(): dict(profile)
            for name, profile in providers.items()
            if str(name).strip() and isinstance(profile, dict)
        }

    def _build_adapter_config(
        self,
        engine: str,
        backend_cfg_raw: dict[str, Any],
        *,
        target_model: str | None = None,
        target_provider: str | None = None,
        apply_persisted_timeout: bool = True,
    ) -> AgentConfig:
        agent_extra = dict(getattr(self.config, "extra", None) or {})
        backend_extra = dict(backend_cfg_raw)
        backend_extra.pop("engine", None)
        backend_extra.pop("model", None)
        backend_extra.pop("models", None)
        backend_extra.pop("default_model", None)
        backend_scope = backend_cfg_raw.get("access_scope", self.config.access_scope)
        backend_extra.pop("access_scope", None)
        extra = {**agent_extra, **backend_extra}
        if engine == HER_V2_ENGINE:
            raw_her_v2 = extra.get("her_v2")
            if isinstance(raw_her_v2, dict):
                try:
                    selected = self.get_her_v2_configuration()
                    extra["her_v2"] = apply_her_v2_runtime_configuration(
                        raw_her_v2,
                        selected,
                    )
                except (TypeError, ValueError) as exc:
                    self.logger.warning(
                        "Ignoring invalid HER v2 runtime override while building adapter config: %s",
                        exc,
                    )
        habit_override = self.get_habit_meditation_override()
        if engine == "her-v2" and habit_override is not None:
            extra["habit_meditation_enabled"] = habit_override
        extra = apply_timeout_layers(
            extra,
            engine=engine,
            agent_extra=agent_extra,
            backend_extra=backend_extra,
            persisted_override=(
                self._timeout_override_for(engine)
                if apply_persisted_timeout
                else {}
            ),
        )
        resolved_model = target_model or backend_cfg_raw.get("default_model") or backend_cfg_raw.get("model")
        if not resolved_model and isinstance(backend_cfg_raw.get("models"), list):
            resolved_model = next(iter(backend_cfg_raw["models"]), None)
        return AgentConfig(
            name=self.config.name,
            engine=engine,
            workspace_dir=self.config.workspace_dir,
            system_md=self.config.system_md,
            model=resolved_model or "default",
            is_active=True,
            extra=extra,
            access_scope=backend_scope,
            project_root=self.config.project_root,
        )

    def _refresh_current_backend_timeout_config(self, engine: str) -> None:
        backend = self.current_backend
        if backend is None or getattr(getattr(backend, "config", None), "engine", None) != engine:
            return
        current_provider = None
        backend_cfg_raw = self._select_backend_cfg(
            engine,
            target_model=getattr(backend.config, "model", None),
            target_provider=current_provider,
        )
        if backend_cfg_raw is None:
            raise ValueError(f"Backend {engine} is not allowed for {self.config.name}.")
        rebuilt = self._build_adapter_config(
            engine,
            backend_cfg_raw,
            target_model=getattr(backend.config, "model", None),
            target_provider=current_provider,
        )
        if backend.config.extra is None:
            backend.config.extra = {}
        for key in (IDLE_TIMEOUT_KEY, HARD_TIMEOUT_KEY, LEGACY_TIMEOUT_KEY, TIMEOUT_POLICY_META_KEY):
            if key in rebuilt.extra:
                backend.config.extra[key] = rebuilt.extra[key]
            else:
                backend.config.extra.pop(key, None)
        backend._validate_timeout_configuration()

    def get_active_timeout_policy(self) -> TimeoutPolicySnapshot:
        if self.current_backend is None:
            raise RuntimeError("No active backend is initialized.")
        return timeout_policy_snapshot(self.current_backend)

    def set_active_timeout_override(
        self,
        *,
        idle_seconds: int,
    ) -> TimeoutPolicySnapshot:
        policy = self.get_active_timeout_policy()
        set_timeout_override(
            self.state_store,
            policy.engine,
            idle_seconds=idle_seconds,
        )
        self._save_state()
        self._refresh_current_backend_timeout_config(policy.engine)
        return self.get_active_timeout_policy()

    def clear_active_timeout_override(self) -> TimeoutPolicySnapshot:
        policy = self.get_active_timeout_policy()
        clear_timeout_override(self.state_store, policy.engine)
        self._save_state()
        self._refresh_current_backend_timeout_config(policy.engine)
        return self.get_active_timeout_policy()

    def _attach_runtime_context(self, adapter_cfg: AgentConfig) -> None:
        setattr(adapter_cfg, "_hashi_secrets", self.secrets)
        setattr(adapter_cfg, "_hashi_runtime", self.runtime)

    def _select_backend_cfg(
        self,
        engine: str,
        target_model: str | None = None,
        target_provider: str | None = None,
    ) -> dict | None:
        """Pick allowed backend entry for engine, preferring model/provider match.

        Provider-specific HER v2 routing is resolved by its dedicated runtime
        configuration before this generic backend selector is called.
        """
        engine = canonical_backend_engine(engine)
        candidates = [
            backend
            for backend in self.config.allowed_backends
            if canonical_backend_engine(backend.get("engine")) == engine
        ]
        if not candidates:
            return None
        model = str(target_model or "").strip()
        if not model:
            return candidates[0]

        for backend in candidates:
            matches = str(backend.get("model") or "").strip() == model
            if matches:
                return backend

        return candidates[0]

    def _instance_provider_backend_cfg(
        self,
        engine: str,
        *,
        target_model: str | None,
    ) -> dict[str, Any] | None:
        """Build an internal call config from an instance-level HER provider."""

        option = self._her_v2_provider_option(engine)
        if option is None or not option.get("available"):
            return None
        models = list(option.get("models") or [])
        if target_model and target_model not in models:
            return None
        profile = next(
            (
                raw
                for name, raw in self._her_provider_profiles().items()
                if canonical_backend_engine(raw.get("engine") or f"{name}-api")
                == canonical_backend_engine(engine)
            ),
            {},
        )
        result = dict(profile)
        result.update(
            {
                "engine": canonical_backend_engine(engine),
                "models": models,
                "default_model": (
                    target_model
                    or option.get("fast_model")
                    or option.get("pro_model")
                ),
            }
        )
        return result

    def create_ephemeral_backend(self, engine: str, target_model: str | None = None):
        engine = canonical_backend_engine(engine)
        require_level_available(self.privacy_level)
        require_backend_compatibility(engine, self.privacy_level)
        backend_cfg_raw = self._select_backend_cfg(engine, target_model=target_model)
        if not backend_cfg_raw:
            backend_cfg_raw = self._instance_provider_backend_cfg(
                engine,
                target_model=target_model,
            )
        if not backend_cfg_raw:
            raise ValueError(
                f"Backend {engine} is not configured for {self.config.name}."
            )

        adapter_cfg = self._build_adapter_config(
            engine,
            backend_cfg_raw,
            target_model=target_model,
            apply_persisted_timeout=False,
        )
        if engine == "her-v2":
            # One-shot internal sidecars are not agent runs. Keep globally
            # enabled Habit planning/Meditation out of health probes, wrapper
            # helpers, and other ephemeral HER invocations.
            extra = dict(adapter_cfg.extra or {})
            habit_config = extra.get("habit_meditation")
            habit_config = (
                dict(habit_config) if isinstance(habit_config, dict) else {}
            )
            habit_config["enabled"] = False
            extra["habit_meditation"] = habit_config
            extra["habit_meditation_enabled"] = False
            extra["habit_learning_eligible"] = False
            extra["ephemeral_session"] = True
            adapter_cfg.extra = extra
        self._attach_runtime_context(adapter_cfg)
        from adapters.registry import get_backend_class

        BackendClass = get_backend_class(engine)
        api_key = self._resolve_api_key(engine)
        return BackendClass(adapter_cfg, self.global_config, api_key)

    async def generate_ephemeral_response(
        self,
        *,
        engine: str,
        model: str,
        prompt: str,
        request_id: str,
        silent: bool = True,
    ):
        backend = self.create_ephemeral_backend(engine, target_model=model)
        try:
            initialized = await backend.initialize()
            if not initialized:
                raise RuntimeError(f"Failed to initialize ephemeral backend {engine}.")
            return await backend.generate_response(
                prompt,
                request_id,
                is_retry=False,
                silent=silent,
                on_stream_event=None,
            )
        finally:
            await backend.shutdown()

    async def generate_tool_free_ephemeral_response(
        self,
        *,
        engine: str,
        model: str,
        prompt: str,
        request_id: str,
        silent: bool = True,
    ):
        """Run a one-shot API render with no HASHI tool registry attached."""

        engine = canonical_backend_engine(engine)
        if engine not in {
            "deepseek-api",
            "hashi-api",
            "ollama-api",
            "openrouter-api",
            "xai-api",
        }:
            raise ValueError(f"Backend {engine} is not a tool-free API renderer.")
        require_level_available(self.privacy_level)
        require_backend_compatibility(engine, self.privacy_level)
        backend = self.create_ephemeral_backend(engine, target_model=model)
        backend.privacy_level = self.privacy_level
        if hasattr(backend, "tool_registry"):
            backend.tool_registry = None
        try:
            initialized = await backend.initialize()
            if not initialized:
                raise RuntimeError(f"Failed to initialize ephemeral backend {engine}.")
            if hasattr(backend, "tool_registry"):
                backend.tool_registry = None
            return await backend.generate_response(
                prompt,
                request_id,
                is_retry=False,
                silent=silent,
                on_stream_event=None,
            )
        finally:
            await backend.shutdown()

    def _resolve_xai_api_credentials(self) -> dict[str, str] | None:
        api_key = None
        oauth_refresh = None
        for secret_key in get_secret_lookup_order("xai-api", self.config.name):
            value = str(self.secrets.get(secret_key) or "").strip()
            if not value:
                continue
            if secret_key == "xai_oauth_refresh_token":
                oauth_refresh = value
            elif secret_key in ("xai_api_key", "XAI_API_KEY") and not api_key:
                api_key = value
        if api_key or oauth_refresh:
            return {
                "api_key": api_key or "",
                "oauth_refresh_token": oauth_refresh or "",
            }
        return None

    def _resolve_api_key(self, engine: str) -> Optional[Any]:
        if engine == "xai-api":
            creds = self._resolve_xai_api_credentials()
            if creds:
                self.logger.info("Resolved xAI API credentials from secrets.json")
                return creds
            return None
        for secret_key in get_secret_lookup_order(engine, self.config.name):
            api_key = self.secrets.get(secret_key)
            if api_key:
                self.logger.info(f"Resolved API key for {engine} via '{secret_key}'")
                return api_key
        return None

    async def initialize_active_backend(
        self,
        target_model: str | None = None,
        target_provider: str | None = None,
    ) -> bool:
        engine = self.config.active_backend
        self.logger.info(f"Initializing active backend: {engine}")
        try:
            require_level_available(self.privacy_level)
            require_backend_compatibility(engine, self.privacy_level)
        except PrivacyPolicyError as exc:
            self.logger.error("Backend blocked by privacy policy: %s", exc)
            return False

        resolved_model = target_model or getattr(self, "_active_model_override", None)
        resolved_provider = target_provider
        backend_cfg_raw = self._select_backend_cfg(
            engine,
            target_model=resolved_model,
            target_provider=resolved_provider,
        )
        if not backend_cfg_raw:
            self.logger.error(f"Active backend {engine} not found in allowed_backends.")
            return False

        adapter_cfg = self._build_adapter_config(
            engine,
            backend_cfg_raw,
            target_model=resolved_model,
            target_provider=resolved_provider,
        )

        try:
            from adapters.registry import get_backend_class
            BackendClass = get_backend_class(engine)
            api_key = self._resolve_api_key(engine)
            self._attach_runtime_context(adapter_cfg)

            self.current_backend = BackendClass(adapter_cfg, self.global_config, api_key)
            self.current_backend.privacy_level = self.privacy_level

            # V2.2+: inject the canonical ToolRegistry into tool-capable backends.
            # API adapters consume it directly; HER exposes it through the
            # protocol-neutral HASHI Tool Gateway over MCP stdio.
            if engine in (
                "openrouter-api",
                "deepseek-api",
                "hashi-api",
                "ollama-api",
                "xai-api",
                "her-v2",
            ):
                tools_cfg = self._resolve_tools_config(backend_cfg_raw)
                if tools_cfg:
                    self._attach_tool_registry(tools_cfg, adapter_cfg)

            return await self.current_backend.initialize()
        except Exception as e:
            self.logger.error(f"Failed to initialize backend {engine}: {e}")
            return False

    def _resolve_tools_config(self, backend_cfg_raw: dict) -> dict | None:
        """Merge global default_tools with per-backend tools config.

        Priority: per-backend 'allowed' list extends (not replaces) global defaults.
        Per-backend tool options override global ones. Historical ``max_loops``
        values are discarded because active execution has no tool-round cap.
        """
        global_raw = getattr(self, '_agents_json_global', None) or {}
        global_tools = global_raw.get("default_tools", {})
        backend_tools = backend_cfg_raw.get("tools", {})

        if not global_tools and not backend_tools:
            return None

        # Merge allowed lists (union, global first). A backend-level wildcard must
        # survive the merge with global defaults; otherwise ["*"] + defaults would
        # become ["*", "telegram_send_file"] and fail ToolRegistry's wildcard check.
        global_allowed = set(global_tools.get("allowed", []))
        backend_allowed = set(backend_tools.get("allowed", []))
        if "*" in global_allowed or "*" in backend_allowed:
            # Wildcards remain fail-closed for media tools inside ToolRegistry.
            # Preserve only explicit media opt-ins alongside the wildcard so
            # configured vision cannot silently disappear during this merge.
            explicit_media = sorted(
                (global_allowed | backend_allowed)
                & {"media_read", "vision_inspect"}
            )
            merged_allowed = ["*", *explicit_media]
        else:
            merged_allowed = list(global_allowed | backend_allowed)

        configured_mode = str(backend_cfg_raw.get("image_input") or "").strip().casefold()
        if not configured_mode:
            configured_mode = "tool" if "vision_inspect" in merged_allowed else "none"
        if configured_mode not in {"none", "native", "tool"}:
            raise ValueError("image_input must be one of: none, native, tool")
        # Native support is preferred per attachment at invocation time.  Keep
        # explicitly configured media fallbacks available for other modalities
        # and for one typed capability-drift recovery.

        if not merged_allowed:
            return None

        # Backend-specific settings override global
        merged = dict(global_tools)
        merged.update(backend_tools)
        if "max_loops" in merged:
            self.logger.warning(
                "Ignoring removed tools.max_loops execution ceiling for %s",
                backend_cfg_raw.get("engine") or "backend",
            )
            merged.pop("max_loops", None)
        merged["allowed"] = merged_allowed
        return merged

    def _attach_tool_registry(self, tools_cfg: dict, adapter_cfg) -> None:
        """Create and attach the canonical ToolRegistry to a tool-capable backend."""
        try:
            from tools.registry import ToolRegistry

            allowed = tools_cfg.get("allowed", [])
            if not allowed:
                return

            workzone_dir = (adapter_cfg.extra or {}).get("workzone_dir")
            workspace_dir = Path(workzone_dir).expanduser().resolve() if workzone_dir else adapter_cfg.workspace_dir
            access_root = access_root_for_workzone(adapter_cfg.resolve_access_root(), workspace_dir if workzone_dir else None)
            # Per-tool options (e.g. bash.timeout_max, file_write.max_file_size_kb)
            tool_options = {k: v for k, v in tools_cfg.items()
                            if k != "allowed"}

            # Inject agent token and authorized_id for telegram_send_file tool
            enriched_secrets = dict(self.secrets)
            agent_token = self.secrets.get(self.config.telegram_token_key)
            if agent_token:
                enriched_secrets["_agent_telegram_token"] = agent_token
            if self.global_config and self.global_config.authorized_id:
                enriched_secrets["_authorized_telegram_id"] = str(self.global_config.authorized_id)

            registry = ToolRegistry(
                allowed_tools=allowed,
                access_root=access_root,
                workspace_dir=workspace_dir,
                secrets=enriched_secrets,
                tool_options=tool_options,
                max_loops=None,
                audit_context={
                    "agent_name": getattr(adapter_cfg, "name", workspace_dir.name),
                    "workspace_dir": str(workspace_dir),
                    "safety_mode": "read_write",
                    "global_config": self.global_config,
                    "_runtime": getattr(self, "runtime", None),
                },
                media_roots=self._vision_media_roots(adapter_cfg),
            )
            self.current_backend.tool_registry = registry
            self.logger.info(
                "ToolRegistry attached with unbounded tool rounds: allowed=%s",
                allowed,
            )
        except Exception as e:
            self.logger.error(f"Failed to attach ToolRegistry: {e}")

    def _vision_media_roots(self, adapter_cfg) -> list[Path]:
        """Return agent media and remote attachment roots without broadening file tools."""
        roots: list[Path] = []
        base_media_dir = getattr(self.global_config, "base_media_dir", None)
        if base_media_dir:
            roots.append(Path(base_media_dir) / str(adapter_cfg.name))
        project_root = (
            getattr(self.global_config, "project_root", None)
            or getattr(self.config, "project_root", None)
        )
        instance_id = str(
            getattr(self.global_config, "instance_id", None) or "hashi"
        ).strip().lower()
        if project_root:
            roots.append(Path(project_root) / "state" / "remote_attachments" / instance_id)
        return roots

    async def switch_backend(
        self,
        target_engine: str,
        target_model: str | None = None,
        target_provider: str | None = None,
    ) -> bool:
        target_engine = canonical_backend_engine(target_engine)
        resolved_provider = target_provider
        resolved_model = target_model
        self.logger.info(
            f"Switching backend to {target_engine}"
            + (f" provider={resolved_provider}" if resolved_provider else "")
            + (f" model={resolved_model}" if resolved_model else "")
        )
        backend_cfg_raw = self._select_backend_cfg(
            target_engine,
            target_model=resolved_model,
            target_provider=resolved_provider,
        )
        if not backend_cfg_raw:
            self.logger.error(f"Target backend {target_engine} not allowed.")
            return False
        try:
            require_backend_compatibility(target_engine, self.privacy_level)
        except PrivacyPolicyError as exc:
            self.logger.error("Target backend blocked by privacy policy: %s", exc)
            return False

        previous_engine = self.config.active_backend
        previous_backend_config = getattr(self.current_backend, "config", None)
        previous_model = (
            getattr(self, "_active_model_override", None)
            or getattr(previous_backend_config, "model", None)
        )
        # Cleanly shut down current backend
        if self.current_backend:
            await self.shutdown()

        # Update config and state
        self.config.active_backend = target_engine
        self._active_model_override = resolved_model
        self._save_state()

        # Initialize target backend — rollback on failure
        if not await self.initialize_active_backend(
            target_model=resolved_model,
            target_provider=resolved_provider,
        ):
            self.logger.error(
                f"Failed to initialize {target_engine}; rolling back to {previous_engine}"
            )
            self.config.active_backend = previous_engine
            self._active_model_override = previous_model
            self._save_state()
            if not await self.initialize_active_backend(
                target_model=previous_model,
            ):
                self.logger.critical(
                    f"Rollback to {previous_engine} also failed. Agent has no active backend."
                )
            return False
        return True

    async def shutdown(self):
        if self.current_backend:
            await self.current_backend.shutdown()
            self.current_backend = None

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event=None,
        request_content: dict[str, Any] | None = None,
    ):
        if not self.current_backend:
            raise RuntimeError("No active backend initialized.")
        self._refresh_tool_runtime_context(request_id)
        kwargs = {
            "is_retry": is_retry,
            "silent": silent,
            "on_stream_event": on_stream_event,
        }
        if request_content is not None:
            from adapters.base import BackendResponse
            from orchestrator.multimodal_contract import (
                MultimodalContractError,
                route_request_content,
                validate_authorized_media_references,
            )

            roots_resolver = getattr(
                self.current_backend,
                "authorized_media_roots",
                None,
            )
            try:
                if callable(roots_resolver):
                    validate_authorized_media_references(
                        request_content,
                        authorized_roots=roots_resolver(),
                    )
            except MultimodalContractError as exc:
                return BackendResponse(
                    text="",
                    duration_ms=0,
                    error=str(exc),
                    is_success=False,
                    error_code=exc.code,
                    error_retryable=False,
                    stream_metadata={"attachment_id": exc.attachment_id or None},
                )

            parameters = inspect.signature(
                self.current_backend.generate_response
            ).parameters
            accepts_structured_content = "request_content" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if accepts_structured_content:
                kwargs["request_content"] = request_content
            else:
                capability_resolver = getattr(
                    self.current_backend,
                    "resolve_input_capability",
                    None,
                )
                try:
                    capability = (
                        capability_resolver()
                        if callable(capability_resolver)
                        else None
                    )
                    if capability is not None:
                        decisions = route_request_content(
                            request_content,
                            capability,
                        )
                    else:
                        decisions = ()
                except MultimodalContractError as exc:
                    return BackendResponse(
                        text="",
                        duration_ms=0,
                        error=str(exc),
                        is_success=False,
                        error_code=exc.code,
                        error_retryable=False,
                        stream_metadata={
                            "attachment_id": exc.attachment_id or None
                        },
                    )
                if capability is not None:
                    native = [item for item in decisions if item.route == "native"]
                    if native:
                        first = native[0]
                        return BackendResponse(
                            text="",
                            duration_ms=0,
                            error=(
                                "Backend declared native media capability but its "
                                "generate_response boundary cannot accept canonical "
                                f"content for attachment {first.attachment_id!r}"
                            ),
                            is_success=False,
                            error_code="MEDIA_TRANSPORT_UNSUPPORTED",
                            error_retryable=False,
                            stream_metadata={
                                "attachment_id": first.attachment_id,
                                "multimodal_routing": [
                                    item.as_dict() for item in decisions
                                ],
                            },
                        )
        return await self.current_backend.generate_response(
            prompt,
            request_id,
            **kwargs,
        )

    def _refresh_tool_runtime_context(self, request_id: str) -> None:
        registry = getattr(self.current_backend, "tool_registry", None)
        if registry is None:
            return
        context = dict(getattr(registry, "audit_context", {}) or {})
        runtime = getattr(self, "runtime", None)
        meta_registry = getattr(runtime, "_request_meta_by_id", None)
        request_meta = (
            dict(meta_registry.get(request_id) or {})
            if isinstance(meta_registry, dict)
            else {}
        )
        if not request_meta:
            current_meta = dict(getattr(runtime, "current_request_meta", None) or {})
            if str(current_meta.get("request_id") or "") == str(request_id or ""):
                request_meta = current_meta
        context.update(
            {
                "_runtime": runtime,
                "agent_name": getattr(runtime, "name", context.get("agent_name")),
                "request_id": request_meta.get("request_id") or request_id,
                "chat_id": request_meta.get("chat_id"),
                "request_source": request_meta.get("source"),
                "request_summary": request_meta.get("summary"),
            }
        )
        registry.audit_context = context
