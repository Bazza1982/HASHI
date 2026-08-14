import json
import logging
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
    validate_timeout_pair,
)
from orchestrator.backend_timeout import (
    clear_timeout_override,
    read_timeout_override,
    set_timeout_override,
)
from orchestrator.config import AgentConfig, FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_registry import (
    canonical_backend_engine,
    get_available_models,
    get_secret_lookup_order,
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
                raw = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
                return raw.get("global", {})
        except Exception:
            pass
        return {}

    def _load_state(self):
        self._active_model_override = None
        self._active_provider_override = None
        self.agent_mode = "flex"  # default mode
        self.privacy_level = PrivacyLevel.PROVIDER_TRUST
        if self.state_file.exists():
            try:
                state = self.state_store.read()
                if "active_backend" in state:
                    self.config.active_backend = canonical_backend_engine(state["active_backend"])
                if "active_model" in state:
                    self._active_model_override = state["active_model"]
                if "active_provider" in state:
                    self._active_provider_override = state["active_provider"]
                if "agent_mode" in state:
                    self.agent_mode = state["agent_mode"]
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
                        if effort is None and engine == "her":
                            effort = backend_efforts.get("her")
                        if isinstance(effort, str) and effort.strip():
                            backend_cfg["effort"] = effort.strip().lower()
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
        if getattr(self, "_active_model_override", None):
            state["active_model"] = self._active_model_override
        else:
            state.pop("active_model", None)
        if self.config.active_backend == "her" and getattr(self, "_active_provider_override", None):
            state["active_provider"] = self._active_provider_override
        else:
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
        active_provider: str | None = None,
    ):
        if active_model is not None:
            self._active_model_override = active_model
        if active_provider is not None:
            self._active_provider_override = active_provider
        # Preserve state blocks owned by newer/optional features. This method is
        # called from the runtime event loop and is expected to stay serialized.
        self._write_state_dict(self._read_state_dict())

    def persist_state(
        self,
        active_model: str | None = None,
        active_provider: str | None = None,
    ):
        self._save_state(active_model=active_model, active_provider=active_provider)

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
        if backend is None or getattr(getattr(backend, "config", None), "engine", None) != "her":
            return
        current_extra = getattr(backend.config, "extra", None) or {}
        provider = str(current_extra.get("provider") or "").strip() or None
        backend_cfg_raw = self._select_backend_cfg(
            "her",
            target_model=getattr(backend.config, "model", None),
            target_provider=provider,
        )
        if backend_cfg_raw is None:
            raise ValueError("HER backend configuration is unavailable.")
        rebuilt = self._build_adapter_config(
            "her",
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

    def _claw_provider_profiles(self) -> dict[str, dict[str, Any]]:
        raw = getattr(self.global_config, "her_providers", None) or getattr(self.global_config, "claw_providers", None) or {}
        providers = raw.get("providers") if isinstance(raw, dict) else {}
        if not isinstance(providers, dict):
            return {}
        return {
            str(name).strip(): dict(profile)
            for name, profile in providers.items()
            if str(name).strip() and isinstance(profile, dict)
        }

    @staticmethod
    def _strip_claw_provider_prefix(model: str, provider: str | None) -> str:
        value = str(model or "").strip()
        prefix = f"{provider}:" if provider else ""
        if prefix and value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix):]
        return value

    @staticmethod
    def _claw_entry_model_values(entry: dict[str, Any]) -> list[Any]:
        raw_models = entry.get("models")
        values = raw_models if isinstance(raw_models, list) else []
        if entry.get("model"):
            values = [*values, entry.get("model")]
        if entry.get("default_model"):
            values = [*values, entry.get("default_model")]
        return values

    def _claw_entry_models(self, entry: dict[str, Any], provider: str | None) -> list[str]:
        models: list[str] = []
        for value in self._claw_entry_model_values(entry):
            route_provider, route_model = self._normalize_claw_route(str(value), None)
            if route_provider and provider and route_provider != provider:
                continue
            models.append(
                str(
                    route_model
                    if route_provider
                    else self._strip_claw_provider_prefix(str(value), provider)
                )
            )
        return self._unique_strings(models)

    def get_claw_provider_options(self) -> list[dict[str, Any]]:
        """Return provider/model choices allowed for this agent.

        Global provider profiles own connection details. Agent backend rows own
        authorization and may narrow each provider to one model (legacy
        ``model``) or a provider-specific ``models`` list.
        """
        profiles = self._claw_provider_profiles()
        entries = [
            entry
            for entry in self.config.allowed_backends
            if entry.get("engine") == "her"
        ]
        explicit_names = self._unique_strings(entry.get("provider") for entry in entries)
        generic_entries = [entry for entry in entries if not str(entry.get("provider") or "").strip()]
        names = list(explicit_names)
        if generic_entries:
            for name in profiles:
                if name not in names:
                    names.append(name)

        options: list[dict[str, Any]] = []
        for name in names:
            profile = profiles.get(name)
            matching = [entry for entry in entries if str(entry.get("provider") or "").strip() == name]
            models: list[str] = []
            for entry in matching:
                models.extend(self._claw_entry_models(entry, name))
            models = self._unique_strings(models)

            if not models and profile:
                profile_models = profile.get("models") if isinstance(profile.get("models"), list) else []
                profile_values = [*profile_models, profile.get("default_model")]
                models = []
                for value in profile_values:
                    route_provider, route_model = self._normalize_claw_route(str(value or ""), None)
                    if route_provider and route_provider != name:
                        continue
                    models.append(str(route_model or value or ""))
                models = self._unique_strings(models)
            if not models and generic_entries:
                for entry in generic_entries:
                    for value in self._claw_entry_model_values(entry):
                        route_provider, route_model = self._normalize_claw_route(str(value), None)
                        if route_provider:
                            if route_provider == name:
                                models.append(str(route_model or ""))
                            continue
                        # Legacy provider-less Claw rows historically meant
                        # OpenRouter. If only one profile exists, that sole
                        # provider is unambiguous and receives the bare model.
                        if name == "openrouter" or len(profiles) == 1:
                            models.append(str(value or ""))
                models = self._unique_strings(models)
            if not models and generic_entries and name == "openrouter":
                models = get_available_models("her")

            status = str((profile or {}).get("status") or "stable").strip().lower()
            reason = None
            if profile is None:
                reason = "provider profile is not configured"
            elif status == "disabled":
                reason = "provider is disabled"
            elif not models:
                reason = "no models are allowed for this agent"
            options.append(
                {
                    "name": name,
                    "status": status,
                    "models": models,
                    "available": reason is None,
                    "reason": reason,
                }
            )
        return options

    def get_claw_models(self, provider: str | None) -> list[str]:
        name = str(provider or "").strip()
        for option in self.get_claw_provider_options():
            if option["name"] == name and option["available"]:
                return list(option["models"])
        return []

    def get_active_provider(self) -> str | None:
        if self.config.active_backend != "her":
            return None
        backend = self.current_backend
        if backend is not None:
            extra = getattr(getattr(backend, "config", None), "extra", None) or {}
            provider = str(extra.get("provider") or "").strip()
            if provider:
                return provider
        provider = str(getattr(self, "_active_provider_override", None) or "").strip()
        if provider:
            return provider
        model = str(getattr(self, "_active_model_override", None) or "").strip()
        selected = self._select_backend_cfg("her", target_model=model)
        if selected:
            provider = str(selected.get("provider") or "").strip()
            if provider:
                return provider
        for option in self.get_claw_provider_options():
            if option["available"]:
                return str(option["name"])
        return None

    def _normalize_claw_route(
        self,
        target_model: str | None,
        target_provider: str | None,
    ) -> tuple[str | None, str | None]:
        provider = str(target_provider or "").strip() or None
        model = str(target_model or "").strip() or None
        if not model or ":" not in model:
            return provider, model

        prefix, bare_model = model.split(":", 1)
        known_providers = set(self._claw_provider_profiles())
        known_providers.update(
            str(entry.get("provider") or "").strip()
            for entry in self.config.allowed_backends
            if entry.get("engine") == "her" and entry.get("provider")
        )
        if prefix not in known_providers or not bare_model:
            return provider, model
        if provider and provider != prefix:
            raise ValueError(
                f"HER provider mismatch: requested {provider!r} but model prefix selects {prefix!r}."
            )
        return prefix, bare_model

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
        habit_override = self.get_habit_meditation_override()
        if engine == "her" and habit_override is not None:
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
        if engine == "her":
            provider, resolved_model = self._normalize_claw_route(resolved_model, target_provider)
            provider = provider or str(backend_cfg_raw.get("provider") or "").strip() or None
            if not resolved_model and provider:
                profile = self._claw_provider_profiles().get(provider) or {}
                resolved_model = profile.get("default_model")
                if not resolved_model and isinstance(profile.get("models"), list):
                    resolved_model = next(iter(profile["models"]), None)
                provider, resolved_model = self._normalize_claw_route(
                    resolved_model,
                    provider,
                )
            if provider:
                extra["provider"] = provider
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
        current_extra = getattr(backend.config, "extra", None) or {}
        current_provider = (
            str(current_extra.get("provider") or "").strip() or None
            if engine == "her"
            else None
        )
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
        hard_seconds: int | None = None,
    ) -> TimeoutPolicySnapshot:
        policy = self.get_active_timeout_policy()
        validate_timeout_pair(
            idle_seconds,
            hard_seconds if hard_seconds is not None else policy.hard_seconds,
        )
        set_timeout_override(
            self.state_store,
            policy.engine,
            idle_seconds=idle_seconds,
            hard_seconds=hard_seconds,
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

        Multiple HER rows (e.g. OpenRouter vs xAI) share the same engine name;
        first-match alone would always bind the wrong provider.
        """
        engine = canonical_backend_engine(engine)
        candidates = [b for b in self.config.allowed_backends if b.get("engine") == engine]
        if not candidates:
            return None
        model = str(target_model or "").strip()
        provider = str(target_provider or "").strip() or None
        if engine == "her":
            provider, normalized_model = self._normalize_claw_route(model, provider)
            model = str(normalized_model or "").strip()
            if provider:
                provider_candidates = [
                    backend
                    for backend in candidates
                    if str(backend.get("provider") or "").strip() == provider
                ]
                generic_candidates = [backend for backend in candidates if not backend.get("provider")]
                candidates = provider_candidates or generic_candidates
                if not candidates:
                    return None
        if not model:
            return candidates[0]

        for backend in candidates:
            backend_provider = provider or str(backend.get("provider") or "").strip() or None
            if engine == "her":
                matches = model in self._claw_entry_models(backend, backend_provider)
            else:
                matches = str(backend.get("model") or "").strip() == model
            if matches:
                return backend

        if engine == "her" and ":" in model:
            provider_name, bare_model = model.split(":", 1)
            provider_name = provider_name.strip()
            bare_model = bare_model.strip()
            for backend in candidates:
                if str(backend.get("provider") or "").strip() != provider_name:
                    continue
                if not bare_model or str(backend.get("model") or "").strip() == bare_model:
                    return backend
                return backend

        # Grok models on HER should prefer the HASHI xAI OAuth provider when present.
        lowered = model.lower()
        if lowered.startswith("grok") or lowered.startswith("xai/"):
            for backend in candidates:
                if str(backend.get("provider") or "").strip() == "xai":
                    return backend

        return candidates[0]

    def create_ephemeral_backend(self, engine: str, target_model: str | None = None):
        engine = canonical_backend_engine(engine)
        backend_cfg_raw = self._select_backend_cfg(engine, target_model=target_model)
        if not backend_cfg_raw:
            raise ValueError(f"Backend {engine} not allowed for {self.config.name}.")

        adapter_cfg = self._build_adapter_config(
            engine,
            backend_cfg_raw,
            target_model=target_model,
            apply_persisted_timeout=False,
        )
        if engine == "her":
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
        if engine == "her" and resolved_provider is None:
            resolved_provider = getattr(self, "_active_provider_override", None)
        if engine == "her":
            resolved_provider, resolved_model = self._normalize_claw_route(
                resolved_model,
                resolved_provider,
            )
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
            if engine in ("openrouter-api", "deepseek-api", "ollama-api", "xai-api", "her"):
                tools_cfg = self._resolve_tools_config(backend_cfg_raw)
                if engine == "her" and not tools_cfg:
                    tools_cfg = {"allowed": ["*"], "max_loops": 25}
                if tools_cfg:
                    self._attach_tool_registry(tools_cfg, adapter_cfg)

            return await self.current_backend.initialize()
        except Exception as e:
            self.logger.error(f"Failed to initialize backend {engine}: {e}")
            return False

    def _resolve_tools_config(self, backend_cfg_raw: dict) -> dict | None:
        """Merge global default_tools with per-backend tools config.

        Priority: per-backend 'allowed' list extends (not replaces) global defaults.
        Per-backend max_loops and tool_options override global ones.
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
            merged_allowed = ["*"]
        else:
            merged_allowed = list(global_allowed | backend_allowed)

        if not merged_allowed:
            return None

        # Backend-specific settings override global
        merged = dict(global_tools)
        merged.update(backend_tools)
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
            max_loops = int(tools_cfg.get("max_loops", 25))

            # Per-tool options (e.g. bash.timeout_max, file_write.max_file_size_kb)
            tool_options = {k: v for k, v in tools_cfg.items()
                            if k not in ("allowed", "max_loops")}

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
                max_loops=max_loops,
                audit_context={
                    "agent_name": getattr(adapter_cfg, "name", workspace_dir.name),
                    "workspace_dir": str(workspace_dir),
                    "safety_mode": "read_write",
                    "global_config": self.global_config,
                    "_runtime": getattr(self, "runtime", None),
                },
            )
            self.current_backend.tool_registry = registry
            self.logger.info(
                f"ToolRegistry attached: allowed={allowed}, max_loops={max_loops}"
            )
        except Exception as e:
            self.logger.error(f"Failed to attach ToolRegistry: {e}")

    async def switch_backend(
        self,
        target_engine: str,
        target_model: str | None = None,
        target_provider: str | None = None,
    ) -> bool:
        target_engine = canonical_backend_engine(target_engine)
        resolved_provider = target_provider
        resolved_model = target_model
        if target_engine == "her":
            try:
                resolved_provider, resolved_model = self._normalize_claw_route(
                    target_model,
                    target_provider,
                )
            except ValueError as exc:
                self.logger.error("Invalid HER route: %s", exc)
                return False
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
        if target_engine == "her":
            resolved_provider = (
                resolved_provider
                or str(backend_cfg_raw.get("provider") or "").strip()
                or None
            )
            if not resolved_model:
                models = self.get_claw_models(resolved_provider)
                resolved_model = next(iter(models), None)
            if not resolved_provider or not resolved_model:
                self.logger.error("HER backend requires an explicit provider and model.")
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
        previous_extra = getattr(previous_backend_config, "extra", None) or {}
        previous_provider = (
            getattr(self, "_active_provider_override", None)
            or (
                str(previous_extra.get("provider") or "").strip()
                if isinstance(previous_extra, dict)
                else None
            )
        )

        # Cleanly shut down current backend
        if self.current_backend:
            await self.shutdown()

        # Update config and state
        self.config.active_backend = target_engine
        self._active_model_override = resolved_model
        self._active_provider_override = resolved_provider if target_engine == "her" else None
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
            self._active_provider_override = previous_provider
            self._save_state()
            if not await self.initialize_active_backend(
                target_model=previous_model,
                target_provider=previous_provider,
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
    ):
        if not self.current_backend:
            raise RuntimeError("No active backend initialized.")
        self._refresh_tool_runtime_context(request_id)
        return await self.current_backend.generate_response(
            prompt,
            request_id,
            is_retry=is_retry,
            silent=silent,
            on_stream_event=on_stream_event,
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
