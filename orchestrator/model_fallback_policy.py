"""Authoritative user-consented fallback policy for ordinary model requests.

Functional owner: PAO (the cross-backend fallback decision).  Engineering
layer: Functions plus the existing user-scoped configuration persistence
boundary.  HERV3 and Codex CLI expose typed capabilities and consume a
resolved decision; the Frontend Connector renders and edits this one policy.

The default is ``off``: a configured API key, installed executable, available
model, or catalogue entry proves capability only -- never consent to spend
money or send data to another destination.

This module owns the semantic policy, its typed routes, the ordered
resolution gates, the additional-paid-call budget, the interactive
confirmation binding, and the storage schema.  It deliberately does not own
cross-Provider compatibility facts (``orchestrator.flexible_backend_registry``),
privacy levels (``orchestrator.privacy_levels``), engine selection (PAO), or
rendering (Frontend Connector).

Fallback differs from same-target recovery and whole-Turn replay:

* same-target local recovery opens another physical request to the already
  selected Provider/model under the existing bounded recovery budget;
* fallback selects a different user-authorised Provider route, model, or
  Engine after an eligible failure;
* whole-Turn replay starts the user's work again and stays forbidden after
  possible effects.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from hashlib import blake2s
from pathlib import Path
from typing import Any, Mapping

from orchestrator.config_json import ConfigDocument, read_config_json, write_config_json
from orchestrator.privacy_levels import PrivacyLevel

POLICY_VERSION = 1
STATE_FILENAME = "model_fallback_policy.json"

MODE_OFF = "off"
MODE_ASK = "ask"
MODE_AUTO = "auto"
FALLBACK_MODES = (MODE_OFF, MODE_ASK, MODE_AUTO)

SOURCE_HERV3 = "herv3"
SOURCE_CODEX_CLI = "codex-cli"
FALLBACK_SOURCES = (SOURCE_HERV3, SOURCE_CODEX_CLI)

TARGET_KIND_HERV3_MODEL = "herv3_model"
TARGET_KIND_CODEX_CLI = "codex_cli"
FALLBACK_TARGET_KINDS = (TARGET_KIND_HERV3_MODEL, TARGET_KIND_CODEX_CLI)

# Hard request-scope exclusions evaluated by the policy evaluator itself, so
# aliases, scheduler entry, Frontend Connector entry, and future renderers
# cannot bypass them by hiding UI choices or matching command text.
EXCLUDED_MODALITIES = frozenset({"voice", "phone", "call"})

# Stable, language-neutral blocking reasons.  Rendering layers map these to
# locale entries; the policy never hard-codes user-facing prose.
REASON_DISABLED = "disabled"
REASON_CONFIRMATION_REQUIRED = "confirmation_required"
REASON_PRIVACY_BLOCKED = "privacy_blocked"
REASON_NO_ALLOWED_TARGET = "no_allowed_target"
REASON_MISSING_KEY_OR_RUNTIME = "missing_key_or_runtime"
REASON_INCOMPATIBLE_CAPABILITY = "incompatible_capability"
REASON_UNSAFE_CHECKPOINT = "unsafe_checkpoint"
REASON_MODALITY_EXCLUDED = "modality_excluded"
REASON_UNKNOWN_COST = "unknown_cost"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_SOURCE_DISABLED = "source_disabled"
REASON_INELIGIBLE_FAILURE = "ineligible_failure"

DISPOSITION_DISABLED = "disabled"
DISPOSITION_ASK_REQUIRED = "ask-required"
DISPOSITION_BLOCKED = "blocked"
DISPOSITION_UNAVAILABLE = "unavailable"
DISPOSITION_READY = "ready"

_POLICY_LOCK = threading.RLock()


def _actor_key(value: Any) -> str:
    """Map Session owner IDs and Telegram actor IDs to one preference key.

    Kept identical to the shared user-scoped preference convention: a
    ``user:`` prefix is stripped so the same human resolves to one policy.
    """

    raw = str(value) if value is not None else ""
    raw = raw.strip()
    if raw.casefold().startswith("user:"):
        session_actor = raw.split(":", 1)[1].strip()
        if session_actor:
            return session_actor
    return raw or "default"


def _bridge_home(runtime: Any) -> Path:
    global_config = getattr(runtime, "global_config", None)
    configured = getattr(global_config, "bridge_home", None)
    if configured:
        return Path(configured)
    project_root = getattr(global_config, "project_root", None)
    if project_root:
        return Path(project_root)
    workspace = getattr(runtime, "workspace_dir", None)
    if workspace:
        path = Path(workspace)
        return path.parent.parent if len(path.parents) >= 2 else path.parent
    return Path(".")


def policy_path(runtime: Any) -> Path:
    return _bridge_home(runtime) / "state" / STATE_FILENAME


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return int(value)


def _privacy_name(level: int | PrivacyLevel) -> str:
    try:
        parsed = PrivacyLevel(int(level))
    except (TypeError, ValueError):
        return "unknown"
    return parsed.name.casefold()


@dataclass(frozen=True)
class FallbackRoute:
    """One exact, typed fallback route choice.

    ``herv3_model`` routes name a concrete Provider and one exact model the
    user explicitly allowed.  When ``same_model`` is set, the authoritative
    compatibility registry must prove that the target serves the same
    canonical model as the failing source; string similarity never does.
    ``codex_cli`` routes name the Codex CLI engine itself.
    """

    source: str
    target_kind: str
    provider: str = ""
    model: str = ""
    same_model: bool = False

    def __post_init__(self) -> None:
        source = str(self.source or "").strip().casefold()
        target_kind = str(self.target_kind or "").strip().casefold()
        provider = str(self.provider or "").strip()
        model = str(self.model or "").strip()
        if source not in FALLBACK_SOURCES:
            raise ValueError(f"unknown fallback source: {self.source!r}")
        if target_kind not in FALLBACK_TARGET_KINDS:
            raise ValueError(f"unknown fallback target kind: {self.target_kind!r}")
        if target_kind == TARGET_KIND_HERV3_MODEL:
            if not provider or provider.casefold() == "her-v2":
                raise ValueError("a HERV3 fallback route requires a concrete Provider")
            if not model:
                raise ValueError("a HERV3 fallback route requires one exact model")
            provider = provider.casefold()
        else:
            provider = provider or "codex-cli"
            if provider.casefold() != "codex-cli":
                raise ValueError("a Codex CLI fallback route must target codex-cli")
            provider = "codex-cli"
            model = model or ""
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target_kind", target_kind)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "same_model", bool(self.same_model))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FallbackRoute":
        if not isinstance(raw, Mapping):
            raise ValueError("fallback route must be an object")
        return cls(
            source=str(raw.get("source") or ""),
            target_kind=str(raw.get("target_kind") or ""),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            same_model=bool(raw.get("same_model", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target_kind": self.target_kind,
            "provider": self.provider,
            "model": self.model,
            "same_model": self.same_model,
        }

    def key(self) -> str:
        return f"{self.source}|{self.target_kind}|{self.provider}|{self.model}"


@dataclass(frozen=True)
class FallbackSourceRule:
    """Independent permission for one fallback source.

    Enabling Codex CLI fallback must not enable HERV3 fallback (and vice
    versa); source and target are both recorded, so cross-engine direction is
    explicit in storage, resolution, and UI.
    """

    enabled: bool = True
    allow_herv3_targets: bool = True
    allow_codex_cli_targets: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "FallbackSourceRule":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("fallback source rule must be an object")
        return cls(
            enabled=_strict_bool(raw.get("enabled", True), "source rule enabled"),
            allow_herv3_targets=_strict_bool(
                raw.get("allow_herv3_targets", True),
                "source rule allow_herv3_targets",
            ),
            allow_codex_cli_targets=_strict_bool(
                raw.get("allow_codex_cli_targets", True),
                "source rule allow_codex_cli_targets",
            ),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "enabled": self.enabled,
            "allow_herv3_targets": self.allow_herv3_targets,
            "allow_codex_cli_targets": self.allow_codex_cli_targets,
        }


@dataclass(frozen=True)
class FallbackRouteFacts:
    """Consumer-supplied capability facts for one candidate route.

    ``credential_ready`` covers configured credential/runtime availability;
    ``capability_ok`` covers model, Tool, context, and message-contract
    compatibility.  Consumers build them from the authoritative owners
    (instance opt-ins, backend manager, capability facts); the policy never
    copies model catalogues.
    """

    credential_ready: bool = False
    capability_ok: bool = False
    capability_reason: str = REASON_INCOMPATIBLE_CAPABILITY


@dataclass(frozen=True)
class FallbackConfirmation:
    """One-time confirmation bound to the exact failed request."""

    action_id: str
    request_id: str
    route_key: str
    privacy_level: int
    budget_used_at_issue: int
    issued_at: float
    expires_at: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FallbackConfirmation":
        if not isinstance(raw, Mapping):
            raise ValueError("fallback confirmation must be an object")
        return cls(
            action_id=str(raw.get("action_id") or ""),
            request_id=str(raw.get("request_id") or ""),
            route_key=str(raw.get("route_key") or ""),
            privacy_level=int(raw.get("privacy_level") or 0),
            budget_used_at_issue=int(raw.get("budget_used_at_issue") or 0),
            issued_at=float(raw.get("issued_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "request_id": self.request_id,
            "route_key": self.route_key,
            "privacy_level": self.privacy_level,
            "budget_used_at_issue": self.budget_used_at_issue,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class FallbackDecision:
    """The resolved effective decision recorded for diagnostics and delivery."""

    allowed: bool
    mode: str
    source: str
    route: FallbackRoute | None
    blocking_reason: str | None
    disposition: str
    additional_calls_limit: int
    additional_calls_used: int
    allow_unknown_cost: bool
    cost_ceiling_usd: float | None
    confirmation: FallbackConfirmation | None = None
    tool_activity: bool = False
    visible_partial_answer: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "source": self.source,
            "route": self.route.to_dict() if self.route is not None else None,
            "route_key": self.route.key() if self.route is not None else None,
            "blocking_reason": self.blocking_reason,
            "disposition": self.disposition,
            "additional_calls_limit": self.additional_calls_limit,
            "additional_calls_used": self.additional_calls_used,
            "allow_unknown_cost": self.allow_unknown_cost,
            "cost_ceiling_usd": self.cost_ceiling_usd,
            "tool_activity": self.tool_activity,
            "visible_partial_answer": self.visible_partial_answer,
            "confirmation": (
                self.confirmation.to_dict() if self.confirmation is not None else None
            ),
        }


@dataclass(frozen=True)
class FallbackPolicy:
    """One authoritative persisted fallback policy for one user."""

    version: int = POLICY_VERSION
    mode: str = MODE_OFF
    routes: tuple[FallbackRoute, ...] = ()
    sources: Mapping[str, FallbackSourceRule] = field(
        default_factory=lambda: {
            SOURCE_HERV3: FallbackSourceRule(),
            SOURCE_CODEX_CLI: FallbackSourceRule(),
        }
    )
    # Per-privacy-policy narrowing: a narrower denial always wins and can only
    # narrow the global policy, never widen it.
    privacy_narrowing: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    max_additional_calls: int = 1
    max_extra_cost_usd: float | None = None
    allow_unknown_cost: bool = False

    def __post_init__(self) -> None:
        if int(self.version) != POLICY_VERSION:
            raise ValueError(f"unsupported fallback policy version: {self.version!r}")
        mode = str(self.mode or "").strip().casefold()
        if mode not in FALLBACK_MODES:
            raise ValueError(f"unknown fallback mode: {self.mode!r}")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "routes", tuple(self.routes))
        parsed_sources: dict[str, FallbackSourceRule] = {}
        for name in FALLBACK_SOURCES:
            value = (self.sources or {}).get(name)
            parsed_sources[name] = (
                value
                if isinstance(value, FallbackSourceRule)
                else FallbackSourceRule.from_mapping(value)
            )
        object.__setattr__(self, "sources", parsed_sources)
        object.__setattr__(self, "version", POLICY_VERSION)
        narrowing: dict[str, Mapping[str, Any]] = {}
        for name, value in dict(self.privacy_narrowing or {}).items():
            normalized = str(name or "").strip().casefold()
            if not normalized:
                raise ValueError("privacy narrowing names must be non-empty")
            if not isinstance(value, Mapping):
                raise ValueError("privacy narrowing must be an object")
            narrowing[normalized] = dict(value)
        object.__setattr__(self, "privacy_narrowing", narrowing)
        calls = _non_negative_int(self.max_additional_calls, "max_additional_calls")
        object.__setattr__(self, "max_additional_calls", calls)
        if self.max_extra_cost_usd is not None:
            ceiling = float(self.max_extra_cost_usd)
            if ceiling < 0:
                raise ValueError("max_extra_cost_usd must be non-negative")
            object.__setattr__(self, "max_extra_cost_usd", ceiling)
        object.__setattr__(
            self,
            "allow_unknown_cost",
            _strict_bool(self.allow_unknown_cost, "allow_unknown_cost"),
        )

    def source_rule(self, source: str) -> FallbackSourceRule:
        return self.sources.get(
            str(source or "").strip().casefold(), FallbackSourceRule()
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "FallbackPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("fallback policy must be an object")
        if int(raw.get("version") or POLICY_VERSION) != POLICY_VERSION:
            raise ValueError("unsupported fallback policy version")
        routes_raw = raw.get("routes") or []
        if not isinstance(routes_raw, list):
            raise ValueError("fallback routes must be a list")
        routes = tuple(FallbackRoute.from_mapping(item) for item in routes_raw)
        sources_raw = raw.get("sources") or {}
        if not isinstance(sources_raw, Mapping):
            raise ValueError("fallback sources must be an object")
        sources = {
            name: FallbackSourceRule.from_mapping(sources_raw.get(name))
            for name in FALLBACK_SOURCES
        }
        narrowing_raw = raw.get("privacy_narrowing") or {}
        if not isinstance(narrowing_raw, Mapping):
            raise ValueError("privacy narrowing must be an object")
        ceiling = raw.get("max_extra_cost_usd")
        return cls(
            version=POLICY_VERSION,
            mode=str(raw.get("mode") or MODE_OFF),
            routes=routes,
            sources=sources,
            privacy_narrowing=narrowing_raw,
            max_additional_calls=int(raw.get("max_additional_calls") or 0),
            max_extra_cost_usd=(None if ceiling is None else float(ceiling)),
            allow_unknown_cost=raw.get("allow_unknown_cost", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "routes": [route.to_dict() for route in self.routes],
            "sources": {
                name: rule.to_dict() for name, rule in self.sources.items()
            },
            "privacy_narrowing": {
                name: dict(value) for name, value in self.privacy_narrowing.items()
            },
            "max_additional_calls": self.max_additional_calls,
            "max_extra_cost_usd": self.max_extra_cost_usd,
            "allow_unknown_cost": self.allow_unknown_cost,
        }

    def with_route(
        self, route: FallbackRoute, *, position: int | None = None
    ) -> "FallbackPolicy":
        """Return a policy with one exact route added or moved."""

        routes = [item for item in self.routes if item.key() != route.key()]
        index = (
            len(routes)
            if position is None
            else max(0, min(int(position), len(routes)))
        )
        routes.insert(index, route)
        return replace(self, routes=tuple(routes))

    def without_route(self, route_key: str) -> "FallbackPolicy":
        return replace(
            self,
            routes=tuple(item for item in self.routes if item.key() != route_key),
        )

    def effective_narrowing(self, privacy_level: int) -> Mapping[str, Any]:
        return dict(self.privacy_narrowing.get(_privacy_name(privacy_level)) or {})


def default_policy() -> FallbackPolicy:
    """The safe default: fallback is OFF for every existing and migrated user."""

    return FallbackPolicy()


def request_modality(
    *,
    voice_origin: bool = False,
    request_mode: str | None = None,
) -> str:
    """Map authoritative request facts onto the policy modality vocabulary."""

    mode = str(request_mode or "").strip().casefold()
    if mode in EXCLUDED_MODALITIES:
        return mode
    return "voice" if voice_origin else "text"


def _narrowed_limit(policy: FallbackPolicy, privacy_level: int) -> int:
    narrowing = policy.effective_narrowing(privacy_level)
    limit = int(policy.max_additional_calls)
    raw_limit = narrowing.get("max_additional_calls")
    if raw_limit is not None:
        limit = min(limit, max(0, int(raw_limit)))
    return limit


def _narrowed_route_keys(
    policy: FallbackPolicy, privacy_level: int
) -> frozenset[str] | None:
    narrowing = policy.effective_narrowing(privacy_level)
    allowed = narrowing.get("allow_route_keys")
    if allowed is None:
        return None
    if not isinstance(allowed, (list, tuple)):
        raise ValueError("privacy narrowing allow_route_keys must be a list")
    return frozenset(str(item) for item in allowed)


def _confirmation_valid(
    confirmation: FallbackConfirmation | None,
    *,
    facts: "FallbackRequestFacts",
    route_key: str,
    now: float,
) -> bool:
    """A confirmation is bound to the exact failed request and is single-use.

    Reuse is prevented by binding the still-authoritative additional-call
    budget: after the first paid call advances it, the old action no longer
    matches and cannot issue another call.
    """

    if confirmation is None:
        return False
    return bool(
        confirmation.action_id
        and confirmation.request_id == str(facts.request_id or "")
        and confirmation.route_key == route_key
        and int(confirmation.privacy_level) == int(facts.privacy_level)
        and int(confirmation.budget_used_at_issue) == int(facts.budget_used)
        and now <= float(confirmation.expires_at)
    )


def make_ask_confirmation(
    policy: FallbackPolicy,
    facts: "FallbackRequestFacts",
    route: FallbackRoute,
    *,
    now: float | None = None,
    ttl_s: float = 600.0,
) -> FallbackConfirmation:
    """Build the one-time action an interactive Frontend may present."""

    issued = float(now if now is not None else time.time())
    material = "|".join(
        (
            str(facts.request_id or ""),
            route.key(),
            str(int(facts.privacy_level)),
            str(int(facts.budget_used)),
        )
    )
    action_id = blake2s(material.encode("utf-8"), digest_size=10).hexdigest()
    return FallbackConfirmation(
        action_id=action_id,
        request_id=str(facts.request_id or ""),
        route_key=route.key(),
        privacy_level=int(facts.privacy_level),
        budget_used_at_issue=int(facts.budget_used),
        issued_at=issued,
        expires_at=issued + max(30.0, float(ttl_s)),
    )


@dataclass(frozen=True)
class FallbackRequestFacts:
    """Everything the policy needs to know about one candidate request.

    Consumers supply authoritative facts only; the resolver owns the ordered
    gates and records the first blocking reason.
    """

    source: str
    source_provider: str
    source_model: str
    modality: str
    privacy_level: int
    tool_activity: bool = False
    visible_partial_answer: bool = False
    checkpoint_safe: bool = False
    budget_used: int = 0
    known_extra_cost_usd: float | None = None
    interactive: bool = True
    request_id: str = ""
    confirmation: FallbackConfirmation | None = None
    route_facts: Mapping[str, FallbackRouteFacts] = field(default_factory=dict)

    def facts_for(self, route: FallbackRoute) -> FallbackRouteFacts:
        value = (self.route_facts or {}).get(route.key())
        if isinstance(value, FallbackRouteFacts):
            return value
        return FallbackRouteFacts()


def _same_model_claim_holds(facts: "FallbackRequestFacts", route: FallbackRoute) -> bool:
    """Prove a same-model claim through the authoritative registry."""

    from orchestrator.flexible_backend_registry import same_model_target

    return same_model_target(
        source_engine=facts.source_provider,
        source_model=facts.source_model,
        target_engine=route.provider,
        target_model=route.model,
    )


def resolve_fallback_decision(
    policy: FallbackPolicy,
    facts: FallbackRequestFacts,
    *,
    now: float | None = None,
) -> FallbackDecision:
    """Resolve the effective decision and record the first blocking reason.

    Gate order (a narrower denial always wins):

    1. hard request-scope exclusion, including phone/call/voice;
    2. current privacy policy;
    3. global mode;
    4. source-backend rule;
    5. exact allowed target and its order;
    6. configured credential/runtime availability;
    7. model, Tool, context, and message-contract compatibility;
    8. checkpoint/current-call safety;
    9. remaining additional-call and cost budget.
    """

    timestamp = float(now if now is not None else time.time())
    source = str(facts.source or "").strip().casefold()
    modality = str(facts.modality or "text").strip().casefold()
    limit = _narrowed_limit(policy, facts.privacy_level)
    allow_known = bool(policy.allow_unknown_cost)

    def decision(
        *,
        allowed: bool,
        route: FallbackRoute | None = None,
        reason: str | None = None,
        disposition: str,
        confirmation: FallbackConfirmation | None = None,
    ) -> FallbackDecision:
        return FallbackDecision(
            allowed=bool(allowed),
            mode=policy.mode,
            source=source,
            route=route,
            blocking_reason=reason,
            disposition=disposition,
            additional_calls_limit=limit,
            additional_calls_used=int(facts.budget_used),
            allow_unknown_cost=allow_known,
            cost_ceiling_usd=policy.max_extra_cost_usd,
            confirmation=confirmation,
            tool_activity=bool(facts.tool_activity),
            visible_partial_answer=bool(facts.visible_partial_answer),
        )

    # 1. hard request-scope exclusion (phone/call/voice) evaluated first.
    if modality in EXCLUDED_MODALITIES:
        return decision(
            allowed=False,
            reason=REASON_MODALITY_EXCLUDED,
            disposition=DISPOSITION_BLOCKED,
        )
    if source not in FALLBACK_SOURCES:
        return decision(
            allowed=False,
            reason=REASON_NO_ALLOWED_TARGET,
            disposition=DISPOSITION_BLOCKED,
        )

    # 2. current privacy policy: narrowing only.
    narrowing = policy.effective_narrowing(facts.privacy_level)
    if narrowing.get("blocked") is True:
        return decision(
            allowed=False,
            reason=REASON_PRIVACY_BLOCKED,
            disposition=DISPOSITION_BLOCKED,
        )
    allowed_keys = _narrowed_route_keys(policy, facts.privacy_level)

    # 3. global mode.
    if policy.mode == MODE_OFF:
        return decision(
            allowed=False,
            reason=REASON_DISABLED,
            disposition=DISPOSITION_DISABLED,
        )

    # 4. source-backend rule.
    rule = policy.source_rule(source)
    if not rule.enabled:
        return decision(
            allowed=False,
            reason=REASON_SOURCE_DISABLED,
            disposition=DISPOSITION_BLOCKED,
        )

    # 5-9. ordered allowlist with the remaining gates.
    candidates = [route for route in policy.routes if route.source == source]
    if not candidates:
        return decision(
            allowed=False,
            reason=REASON_NO_ALLOWED_TARGET,
            disposition=DISPOSITION_UNAVAILABLE,
        )
    first_reason: str | None = None
    for route in candidates:
        if (
            route.target_kind == TARGET_KIND_HERV3_MODEL
            and not rule.allow_herv3_targets
        ):
            first_reason = first_reason or REASON_NO_ALLOWED_TARGET
            continue
        if (
            route.target_kind == TARGET_KIND_CODEX_CLI
            and not rule.allow_codex_cli_targets
        ):
            first_reason = first_reason or REASON_NO_ALLOWED_TARGET
            continue
        if allowed_keys is not None and route.key() not in allowed_keys:
            first_reason = first_reason or REASON_NO_ALLOWED_TARGET
            continue
        route_facts = facts.facts_for(route)
        # 6. credential/runtime availability.
        if not route_facts.credential_ready:
            first_reason = first_reason or REASON_MISSING_KEY_OR_RUNTIME
            continue
        # 7. model, Tool, context, and message-contract compatibility.
        if not route_facts.capability_ok:
            first_reason = first_reason or route_facts.capability_reason
            continue
        if route.same_model and not _same_model_claim_holds(facts, route):
            first_reason = first_reason or REASON_INCOMPATIBLE_CAPABILITY
            continue
        # 8. checkpoint/current-call safety.
        if route.target_kind == TARGET_KIND_CODEX_CLI and (
            facts.tool_activity or facts.visible_partial_answer
        ):
            first_reason = first_reason or REASON_UNSAFE_CHECKPOINT
            continue
        if (
            route.target_kind == TARGET_KIND_HERV3_MODEL
            and facts.tool_activity
            and not facts.checkpoint_safe
        ):
            first_reason = first_reason or REASON_UNSAFE_CHECKPOINT
            continue
        # 9. remaining additional-call and cost budget.
        if int(facts.budget_used) >= limit:
            first_reason = first_reason or REASON_BUDGET_EXHAUSTED
            continue
        if facts.known_extra_cost_usd is None and not allow_known:
            if policy.mode == MODE_ASK and facts.interactive:
                # The bound interactive confirmation is the user's explicit
                # cost decision for an unknown price.
                pass
            else:
                first_reason = first_reason or REASON_UNKNOWN_COST
                continue
        if (
            facts.known_extra_cost_usd is not None
            and policy.max_extra_cost_usd is not None
            and float(facts.known_extra_cost_usd) > float(policy.max_extra_cost_usd)
        ):
            first_reason = first_reason or REASON_BUDGET_EXHAUSTED
            continue
        # Mode gate: only an exact, bound, unexpired confirmation authorises
        # an interactive ask; background work can never answer, so it fails
        # closed and reports that automatic fallback was not authorised.
        if policy.mode == MODE_ASK:
            if _confirmation_valid(
                facts.confirmation, facts=facts, route_key=route.key(), now=timestamp
            ):
                return decision(
                    allowed=True,
                    route=route,
                    disposition=DISPOSITION_READY,
                    confirmation=facts.confirmation,
                )
            if facts.interactive:
                return decision(
                    allowed=False,
                    route=route,
                    reason=REASON_CONFIRMATION_REQUIRED,
                    disposition=DISPOSITION_ASK_REQUIRED,
                    confirmation=make_ask_confirmation(
                        policy, facts, route, now=timestamp
                    ),
                )
            return decision(
                allowed=False,
                route=route,
                reason=REASON_CONFIRMATION_REQUIRED,
                disposition=DISPOSITION_BLOCKED,
            )
        return decision(
            allowed=True,
            route=route,
            disposition=DISPOSITION_READY,
        )
    return decision(
        allowed=False,
        reason=first_reason or REASON_NO_ALLOWED_TARGET,
        disposition=DISPOSITION_UNAVAILABLE,
    )


def read_policy(
    runtime: Any,
    actor_id: Any,
    *,
    for_update: bool = False,
) -> tuple[FallbackPolicy, ConfigDocument | None]:
    """Read the saved policy; a missing file migrates to the safe OFF default.

    Display reads fall back to the default when preferences cannot be read.
    An update must use ``for_update=True`` and never treat damage as an
    invitation to replace the file with defaults.
    """

    path = policy_path(runtime)
    with _POLICY_LOCK:
        try:
            payload = read_config_json(path)
        except FileNotFoundError:
            return default_policy(), None
        except (OSError, ValueError, UnicodeError):
            if for_update:
                raise
            return default_policy(), None
    if for_update:
        version = payload.get("version", POLICY_VERSION)
        if type(version) is not int or version != POLICY_VERSION:
            raise ValueError("Unsupported model fallback policy version")
        users = payload.get("users")
        if not isinstance(users, dict):
            raise ValueError("model fallback policy users must be a JSON object")
        raw = users.get(_actor_key(actor_id))
        policy = (
            FallbackPolicy.from_mapping(raw)
            if isinstance(raw, Mapping)
            else default_policy()
        )
        return policy, payload
    users = payload.get("users")
    record = users.get(_actor_key(actor_id)) if isinstance(users, dict) else None
    policy = (
        FallbackPolicy.from_mapping(record)
        if isinstance(record, Mapping)
        else default_policy()
    )
    return policy, None


def write_policy(
    runtime: Any,
    actor_id: Any,
    policy: FallbackPolicy,
    *,
    document: ConfigDocument | Mapping[str, Any] | None,
) -> Path:
    """Publish one actor's policy atomically under the read revision.

    Unknown fields and other actors' records are preserved; a stale or
    damaged document is reported and never blindly retried.
    """

    path = policy_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _POLICY_LOCK:
        if document is None:
            payload: Any = {"version": POLICY_VERSION, "users": {}}
        else:
            payload = (
                document.copy()
                if isinstance(document, ConfigDocument)
                else dict(document)
            )
        payload.setdefault("version", POLICY_VERSION)
        payload["users"] = dict(payload.get("users") or {})
        payload["users"][_actor_key(actor_id)] = policy.to_dict()
        if isinstance(payload, ConfigDocument):
            write_config_json(path, payload)
        else:
            write_config_json(path, payload, expected_revision=None)
    return path


__all__ = [
    "FALLBACK_MODES",
    "FALLBACK_SOURCES",
    "FALLBACK_TARGET_KINDS",
    "FallbackConfirmation",
    "FallbackDecision",
    "FallbackPolicy",
    "FallbackRequestFacts",
    "FallbackRoute",
    "FallbackRouteFacts",
    "FallbackSourceRule",
    "MODE_ASK",
    "MODE_AUTO",
    "MODE_OFF",
    "SOURCE_CODEX_CLI",
    "SOURCE_HERV3",
    "TARGET_KIND_CODEX_CLI",
    "TARGET_KIND_HERV3_MODEL",
    "default_policy",
    "make_ask_confirmation",
    "policy_path",
    "read_policy",
    "request_modality",
    "resolve_fallback_decision",
    "write_policy",
]
