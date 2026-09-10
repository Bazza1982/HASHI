"""Portable continuity-state selection owned by the PCM functional layer.

The transfer package asks this module for an explicit plan.  Keeping the list
here prevents the transport layer from guessing that arbitrary files with
words such as ``memory`` or ``transcript`` belong to an Agent's continuity.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_CONTINUITY_ITEMS = 100_000


class PcmTransferError(ValueError):
    """Raised when continuity inventory cannot be bounded safely."""


@dataclass(frozen=True)
class PcmTransferItem:
    source: Path
    relative_path: str
    is_control: bool = False


@dataclass(frozen=True)
class PcmTransferExclusion:
    relative_path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.relative_path, "reason": self.reason}


@dataclass(frozen=True)
class PcmTransferPlan:
    items: tuple[PcmTransferItem, ...]
    excluded: tuple[PcmTransferExclusion, ...]


_CONTINUITY_FILES = (
    "agent.md",
    "bridge_memory.sqlite",
    "transcript.jsonl",
    "core_transcript.jsonl",
    "audit_transcript.jsonl",
    "recent_context.jsonl",
    "sys_prompts.json",
    "state.json",
    "post_turn_observers.json",
    "memory/memory_plus_state.json",
    "memory/memory_plus_index.json",
    "memory/memory_plus_notepad.md",
)
_MEMORY_PLUS_ARCHIVE = "memory/memory_plus_wiki"


def build_agent_continuity_plan(workspace_dir: Path | str) -> PcmTransferPlan:
    """Return the exact portable PCM/Memory+ continuity inventory.

    Symbolic links and Windows junctions are inventory diagnostics, never
    followed or materialised.  Missing optional continuity files are normal.
    """

    workspace = Path(workspace_dir)
    items: list[PcmTransferItem] = []
    excluded: list[PcmTransferExclusion] = []
    for relative in _CONTINUITY_FILES:
        _add_regular_candidate(
            workspace,
            relative,
            items,
            excluded,
            is_control=relative == "agent.md",
        )

    archive = workspace / Path(_MEMORY_PLUS_ARCHIVE)
    try:
        archive_stat = archive.lstat()
    except FileNotFoundError:
        archive_stat = None
    except OSError:
        excluded.append(PcmTransferExclusion(_MEMORY_PLUS_ARCHIVE, "unreadable"))
        archive_stat = None
    if archive_stat is not None:
        if _is_link_like(archive, archive_stat):
            excluded.append(
                PcmTransferExclusion(
                    _MEMORY_PLUS_ARCHIVE, "continuity_symlink_not_followed"
                )
            )
        elif stat.S_ISDIR(archive_stat.st_mode):
            _walk_archive(workspace, archive, items, excluded)
        else:
            excluded.append(
                PcmTransferExclusion(_MEMORY_PLUS_ARCHIVE, "unsupported_file_type")
            )

    return PcmTransferPlan(
        items=tuple(sorted(items, key=lambda item: item.relative_path)),
        excluded=tuple(sorted(excluded, key=lambda item: item.relative_path)),
    )


def _walk_archive(
    workspace: Path,
    archive: Path,
    items: list[PcmTransferItem],
    excluded: list[PcmTransferExclusion],
) -> None:
    for current, dir_names, file_names in os.walk(archive, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(dir_names):
            child = current_path / name
            relative = child.relative_to(workspace).as_posix()
            try:
                info = child.lstat()
            except OSError:
                _check_plan_limit(items, excluded)
                excluded.append(PcmTransferExclusion(relative, "unreadable"))
                continue
            if _is_link_like(child, info):
                _check_plan_limit(items, excluded)
                excluded.append(
                    PcmTransferExclusion(relative, "continuity_symlink_not_followed")
                )
            elif stat.S_ISDIR(info.st_mode):
                kept.append(name)
            else:
                _check_plan_limit(items, excluded)
                excluded.append(
                    PcmTransferExclusion(relative, "unsupported_file_type")
                )
        dir_names[:] = kept
        for name in sorted(file_names):
            relative = (current_path / name).relative_to(workspace).as_posix()
            _add_regular_candidate(workspace, relative, items, excluded)


def _add_regular_candidate(
    workspace: Path,
    relative: str,
    items: list[PcmTransferItem],
    excluded: list[PcmTransferExclusion],
    *,
    is_control: bool = False,
) -> None:
    _check_plan_limit(items, excluded)
    path = workspace / Path(relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        excluded.append(PcmTransferExclusion(relative, "unreadable"))
        return
    if _is_link_like(path, info):
        excluded.append(
            PcmTransferExclusion(relative, "continuity_symlink_not_followed")
        )
    elif stat.S_ISREG(info.st_mode):
        items.append(PcmTransferItem(path, relative, is_control=is_control))
    else:
        excluded.append(PcmTransferExclusion(relative, "unsupported_file_type"))


def _check_plan_limit(
    items: list[PcmTransferItem], excluded: list[PcmTransferExclusion]
) -> None:
    if len(items) + len(excluded) >= MAX_CONTINUITY_ITEMS:
        raise PcmTransferError("Agent continuity contains too many filesystem members")


def _is_link_like(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    checker = getattr(path, "is_junction", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except OSError:
        return True


def is_portable_memory_path(relative_path: str) -> bool:
    """Project the same PCM-owned scope for package receiver validation."""
    from pathlib import PurePosixPath
    path = PurePosixPath(relative_path)
    return path.as_posix() in _CONTINUITY_FILES or (
        len(path.parts) > 2 and path.parts[:2] == ("memory", "memory_plus_wiki")
    )
