"""Shared, connector-neutral parsing rules for the sensitive ``/sys`` command.

Only the structural prefix is tokenized.  Prompt text is carried as one opaque
argument so frontends cannot change its quotes, newlines, or internal spacing.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable

MAX_SYS_PROMPT_CHARS = 65_536
MAX_SYS_COMMAND_CHARS = MAX_SYS_PROMPT_CHARS + 256
SYS_TEXT_ACTIONS = frozenset({"save", "replace"})
SYS_GLOBAL_SCOPE_ALIASES = frozenset({"global", "g"})
SYS_LOCAL_SCOPE_ALIASES = frozenset({"local", "l"})
SYS_SCOPE_ALIASES = SYS_GLOBAL_SCOPE_ALIASES | SYS_LOCAL_SCOPE_ALIASES

# Commands that can move another Agent's configuration, and the verbs that do it.
# `clear` never names a second Agent, so it needs no cross-Agent authorization.
CONFIG_TRANSFER_COMMANDS = frozenset({"sys", "workzone"})
CONFIG_TRANSFER_VERBS = frozenset({"copy", "import"})


class SysCommandContractError(ValueError):
    """A public, typed ``/sys`` validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code
        self.error_message = message
        self.request_outcome = "rejected"


def _split_words(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        return [
            part[1:-1]
            if len(part) >= 2
            and part[0] == part[-1]
            and part[0] in {'"', "'"}
            else part
            for part in shlex.split(raw, posix=False)
        ]
    except Exception:
        return raw.split()


def _next_token(value: str, offset: int) -> tuple[str, int]:
    match = re.search(r"\S+", value[offset:])
    if match is None:
        return "", len(value)
    start = offset + match.start()
    end = offset + match.end()
    return value[start:end], end


def _prompt_body_after_separator(value: str, offset: int) -> str:
    """Consume one syntax separator and preserve every prompt character after it."""

    if value.startswith("\r\n", offset):
        return value[offset + 2 :]
    if offset < len(value) and value[offset] in " \t\r\n":
        return value[offset + 1 :]
    return value[offset:]


def _validate_prompt_text(value: str) -> str:
    if len(value) > MAX_SYS_PROMPT_CHARS:
        raise SysCommandContractError(
            "sys_prompt_too_large",
            f"System prompt text is too long ({len(value)} characters). "
            f"The maximum is {MAX_SYS_PROMPT_CHARS} characters.",
        )
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise SysCommandContractError(
            "sys_prompt_invalid",
            "System prompt text contains an unsupported control character.",
        )
    return value


def parse_sys_command_arguments(remainder: str) -> list[str]:
    """Parse a raw ``/sys`` suffix without tokenizing its prompt body."""

    raw = str(remainder or "")
    args: list[str] = []
    offset = 0
    first, offset = _next_token(raw, offset)
    if not first:
        return args
    args.append(first)

    target = first.casefold()
    if target in SYS_SCOPE_ALIASES:
        target, offset = _next_token(raw, offset)
        if not target:
            return args
        args.append(target)

    if target.casefold() == "output":
        slot, offset = _next_token(raw, offset)
        if slot:
            args.append(slot)
        args.extend(_split_words(raw[offset:]))
        return args

    action, offset = _next_token(raw, offset)
    if not action:
        return args
    args.append(action)
    if action.casefold() in SYS_TEXT_ACTIONS:
        # Exactly one separator belongs to the command grammar. Additional
        # indentation, blank lines, and trailing whitespace belong to the prompt.
        body = _prompt_body_after_separator(raw, offset)
        if body:
            args.append(body)
        return args
    args.extend(_split_words(raw[offset:]))
    return args


def normalize_sys_arguments(arguments: Iterable[object]) -> list[str]:
    """Canonicalize legacy split arguments into a single prompt argument."""

    values = [str(argument) for argument in arguments]
    if not values:
        return []
    scope_offset = 1 if values[0].casefold() in SYS_SCOPE_ALIASES else 0
    action_offset = scope_offset + 1
    if action_offset >= len(values):
        return values
    if values[action_offset].casefold() not in SYS_TEXT_ACTIONS:
        return values
    prefix = values[: action_offset + 1]
    body_parts = values[action_offset + 1 :]
    if not body_parts:
        return prefix
    body = body_parts[0] if len(body_parts) == 1 else " ".join(body_parts)
    return [*prefix, _validate_prompt_text(body)]


def config_transfer_target_agent(
    command: str, arguments: Iterable[str] | None
) -> str | None:
    """Return the sibling Agent a cross-Agent config transfer names.

    One parser serves both the authorization gate and the command handlers, so a
    permission check can never inspect a different Agent than the one the
    handler will read or write. ``None`` means the command touches no other
    Agent (or is malformed, which the handler rejects on its own).
    """

    name = str(command or "").casefold()
    if name not in CONFIG_TRANSFER_COMMANDS:
        return None
    values = [str(item) for item in arguments or ()]
    if name == "sys":
        try:
            values = normalize_sys_arguments(values)
        except SysCommandContractError:
            # The handler reports this typed failure itself; it is not a transfer.
            return None
        if values and values[0].casefold() in SYS_GLOBAL_SCOPE_ALIASES:
            # `/sys global` is refused by the handler itself.
            return None
        if values and values[0].casefold() in SYS_LOCAL_SCOPE_ALIASES:
            values = values[1:]
    else:
        # `/workzone` drops blank arguments before it parses its verb.
        values = [item.strip() for item in values if item.strip()]
    if len(values) < 2:
        return None
    if values[0].casefold() not in CONFIG_TRANSFER_VERBS:
        return None
    return values[1]


def is_sys_command_line(command_line: str) -> bool:
    match = re.match(r"^\s*/([^\s]+)", str(command_line or ""))
    return bool(match and match.group(1).split("@", 1)[0].casefold() == "sys")


def command_line_limit(command_line: str, *, default: int = 16_384) -> int:
    return MAX_SYS_COMMAND_CHARS if is_sys_command_line(command_line) else default


def validate_command_line_length(command_line: str, *, default: int = 16_384) -> None:
    if is_sys_command_line(command_line):
        raw = str(command_line or "").lstrip()
        match = re.match(r"/[^\s]+([\s\S]*)$", raw)
        normalize_sys_arguments(
            parse_sys_command_arguments(match.group(1) if match else "")
        )
    limit = command_line_limit(command_line, default=default)
    if len(str(command_line or "")) <= limit:
        return
    if is_sys_command_line(command_line):
        raise SysCommandContractError(
            "sys_prompt_too_large",
            f"System prompt command is too long. The prompt text maximum is "
            f"{MAX_SYS_PROMPT_CHARS} characters.",
        )
    raise ValueError("command is too long")


__all__ = [
    "CONFIG_TRANSFER_COMMANDS",
    "CONFIG_TRANSFER_VERBS",
    "MAX_SYS_COMMAND_CHARS",
    "MAX_SYS_PROMPT_CHARS",
    "SYS_GLOBAL_SCOPE_ALIASES",
    "SYS_LOCAL_SCOPE_ALIASES",
    "SysCommandContractError",
    "command_line_limit",
    "config_transfer_target_agent",
    "is_sys_command_line",
    "normalize_sys_arguments",
    "parse_sys_command_arguments",
    "validate_command_line_length",
]
# HASHI-Fingerprint: hfp_535bd45326c54db5bbdcb9b4d655636c
