"""Private formal decision intentions for the existing shared command contract.

No parser result, invocation ID, connection binding, or action ID is authority.
Only the original authenticated user session may submit the exact issued action.
The service still fences the canonical PAO Run immediately before a fresh grant.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex

from orchestrator.permissions.authority import RevisionConflict
from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied, SessionDecisionGateway
from orchestrator.permissions.models import (
    PermissionSettings, PermissionSubject, canonical_scope_set, normalize_scope, scope_covers,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DURATION = re.compile(r"([1-9][0-9]{0,7})(s|m|h|d)\Z")


@dataclass(frozen=True, slots=True)
class DecisionIntent:
    request_id: str
    revision: int
    choice: str
    lifetime: str
    duration_seconds: int
    actions: tuple[str, ...] | None = None
    resources: tuple[int, ...] | None = None
    path: str | None = None


def parse_decision_intent(arguments: str, *, default_seconds: int | None = None) -> DecisionIntent:
    if isinstance(arguments, str) and 1 <= len(arguments) <= 8192:
        lexer = shlex.shlex(arguments, posix=True)
        lexer.whitespace_split, lexer.commenters, lexer.escape = True, "", ""
        words = list(lexer)
    elif (isinstance(arguments, (tuple, list)) and len(arguments) <= 64
          and all(isinstance(word, str) and 1 <= len(word) <= 4096 for word in arguments)):
        words = list(arguments)
    else:
        raise ValueError("a bounded formal decision command is required")
    if len(words) < 4 or words[0] not in {"approve", "deny"} or not _TOKEN.fullmatch(words[1]):
        raise ValueError("use approve or deny with a request ID and exact revision")
    choice, request_id = words[:2]
    lifetime, position = "task", 2
    if choice == "approve":
        if words[2] not in {"once", "run", "task", "ttl"}:
            raise ValueError("select once, run, task, or ttl")
        lifetime, position = words[2], 3
    options = {}
    if choice == "approve" and lifetime == "ttl" and position < len(words) and not words[position].startswith("--"):
        options["--for"] = words[position]
        position += 1
    while position < len(words):
        key = words[position]
        allowed = {"--revision"} if choice == "deny" else {
            "--revision", "--for", "--actions", "--resources", "--path",
        }
        if key not in allowed or key in options or position + 1 == len(words):
            raise ValueError("unknown, duplicate, or incomplete decision option")
        value = words[position + 1]
        if not value or value.startswith("--"):
            raise ValueError("decision option value is required")
        options[key] = value
        position += 2
    revision = options.get("--revision", "")
    if re.fullmatch(r"[1-9][0-9]{0,9}", revision) is None:
        raise ValueError("an exact positive request revision is required")
    seconds = PermissionSettings().task_default_seconds if default_seconds is None else default_seconds
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("the default decision duration is unavailable")
    if "--for" in options:
        duration = _DURATION.fullmatch(options["--for"])
        if duration is None:
            raise ValueError("use a positive duration in s, m, h, or d")
        seconds = int(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[duration[2]]
    elif lifetime == "ttl":
        raise ValueError("ttl requires an explicit duration")
    actions = None
    if "--actions" in options:
        raw = options["--actions"].split(",")
        if (not raw or len(raw) != len(set(raw))
                or any(re.fullmatch(r"[a-z][a-z_]{0,31}", action) is None for action in raw)):
            raise ValueError("select explicit actions")
        actions = tuple(sorted(raw))
    resources = None
    if "--resources" in options:
        raw = options["--resources"].split(",")
        if (not raw or len(raw) != len(set(raw))
                or any(re.fullmatch(r"[1-9][0-9]{0,5}", item) is None for item in raw)):
            raise ValueError("select displayed resource numbers starting at one")
        resources = tuple(sorted(int(item) for item in raw))
    path = options.get("--path")
    if path is not None and not Path(path).is_absolute():
        raise ValueError("a narrower path must be absolute")
    return DecisionIntent(request_id, int(revision), choice, lifetime, seconds, actions, resources, path)


def _selected_scopes(intent, requested):
    selected = requested
    if intent.resources is not None:
        if any(index > len(requested) for index in intent.resources):
            raise ValueError("a selected resource is unavailable")
        selected = tuple(requested[index - 1] for index in intent.resources)
    scopes = []
    for original in selected:
        child = original
        if intent.actions is not None:
            if not set(intent.actions).issubset(original.actions):
                raise ValueError("selected actions exceed a requested resource")
            child = type(original)(original.kind, original.resource, intent.actions,
                                   original.normalization_version)
        if intent.path is not None:
            if len(selected) != 1 or child.kind != "file":
                raise ValueError("select one file resource before narrowing its path")
            child = normalize_scope("file", intent.path, child.actions, cwd=Path(intent.path).parent)
        if not scope_covers(original, child):
            raise ValueError("selected scope exceeds the displayed request")
        scopes.append(child)
    if not scopes:
        raise ValueError("select at least one requested scope")
    return canonical_scope_set(scopes)


class PermissionDecisionController:
    """Trusted FC component; never register this as a model tool or Worker RPC."""

    def __init__(self, authority, gateway: SessionDecisionGateway, *, settings=None):
        if not isinstance(gateway, SessionDecisionGateway) or not gateway.uses_authority(authority):
            raise ValueError("the decision gateway must use this exact authority port")
        if not callable(getattr(authority, "get_request_runtime_binding", None)):
            raise ValueError("the trusted runtime binding port is required")
        self._authority, self._gateway = authority, gateway
        self._settings = settings or PermissionSettings()
        if not isinstance(self._settings, PermissionSettings) and not callable(self._settings):
            raise ValueError("canonical permission settings are required")

    def _settings_for(self, subject):
        value = self._settings(subject.agent_id) if callable(self._settings) else self._settings
        if not isinstance(value, PermissionSettings):
            raise ValueError("current Agent permission settings are unavailable")
        return value

    def submit_session_decision(self, subject, arguments, *, session_token,
                                invocation_id, connection_binding):
        if not isinstance(subject, PermissionSubject):
            raise SessionDecisionDenied("a trusted frontend subject is required")
        owner = self._gateway.owner_from_session(session_token)
        if owner != subject.owner_id:
            raise SessionDecisionDenied("the session does not own this task")
        for value in (invocation_id, connection_binding):
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise ValueError("a server-bound frontend invocation is required")
        settings = self._settings_for(subject)
        intent = parse_decision_intent(arguments, default_seconds=settings.task_default_seconds)
        limit = settings.task_default_seconds if callable(self._settings) else self._authority.max_lease_seconds
        if intent.duration_seconds > limit:
            raise ValueError("decision duration exceeds the instance policy")
        binding = self._authority.get_request_runtime_binding(subject, intent.request_id)
        view = self._authority.get_session_request(subject, intent.request_id)
        request = view.request
        if request.revision != intent.revision:
            raise RevisionConflict("request revision changed")
        scopes = _selected_scopes(intent, canonical_scope_set(request.scopes))
        # The meaning is deliberately absent from this key. An edited retry
        # must conflict with the first intention, not become a second action.
        presentation = "cmd_" + hashlib.sha256(json.dumps(
            [subject.instance_id, subject.owner_id, subject.agent_id, subject.session_id,
             subject.context_generation, invocation_id, connection_binding],
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        action = self._authority.get_presented_decision_action(
            binding.context, intent.request_id, presentation, "text",
        )
        if action is None:
            action = self._authority.issue_decision_action(
                binding.context, intent.request_id, intent.revision, approved_scopes=scopes,
                choice=intent.choice, lifetime=intent.lifetime,
                duration_seconds=intent.duration_seconds,
                presentation_id=presentation, presentation_slot="text",
            )
        elif (action.request_revision != intent.revision or action.approved_scopes != scopes
              or action.choice != intent.choice or action.lifetime != intent.lifetime
              or action.duration_seconds != intent.duration_seconds):
            raise RevisionConflict("frontend invocation already binds a different decision")
        return self._gateway.decide(session_token, binding.context, action.action_id)

    def issue_session_actions(self, subject, request_id, revision, *, session_token, presentation_id):
        if not isinstance(subject, PermissionSubject) or self._gateway.owner_from_session(session_token) != subject.owner_id:
            raise SessionDecisionDenied("the session does not own this task")
        if not isinstance(presentation_id, str) or not 1 <= len(presentation_id) <= 512:
            raise ValueError("a server presentation is required")
        binding = self._authority.get_request_runtime_binding(subject, request_id)
        request = self._authority.get_session_request(subject, request_id).request
        if request.revision != revision:
            raise RevisionConflict("request revision changed")
        duration = self._settings_for(subject).task_default_seconds
        if not callable(self._settings) and duration > self._authority.max_lease_seconds:
            raise ValueError("the default duration exceeds instance policy")
        presentation = "ui_" + hashlib.sha256(json.dumps(
            [subject.instance_id, subject.owner_id, subject.agent_id, subject.session_id,
             subject.context_generation, presentation_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        choices = [("approve", lifetime, duration) for lifetime in ("task", "run", "ttl", "once")
                   if lifetime != "once" or request.operation_id]
        # Shorter choices are derived from the policy ceiling, not extra grants.
        choices.extend(("approve", "ttl", seconds) for seconds in (600, 3600) if seconds < duration)
        choices.append(("deny", "task", duration))
        return tuple(self._authority.issue_decision_action(
            binding.context, request_id, revision, approved_scopes=request.scopes,
            choice=choice, lifetime=lifetime, duration_seconds=seconds,
            presentation_id=presentation, presentation_slot=choice + "_" + lifetime + "_" + str(seconds),
        ) for choice, lifetime, seconds in choices)

    def submit_session_action(self, subject, action_id, *, session_token):
        if not isinstance(subject, PermissionSubject) or self._gateway.owner_from_session(session_token) != subject.owner_id:
            raise SessionDecisionDenied("the session does not own this task")
        binding = self._authority.get_session_decision_binding(subject, action_id)
        return self._gateway.decide(session_token, binding.context, action_id)
