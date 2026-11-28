"""Author-owned HAL v0.1 language: deterministic, dependency-free compiler.

This is NOT an active automation executor. No command dispatch, network access,
background worker, event subscription, or runtime effect occurs here.
Author-reserved semantics (else, $stop, alias mapping) fail explicitly rather
than being invented by the implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
import re

EVENTS = frozenset({
    "#turn_finish", "#turn_error", "#turn_commentary",
    "#system_idle", "#hashideploy_finish", "#reboot_ok",
    # Whiteboard stage-transition feed (Phase 2). Stage names are author data,
    # never compiled-in: only this event token is fixed.
    "#whiteboard_stage",
})
# Events whose triggers carry a whiteboard card context. Conditions and prompt
# variables are only meaningful for these.
CONTEXT_EVENTS = frozenset({"#whiteboard_stage"})
AGENT = r"@[A-Za-z_][A-Za-z0-9_-]*(?:@[A-Za-z_][A-Za-z0-9_-]*)?"
ROLE_TARGET = re.compile(r"^@\{(?P<role>[a-z_]+)\}$")
TARGET = rf"(?:{AGENT}|@\{{[a-z_]+\}})"
WHEN = re.compile(
    rf"^if\s+(?:(?P<agent>{AGENT})\s+)?(?P<event>\#[a-z_]+)"
    r"(?:\s+where\s+(?P<where>\[[^\]]*\]))?"
    rf"\s+then\s+(?P<action>.+)$",
    re.S,
)
PROMPT = re.compile(rf"^(?P<target>{TARGET})\s+\$prompt\s+\"(?P<body>[^\"]*)\"$", re.S)
COMMAND = re.compile(r"^/[A-Za-z_][A-Za-z0-9_-]*(?:\s+[^\r\n\"]+)?$")
UNBOUND = re.compile(r"@agent_trigger(?![A-Za-z0-9_-])")

# Author-selectable condition keys for ``where [...]``. Values are compared
# exactly against the event document, so a whiteboard that adds a new stage key
# needs no HAL change.
CONDITION_KEYS = frozenset({"project", "from_stage", "to_stage", "category"})
# Card roles a prompt may be routed to. The value comes from the card itself.
ROLE_TARGETS = frozenset({"implementer", "reviewer", "tester"})
# Prompt variables available on a context event. Names are fixed here; the
# values always come from the event document.
PROMPT_VARS = frozenset({
    "card_id", "work_key", "project", "from_stage", "to_stage", "revision",
    "round", "max_rounds", "implementer", "reviewer", "tester", "pr_url",
    "head_sha", "last_verdict", "test_env", "next_step",
})
VAR = re.compile(r"\{([a-z_]+)\}")


class HALSyntaxError(ValueError):
    """Explicit parser diagnostic, never an automatic execution fallback."""

    def __init__(self, message: str, *, line: int, column: int):
        super().__init__(f"{message} (line {line}, column {column})")
        self.message = message
        self.line = line
        self.column = column


@dataclass(frozen=True)
class HALAction:
    kind: str  # 'prompt' | 'native_command'
    target: str | None = None
    value: str = ""


@dataclass(frozen=True)
class HALRule:
    source_agent: str | None
    event: str
    action: HALAction
    terminator: str
    line: int
    # ``where [key="value", ...]`` pairs, compared exactly against the event
    # context. Empty for rules that match on the event token alone.
    conditions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HALProgram:
    # Each tuple is one workflow. Only its first rule is an independent entry.
    workflows: tuple[tuple[HALRule, ...], ...]


@dataclass(frozen=True)
class HALEvent:
    name: str
    # Use canonical @agent@hashi_instance identity, not display names.
    agent_identity: str | None = None
    event_id: str | None = None
    # Event document for context events (whiteboard stage transitions). The
    # condition matcher reads it; non-context events leave it None.
    context: Mapping[str, Any] | None = None


def _split_conditions(inner: str) -> list[str]:
    """Split on commas outside double quotes so a value may contain one."""
    parts: list[str] = []
    current: list[str] = []
    in_string = False
    for char in inner:
        if char == '"':
            in_string = not in_string
        if char == "," and not in_string:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if in_string:
        raise ValueError("unclosed string in where clause")
    parts.append("".join(current))
    return [part for part in (p.strip() for p in parts) if part]


_CONDITION_PAIR = re.compile(r'^(?P<key>[a-z_]+)\s*=\s*"(?P<value>[^"]*)"$')


def parse_conditions(source: str, position: int, text: str) -> tuple[tuple[str, str], ...]:
    """Parse ``[key="value", ...]`` explicitly; never guess at a near miss."""
    try:
        parts = _split_conditions(text[1:-1])
    except ValueError:
        raise _fail(source, position, "Unclosed string in HAL where clause") from None
    if not parts:
        raise _fail(source, position, "Empty HAL where clause")
    pairs: list[tuple[str, str]] = []
    for part in parts:
        match = _CONDITION_PAIR.fullmatch(part)
        if not match:
            raise _fail(
                source, position,
                'HAL where conditions must be key="value" pairs separated by commas',
            )
        key = match.group("key")
        if key not in CONDITION_KEYS:
            raise _fail(
                source, position,
                f"Unknown HAL condition key: {key} (allowed: {', '.join(sorted(CONDITION_KEYS))})",
            )
        pairs.append((key, match.group("value")))
    return tuple(pairs)


def _fail(source: str, position: int, message: str) -> HALSyntaxError:
    before = source[:position]
    return HALSyntaxError(
        message,
        line=before.count("\n") + 1,
        column=len(before.rsplit("\n", 1)[-1]) + 1,
    )


def _statements(source: str):
    """Scan terminators outside quotes. A semicolon inside a prompt is text."""
    if not isinstance(source, str):
        raise TypeError("HAL script must be a string")
    start = 0
    in_string = False
    for offset, char in enumerate(source):
        if char == '"':
            in_string = not in_string
        if not in_string and char in (";", "."):
            sentence = source[start:offset].strip()
            if not sentence:
                raise _fail(source, start, "Empty HAL rule")
            yield sentence, char, start
            start = offset + 1
    if in_string:
        raise _fail(source, start, "Unclosed HAL string")
    if source[start:].strip():
        raise _fail(source, start, "HAL rule must end with '.' or ';'")
    if not source.strip():
        raise _fail(source, 0, "HAL script is empty")


def compile_hal(source: str) -> HALProgram:
    """Validate the author-defined subset, compile immutable workflow stages.

    Unsupported semantics are intentional errors (never 'parse as no-op').
    """
    chains: list[tuple[HALRule, ...]] = []
    chain: list[HALRule] = []
    for sentence, terminator, position in _statements(source):
        match = WHEN.fullmatch(sentence)
        if not match:
            raise _fail(source, position, "Expected 'if [@agent] #event then action'")
        source_agent = match.group("agent")
        event = match.group("event")
        if event not in EVENTS:
            raise _fail(source, position, f"Unknown HAL event: {event}")
        if source_agent == "@agent_trigger":
            raise _fail(source, position, "@agent_trigger is a runtime binding, not a source wildcard")
        if source_agent == "@agent_any" and event in {"#system_idle", "#hashideploy_finish", "#reboot_ok"}:
            raise _fail(source, position, "System events have no Agent wildcard source")
        where = match.group("where")
        if where is not None and event not in CONTEXT_EVENTS:
            raise _fail(
                source, position,
                f"HAL where conditions require a context event ({', '.join(sorted(CONTEXT_EVENTS))})",
            )
        if event in CONTEXT_EVENTS and source_agent is not None:
            raise _fail(
                source, position,
                f"{event} is not an Agent event; declare no source Agent and route in the action",
            )
        conditions = (
            parse_conditions(source, position + match.start("where"), where)
            if where is not None else ()
        )
        # A dependent stage of a whiteboard workflow still renders the card
        # frozen at its entry, so role targets and variables are legal there
        # even though the stage itself waits for an Agent event.
        entry_event = chain[0].event if chain else event
        card_context = entry_event in CONTEXT_EVENTS
        action_text = match.group("action").strip()
        # The author has not defined the else failure condition or $stop target.
        if re.search(r"\s+else\s+", re.sub(r'"[^"]*"', '""', action_text)):
            raise _fail(source, position, "HAL 'else' semantics are reserved for author approval")
        prompt = PROMPT.fullmatch(action_text)
        if prompt:
            target = prompt.group("target")
            if target == "@agent_any":
                raise _fail(source, position, "@agent_any cannot be a prompt destination")
            role = ROLE_TARGET.fullmatch(target)
            if role is not None:
                if role.group("role") not in ROLE_TARGETS:
                    raise _fail(
                        source, position,
                        f"Unknown HAL role target: {target} "
                        f"(allowed: {', '.join(sorted(ROLE_TARGETS))})",
                    )
                if not card_context:
                    raise _fail(
                        source, position,
                        f"role target {target} needs a card context entry "
                        f"({', '.join(sorted(CONTEXT_EVENTS))})",
                    )
            body = prompt.group("body")
            if card_context:
                unknown = sorted({name for name in VAR.findall(body) if name not in PROMPT_VARS})
                if unknown:
                    raise _fail(
                        source, position,
                        f"Unknown HAL prompt variable(s): {', '.join(unknown)}",
                    )
            action = HALAction("prompt", target, body)
        elif action_text.startswith("/"):
            if not COMMAND.fullmatch(action_text):
                raise _fail(source, position, "Invalid native HASHI command syntax")
            action = HALAction("native_command", None, action_text)
        elif "$stop" in action_text:
            raise _fail(source, position, "$stop target/semantics await author approval")
        else:
            raise _fail(source, position, "Unknown HAL action or invalid prompt syntax")
        line = source[:position].count("\n") + 1
        chain.append(HALRule(source_agent, event, action, terminator, line, conditions))
        if terminator == ".":
            chains.append(tuple(chain))
            chain = []
    if chain:
        raise _fail(source, len(source), "Trailing ';' requires another dependent 'if' rule")
    return HALProgram(tuple(chains))


def entry_matches(
    rule: HALRule,
    event: HALEvent,
    *,
    current_instance: str | None = None,
) -> bool:
    """Pure event condition matcher; caller MUST pass an entry-stage rule only.

    No polling, command dispatch, side effects, or chain auto-advancement.
    The event adapter must enforce authenticated instance ownership.
    """
    if rule.event != event.name:
        return False
    if not conditions_match(rule.conditions, event.context):
        return False
    return source_matches(
        rule.source_agent,
        event.agent_identity,
        current_instance=current_instance,
    )


def conditions_match(
    conditions: Sequence[tuple[str, str]],
    context: Mapping[str, Any] | None,
) -> bool:
    """Exact, case-sensitive comparison against the event document.

    A declared condition whose key the event does not carry never matches, so a
    typo or an absent field cannot silently widen the rule.
    """
    if not conditions:
        return True
    if context is None:
        return False
    for key, expected in conditions:
        if key not in context:
            return False
        actual = context.get(key)
        if isinstance(actual, bool) or actual is None:
            return False
        if str(actual) != expected:
            return False
    return True


def source_matches(
    source: str | None,
    identity: str | None,
    *,
    current_instance: str | None = None,
) -> bool:
    """Match a declared HAL source against a canonical @name@instance identity.

    - Bare ``@name`` matches only the same-name Agent on ``current_instance``
      (never a remote instance's same-name Agent).
    - Qualified ``@name@instance`` matches that exact Agent and instance.
    - ``@agent_any`` matches any non-empty Agent identity.
    """
    if source is None:
        return True
    if not identity:
        return False
    source = source.casefold()
    if source == "@agent_any":
        return True
    identity = identity.casefold()
    if "@" not in source[1:]:
        local = str(current_instance or "").strip().casefold()
        if local:
            return identity == f"{source}@{local}"
        return source == identity
    return source == identity


def render_context_value(value: Any) -> str:
    """One event field as prompt text; structured values stay compact JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(render_context_value(item) for item in value)
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={render_context_value(item)}" for key, item in value.items())
    return str(value)


def resolve_role_target(action: HALAction, *, context: Mapping[str, Any] | None) -> str | None:
    """Expand ``@{reviewer}``-style targets from the card's own role field.

    Returns the resolved ``@name`` / ``@name@instance`` spec, or ``None`` when
    no role target is present. An empty role is a hard failure at the caller:
    guessing a reviewer would route work to the wrong Agent.
    """
    if action.kind != "prompt" or not action.target:
        return None
    match = ROLE_TARGET.fullmatch(action.target)
    if match is None:
        return None
    role = match.group("role")
    raw = (context or {}).get(role)
    value = render_context_value(raw).strip()
    if not value:
        return ""
    return value if value.startswith("@") else f"@{value}"


def resolve_prompt(
    action: HALAction,
    *,
    agent_trigger: str | None,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Replace only the author's defined variables in prompt strings.

    ``{variable}`` substitution happens only when a card context is supplied;
    without one the braces are ordinary text (v0.1 rules keep their meaning).
    """
    if action.kind != "prompt":
        raise ValueError("Cannot render a non-prompt action")
    if UNBOUND.search(action.value) and not agent_trigger:
        raise ValueError("@agent_trigger has no Agent for this system event")
    body = UNBOUND.sub(lambda _m: agent_trigger or "", action.value)
    if context is None:
        return body

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in PROMPT_VARS:
            return match.group(0)
        return render_context_value(context.get(name))

    return VAR.sub(_substitute, body)
# HASHI-Fingerprint: hfp_978ce8347b004c8eb18a1d80b4f6d29d
