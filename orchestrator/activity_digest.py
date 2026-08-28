"""Deterministic, backend-neutral summaries for Telegram verbose activity.

The digest intentionally reports only facts that HASHI can derive from typed
stream events, known tool names, command exit status, and HER lifecycle state.
It never asks a model to explain activity and never infers task intent from
arbitrary provider prose.
"""

from __future__ import annotations

import re
import shlex
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Mapping, Sequence

from adapters.stream_events import (
    KIND_ERROR,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PROGRESS,
    KIND_REVIEW,
    KIND_SHELL_EXEC,
    KIND_TESTING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    KIND_VALIDATION,
    StreamEvent,
)


CATEGORY_INSPECT = "inspect"
CATEGORY_CHANGE = "change"
CATEGORY_EXECUTE = "execute"
CATEGORY_CHECK = "check"
CATEGORY_EXTERNAL = "external"
CATEGORY_RECOVERY = "recovery"


_PHASES: dict[str, tuple[str, str]] = {
    "preparing": ("⏳", "Preparing"),
    "planning": ("🧭", "Planning"),
    "execution": ("🛠️", "Execution"),
    "replanning": ("🔄", "Replanning"),
    "review": ("🧐", "Review"),
    "verification": ("🔬", "Verification"),
    "finalisation": ("✍️", "Finalisation"),
    "completed": ("✅", "Completed"),
    "blocked": ("⛔", "Blocked"),
    "error": ("❌", "Error"),
}

_PHASE_ALIASES = {
    "received": "preparing",
    "direct": "preparing",
    "immediate_response": "preparing",
    "triage": "preparing",
    "triaged": "preparing",
    "planned": "planning",
    "planning": "planning",
    "executing": "execution",
    "execution": "execution",
    "execution_completed": "execution",
    "replanning": "replanning",
    "reviewing": "review",
    "review": "review",
    "validation": "verification",
    "verification": "verification",
    "testing": "verification",
    "finalising": "finalisation",
    "finalizing": "finalisation",
    "finalisation": "finalisation",
    "finalization": "finalisation",
    "completed": "completed",
    "completed_with_limitations": "completed",
    "pending_user_input": "blocked",
    "stopped": "blocked",
    "failed": "error",
    "error": "error",
}

_SEARCH_TOOLS = {
    "find",
    "glob",
    "google_search",
    "grep",
    "listfiles",
    "search",
    "workspace_inspect",
}
_READ_TOOLS = {
    "file_list",
    "file_read",
    "media_read",
    "process_list",
    "read",
    "read_file",
    "readfile",
}
_CHANGE_TOOL_MARKERS = (
    "apply_patch",
    "create",
    "delete",
    "edit",
    "move",
    "patch",
    "rename",
    "write",
)
_CHECK_TOOL_MARKERS = (
    "check",
    "lint",
    "test",
    "validate",
    "verification",
    "verify",
)
_EXTERNAL_TOOL_MARKERS = (
    "browser",
    "download",
    "fetch",
    "google",
    "http",
    "web",
)

_INSPECT_COMMANDS = {
    "cat",
    "fd",
    "find",
    "grep",
    "head",
    "less",
    "ls",
    "pwd",
    "rg",
    "sed",
    "tail",
    "tree",
}
_CHECK_COMMANDS = {
    "eslint",
    "jest",
    "mypy",
    "pyright",
    "pytest",
    "ruff",
    "shellcheck",
    "tox",
    "tsc",
    "vitest",
}
_EXTERNAL_COMMANDS = {"curl", "http", "wget"}
_SHELL_WRAPPERS = {"bash", "cmd", "dash", "fish", "powershell", "pwsh", "sh", "zsh"}
_COMMAND_PREFIXES = ("Running:", "Cmd:", "Command:")


@dataclass(frozen=True)
class _PendingOperation:
    category: str
    tool_name: str


def _normalise_phase(value: Any) -> str:
    raw = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _PHASE_ALIASES.get(raw, "")


def _metadata(event: StreamEvent) -> Mapping[str, Any]:
    value = getattr(event, "metadata", None)
    return value if isinstance(value, Mapping) else {}


def _command_text(event: StreamEvent) -> str:
    metadata = _metadata(event)
    command = metadata.get("command")
    if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
        return " ".join(str(part) for part in command if str(part).strip())
    if isinstance(command, str) and command.strip():
        return command.strip()
    summary = str(getattr(event, "summary", "") or "").strip()
    for prefix in _COMMAND_PREFIXES:
        if summary.casefold().startswith(prefix.casefold()):
            return summary[len(prefix) :].strip()
    return summary


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _command_programs(command: str) -> list[tuple[str, list[str]]]:
    """Return executable/argument pairs without treating quoted text as code."""

    text = str(command or "").strip()
    if not text:
        return []
    words = _shell_words(text)
    if words:
        executable = PurePath(words[0]).name.casefold()
        if executable in _SHELL_WRAPPERS:
            for flag in ("-lc", "-c", "/c"):
                if flag in words:
                    index = words.index(flag)
                    if index + 1 < len(words):
                        text = words[index + 1]
                    break

    programs: list[tuple[str, list[str]]] = []
    for segment in re.split(r"\s*(?:&&|\|\||;)\s*", text):
        tokens = _shell_words(segment)
        while tokens and ("=" in tokens[0] and not tokens[0].startswith(("/", "./"))):
            tokens.pop(0)
        if not tokens:
            continue
        program = PurePath(tokens[0]).name.casefold()
        if program in {"cd", "pushd", "popd", "env", "sudo"}:
            if program in {"env", "sudo"} and len(tokens) > 1:
                tokens = tokens[1:]
                program = PurePath(tokens[0]).name.casefold()
            else:
                continue
        programs.append((program, [token.casefold() for token in tokens[1:]]))
    return programs


def _classify_command(command: str) -> tuple[str, str]:
    programs = _command_programs(command)
    for program, args in programs:
        if program in _CHECK_COMMANDS:
            return CATEGORY_CHECK, ""
        if program in {"python", "python3", "py"} and len(args) >= 2 and args[0] == "-m":
            if args[1] in {"compileall", "mypy", "pytest", "ruff", "unittest"}:
                return CATEGORY_CHECK, ""
        if program in {"npm", "pnpm", "yarn", "bun"} and any(
            arg in {"test", "lint", "check", "typecheck"} for arg in args
        ):
            return CATEGORY_CHECK, ""
        if program in {"cargo", "go", "dotnet"} and any(
            arg in {"test", "check", "vet"} for arg in args
        ):
            return CATEGORY_CHECK, ""
        if program == "git" and args and args[0] in {"diff", "log", "show", "status"}:
            return CATEGORY_INSPECT, "search"
        if program in _INSPECT_COMMANDS:
            return CATEGORY_INSPECT, "search" if program in {"fd", "find", "grep", "rg"} else "read"
        if program in _EXTERNAL_COMMANDS:
            return CATEGORY_EXTERNAL, ""
    return CATEGORY_EXECUTE, ""


def _classify_tool(event: StreamEvent) -> tuple[str, str]:
    tool = str(getattr(event, "tool_name", "") or "").strip().casefold()
    compact = tool.replace("-", "_").replace(" ", "_")
    tool_parts = set(compact.split("_"))
    if "api" in tool_parts or any(
        marker in compact for marker in _EXTERNAL_TOOL_MARKERS
    ):
        return CATEGORY_EXTERNAL, ""
    if compact in _SEARCH_TOOLS or any(
        marker in compact for marker in ("grep", "glob", "search")
    ):
        return CATEGORY_INSPECT, "search"
    if compact in _READ_TOOLS or any(
        marker in compact for marker in ("inspect", "read", "list")
    ):
        return CATEGORY_INSPECT, "read"
    if any(marker in compact for marker in _CHANGE_TOOL_MARKERS):
        return CATEGORY_CHANGE, ""
    if any(marker in compact for marker in _CHECK_TOOL_MARKERS):
        return CATEGORY_CHECK, ""
    command = _command_text(event)
    if compact in {"bash", "command", "exec", "shell"} or command:
        return _classify_command(command)
    return CATEGORY_EXECUTE, ""


def _event_paths(event: StreamEvent) -> set[str]:
    paths: set[str] = set()
    path = str(getattr(event, "file_path", "") or "").strip()
    if path and path != "unknown":
        paths.add(path)
    values = _metadata(event).get("file_paths")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        paths.update(str(value).strip() for value in values if str(value).strip())
    return paths


def _tool_outcome(event: StreamEvent) -> str:
    metadata = _metadata(event)
    if metadata.get("blocked") is True:
        return "blocked"
    if "is_error" in metadata:
        return "error" if metadata.get("is_error") is True else "success"
    exit_code = metadata.get("exit_code")
    if exit_code is not None:
        try:
            return "success" if int(exit_code) == 0 else "error"
        except (TypeError, ValueError):
            pass
    summary = str(getattr(event, "summary", "") or "").casefold()
    if any(marker in summary for marker in ("blocked", "permission denied")):
        return "blocked"
    if any(marker in summary for marker in ("error", "failed", "failure")):
        return "error"
    match = re.search(r"(?:exit(?:ed)?|code)\s*\(?\s*(-?\d+)", summary)
    if match:
        return "success" if int(match.group(1)) == 0 else "error"
    if any(marker in summary for marker in ("done", "success", "completed")):
        return "success"
    return "unknown"


def _count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


@dataclass
class ActivityDigest:
    """Accumulate one request's typed activity and render a compact status card."""

    started_at: float = field(default_factory=time.monotonic)
    phase: str = "preparing"
    operations: Counter[str] = field(default_factory=Counter)
    completions: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    inspect_files: set[str] = field(default_factory=set)
    changed_files: set[str] = field(default_factory=set)
    inspect_searches: int = 0
    inspect_reads: int = 0
    warning_count: int = 0
    error_count: int = 0
    blocked_count: int = 0
    recovery_count: int = 0
    review_outcome: str = ""
    review_finding_count: int = 0
    completed_with_limitations: bool = False
    waiting: bool = False
    finished: bool = False
    last_activity_at: float = field(default_factory=time.monotonic)
    _pending: list[_PendingOperation] = field(default_factory=list)
    _seen_event_ids: set[str] = field(default_factory=set)
    _version: int = 0

    @property
    def phase_icon(self) -> str:
        return _PHASES.get(self.phase, _PHASES["execution"])[0]

    @property
    def phase_label(self) -> str:
        return _PHASES.get(self.phase, _PHASES["execution"])[1]

    @property
    def version(self) -> int:
        return self._version

    def _touch(self, now: float | None = None) -> None:
        self._version += 1
        self.last_activity_at = time.monotonic() if now is None else now
        self.waiting = False

    def _set_phase(self, value: Any) -> bool:
        phase = _normalise_phase(value)
        if not phase or phase == self.phase:
            return False
        self.phase = phase
        return True

    def _record_operation(
        self,
        category: str,
        *,
        subtype: str = "",
        paths: set[str] | None = None,
        tool_name: str = "",
        pending: bool = False,
    ) -> None:
        self.operations[category] += 1
        if category == CATEGORY_INSPECT:
            self.inspect_files.update(paths or ())
            if subtype == "search":
                self.inspect_searches += 1
            else:
                self.inspect_reads += 1
        elif category == CATEGORY_CHANGE:
            self.changed_files.update(paths or ())
        elif category == CATEGORY_RECOVERY:
            self.recovery_count += 1
        if pending:
            self._pending.append(_PendingOperation(category, tool_name.casefold()))

    def _complete_operation(self, event: StreamEvent) -> None:
        tool_name = str(getattr(event, "tool_name", "") or "").casefold()
        index = next(
            (
                index
                for index in range(len(self._pending) - 1, -1, -1)
                if not tool_name or self._pending[index].tool_name in {"", tool_name}
            ),
            None,
        )
        if index is None:
            category, _subtype = _classify_tool(event)
        else:
            category = self._pending.pop(index).category
        outcome = _tool_outcome(event)
        if outcome == "success":
            self.completions[category] += 1
        elif outcome == "error":
            self.failures[category] += 1
            self.error_count += 1
        elif outcome == "blocked":
            self.blocked_count += 1

    def record(self, event: StreamEvent, *, now: float | None = None) -> bool:
        """Record an event and return whether the rendered digest may change."""

        event_id = str(getattr(event, "event_id", "") or "").strip()
        if event_id and event_id in self._seen_event_ids:
            return False
        if event_id:
            self._seen_event_ids.add(event_id)

        before = self._version
        metadata = _metadata(event)
        raw_phase = (
            metadata.get("lifecycle_state")
            or metadata.get("stage")
            or getattr(event, "phase", "")
        )
        explicit_phase = _normalise_phase(raw_phase)
        phase_changed = self._set_phase(raw_phase)
        kind = str(getattr(event, "kind", "") or "")
        activity_type = str(metadata.get("activity_type") or "").casefold()

        if activity_type in {"stage", "lifecycle"}:
            if metadata.get("terminal") is True:
                self.finished = True
                lifecycle_state = str(metadata.get("lifecycle_state") or "").upper()
                self.completed_with_limitations = (
                    lifecycle_state == "COMPLETED_WITH_LIMITATIONS"
                )
            self._touch(now)
            return True

        if kind == KIND_TOOL_START:
            summary = str(getattr(event, "summary", "") or "").strip()
            if summary.startswith("...") and not getattr(event, "tool_name", ""):
                if phase_changed:
                    self._touch(now)
                return phase_changed

        if (
            not explicit_phase
            and self.phase in {"preparing", "planning"}
            and kind
            in {
                KIND_FILE_READ,
                KIND_FILE_EDIT,
                KIND_SHELL_EXEC,
                KIND_TOOL_START,
                KIND_TOOL_END,
                KIND_TESTING,
                KIND_VALIDATION,
            }
        ):
            self.phase = "execution"
            phase_changed = True

        if kind == KIND_FILE_READ:
            self._record_operation(
                CATEGORY_INSPECT,
                subtype="read",
                paths=_event_paths(event),
                tool_name=str(getattr(event, "tool_name", "") or ""),
            )
        elif kind == KIND_FILE_EDIT:
            self._record_operation(
                CATEGORY_CHANGE,
                paths=_event_paths(event),
                tool_name=str(getattr(event, "tool_name", "") or ""),
            )
        elif kind == KIND_SHELL_EXEC:
            category, subtype = _classify_command(_command_text(event))
            self._record_operation(
                category,
                subtype=subtype,
                tool_name=str(getattr(event, "tool_name", "") or ""),
                pending=True,
            )
        elif kind == KIND_TOOL_START:
            category, subtype = _classify_tool(event)
            self._record_operation(
                category,
                subtype=subtype,
                paths=_event_paths(event),
                tool_name=str(getattr(event, "tool_name", "") or ""),
                pending=True,
            )
        elif kind == KIND_TOOL_END:
            self._complete_operation(event)
        elif kind in {KIND_TESTING, KIND_VALIDATION}:
            self._record_operation(CATEGORY_CHECK)
            outcome = _tool_outcome(event)
            if outcome == "success":
                self.completions[CATEGORY_CHECK] += 1
            elif outcome == "error":
                self.failures[CATEGORY_CHECK] += 1
                self.error_count += 1
        elif kind == KIND_REVIEW:
            self._set_phase("review")
            outcome = str(metadata.get("outcome") or "").strip().upper()
            if outcome:
                self.review_outcome = outcome
                try:
                    self.review_finding_count = max(
                        0, int(metadata.get("finding_count") or 0)
                    )
                except (TypeError, ValueError):
                    self.review_finding_count = 0
        elif kind == KIND_ERROR:
            self.error_count += 1
            if metadata.get("terminal") is True:
                self._set_phase("error")
        elif kind == KIND_PROGRESS:
            summary = str(getattr(event, "summary", "") or "").casefold()
            if activity_type == "recovery":
                self._record_operation(CATEGORY_RECOVERY)
                status = str(metadata.get("status") or "").casefold()
                if status in {"failed", "warning"}:
                    self.warning_count += 1
            elif (
                not sum(self.operations.values())
                and any(marker in summary for marker in ("updated task list", "planning started"))
            ):
                self._set_phase("planning")
            elif any(marker in summary for marker in ("retry", "recover", "replan", "failover", "compaction")):
                self._record_operation(CATEGORY_RECOVERY)
                if any(marker in summary for marker in ("failed", "warning", "did not complete")):
                    self.warning_count += 1
            elif any(marker in summary for marker in ("waiting", "still working", "long-running")):
                self.waiting = True
                self._version += 1
                self.last_activity_at = time.monotonic() if now is None else now
                return True
        else:
            if phase_changed:
                self._touch(now)
            return phase_changed

        self._touch(now)
        return self._version != before or phase_changed

    def mark_waiting(self, *, now: float | None = None) -> bool:
        del now
        if self.waiting:
            return False
        self.waiting = True
        self._version += 1
        return True

    def mark_finished(self) -> None:
        self.finished = True
        self.waiting = False
        if self.phase not in {"error", "blocked"}:
            self.phase = "completed"
        self._version += 1

    def _result_suffix(self, category: str) -> str:
        failures = self.failures[category]
        if failures:
            return f" · {_count_phrase(failures, 'failure')} ❌"
        operations = self.operations[category]
        completions = self.completions[category]
        if operations and completions >= operations:
            return " · completed ✅"
        return ""

    def render_lines(self) -> list[str]:
        lines: list[str] = []
        inspect_operations = self.operations[CATEGORY_INSPECT]
        if inspect_operations:
            if self.inspect_files:
                detail = f"Inspected {_count_phrase(len(self.inspect_files), 'file')}"
                if inspect_operations > len(self.inspect_files):
                    detail += (
                        f" across {_count_phrase(inspect_operations, 'operation')}"
                    )
            else:
                detail = (
                    f"Performed {_count_phrase(inspect_operations, 'inspection')}"
                )
            if self.inspect_searches:
                detail += f" · {_count_phrase(self.inspect_searches, 'search', 'searches')}"
            lines.append(f"🔎 {detail}")

        change_operations = self.operations[CATEGORY_CHANGE]
        if change_operations:
            if self.changed_files:
                detail = f"Changed {_count_phrase(len(self.changed_files), 'file')}"
            else:
                detail = f"Performed {_count_phrase(change_operations, 'file change')}"
            lines.append(f"📝 {detail}")

        execute_operations = self.operations[CATEGORY_EXECUTE]
        if execute_operations:
            lines.append(
                f"⚙️ Ran {_count_phrase(execute_operations, 'command')}"
                f"{self._result_suffix(CATEGORY_EXECUTE)}"
            )

        check_operations = self.operations[CATEGORY_CHECK]
        if check_operations:
            lines.append(
                f"🧪 Ran {_count_phrase(check_operations, 'check')}"
                f"{self._result_suffix(CATEGORY_CHECK)}"
            )

        external_operations = self.operations[CATEGORY_EXTERNAL]
        if external_operations:
            lines.append(
                f"🌐 Used {_count_phrase(external_operations, 'external operation')}"
                f"{self._result_suffix(CATEGORY_EXTERNAL)}"
            )

        if self.recovery_count:
            lines.append(
                f"🔁 Performed {_count_phrase(self.recovery_count, 'recovery action')}"
            )

        if self.review_outcome:
            if self.review_outcome == "PASS":
                lines.append("✅ Review passed")
            elif self.review_outcome == "CONDITIONAL_PASS":
                lines.append("⚠️ Review passed with limitations")
            elif self.review_outcome == "FAIL":
                count = self.review_finding_count
                if count:
                    lines.append(f"❌ Review found {_count_phrase(count, 'issue')}")
                else:
                    lines.append("❌ Review found issues")
            else:
                lines.append(f"⚠️ Review {self.review_outcome.casefold().replace('_', ' ')}")

        if self.completed_with_limitations:
            lines.append("⚠️ Completed with limitations")

        if self.blocked_count:
            lines.append(
                f"⛔ {_count_phrase(self.blocked_count, 'operation')} blocked"
            )
        if self.error_count and not sum(self.failures.values()):
            lines.append(f"❌ {_count_phrase(self.error_count, 'error')} reported")
        if self.warning_count:
            lines.append(f"⚠️ {_count_phrase(self.warning_count, 'warning')} reported")
        if self.waiting and not self.finished:
            lines.append("⏳ Waiting for a long-running operation")
        if not lines and not self.finished:
            lines.append("⏳ Preparing the next step")
        return lines
