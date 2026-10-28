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
SYS_SCOPE_ALIASES = frozenset({"global", "g", "local", "l"})


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
    "MAX_SYS_COMMAND_CHARS",
    "MAX_SYS_PROMPT_CHARS",
    "SysCommandContractError",
    "command_line_limit",
    "is_sys_command_line",
    "normalize_sys_arguments",
    "parse_sys_command_arguments",
    "validate_command_line_length",
]
