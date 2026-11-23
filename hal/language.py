"""Author-owned HAL v0.1 language: deterministic, dependency-free compiler.

This is NOT an active automation executor. No command dispatch, network access,
background worker, event subscription, or runtime effect occurs here.
Author-reserved semantics (else, $stop, alias mapping) fail explicitly rather
than being invented by the implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

EVENTS = frozenset({
    "#turn_finish", "#turn_error", "#turn_commentary",
    "#system_idle", "#hashideploy_finish", "#reboot_ok",
})
AGENT = r"@[A-Za-z_][A-Za-z0-9_-]*(?:@[A-Za-z_][A-Za-z0-9_-]*)?"
WHEN = re.compile(rf"^if\s+(?:(?P<agent>{AGENT})\s+)?(?P<event>\#[a-z_]+)\s+then\s+(?P<action>.+)$", re.S)
PROMPT = re.compile(rf"^(?P<target>{AGENT})\s+\$prompt\s+\"(?P<body>[^\"]*)\"$", re.S)
COMMAND = re.compile(r"^/[A-Za-z_][A-Za-z0-9_-]*(?:\s+[^\r\n\"]+)?$")
UNBOUND = re.compile(r"@agent_trigger(?![A-Za-z0-9_-])")


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
        action_text = match.group("action").strip()
        # The author has not defined the else failure condition or $stop target.
        if re.search(r"\s+else\s+", action_text):
            raise _fail(source, position, "HAL 'else' semantics are reserved for author approval")
        prompt = PROMPT.fullmatch(action_text)
        if prompt:
            target = prompt.group("target")
            if target == "@agent_any":
                raise _fail(source, position, "@agent_any cannot be a prompt destination")
            action = HALAction("prompt", target, prompt.group("body"))
        elif action_text.startswith("/"):
            if not COMMAND.fullmatch(action_text):
                raise _fail(source, position, "Invalid native HASHI command syntax")
            action = HALAction("native_command", None, action_text)
        elif "$stop" in action_text:
            raise _fail(source, position, "$stop target/semantics await author approval")
        else:
            raise _fail(source, position, "Unknown HAL action or invalid prompt syntax")
        line = source[:position].count("\n") + 1
        chain.append(HALRule(source_agent, event, action, terminator, line))
        if terminator == ".":
            chains.append(tuple(chain))
            chain = []
    if chain:
        raise _fail(source, len(source), "Trailing ';' requires another dependent 'if' rule")
    return HALProgram(tuple(chains))


def entry_matches(rule: HALRule, event: HALEvent) -> bool:
    """Pure event condition matcher; caller MUST pass an entry-stage rule only.

    No polling, command dispatch, side effects, or chain auto-advancement.
    The event adapter must enforce authenticated instance ownership.
    """
    if rule.event != event.name:
        return False
    if rule.source_agent is None:
        return True
    if rule.source_agent == "@agent_any":
        return bool(event.agent_identity)
    return rule.source_agent == event.agent_identity


def resolve_prompt(action: HALAction, *, agent_trigger: str | None) -> str:
    """Replace only the author's defined variable in prompt strings."""
    if action.kind != "prompt":
        raise ValueError("Cannot render a non-prompt action")
    if UNBOUND.search(action.value) and not agent_trigger:
        raise ValueError("@agent_trigger has no Agent for this system event")
    return UNBOUND.sub(lambda _m: agent_trigger or "", action.value)
