"""Platform-aware presentation of local paths in user-visible text.

Execution paths and presentation paths are deliberately separate.  A HASHI
process running inside WSL must keep POSIX paths for tools and shell commands,
while a person using HASHI needs locations that can be pasted into Windows
Explorer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from orchestrator.process_execution import runtime_platform_name

WINDOWS_PRESENTATION_PLATFORMS = frozenset({"windows_native", "windows_wsl"})

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC_RE = re.compile(r"^\\\\")
_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")
_WSL_ABSOLUTE_RE = re.compile(
    r"^/(?:"
    r"mnt/[A-Za-z](?:/|$)|"
    r"home(?:/|$)|root(?:/|$)|tmp(?:/|$)|var(?:/|$)|"
    r"opt(?:/|$)|srv(?:/|$)|usr(?:/|$)|etc(?:/|$)|"
    r"workspace(?:/|$)|workspaces(?:/|$)|data(?:/|$)"
    r")"
)
_LOCAL_ABSOLUTE_TOKEN = (
    r"(?:"
    r"/(?:mnt/[A-Za-z]|home|root|tmp|var|opt|srv|usr|etc|workspace|workspaces|data)"
    r"(?:/[^\s`<>()\[\]{}\"'，。；：!?]+)+"
    r"|[A-Za-z]:[\\/][^\n`<>()\[\]{}\"']+"
    r")"
)
_MARKDOWN_ANGLE_LINK_RE = re.compile(
    rf"!?\[[^\]\n]*\]\(<(?P<path>{_LOCAL_ABSOLUTE_TOKEN})>\)"
)
_MARKDOWN_LINK_RE = re.compile(
    rf"!?\[[^\]\n]*\]\((?P<path>{_LOCAL_ABSOLUTE_TOKEN})\)"
)
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?P<value>[^`\n]+)`(?!`)")
_FENCED_BLOCK_RE = re.compile(
    r"(?P<open>```(?P<language>[^\n`]*)\n)(?P<body>[\s\S]*?)(?P<close>```)",
)
_LOCATION_CUE_RE = re.compile(
    r"(?P<prefix>"
    r"(?:\b(?:path|file|folder|directory|location|output|artifact)\b"
    r"|\b(?:saved|written|created|generated)(?:\s+(?:to|at|in))?\b"
    r"|(?:路径|文件|文件夹|目录|位置|保存到|写入|输出到))"
    r"\s*(?:is\s*)?(?:[:：=]\s*)?"
    r")"
    rf"(?P<path>{_LOCAL_ABSOLUTE_TOKEN})",
    re.IGNORECASE,
)
_STANDALONE_PATH_LINE_RE = re.compile(
    rf"^(?P<prefix>\s*(?:[-*]\s+)?)"
    rf"(?P<quote>`?)(?P<path>{_LOCAL_ABSOLUTE_TOKEN})(?P=quote)"
    rf"(?P<suffix>\s*)$"
)
_SOURCE_HASH_LINE_RE = re.compile(
    r"^(?P<path>.+?)#L(?P<line>\d+)(?:C(?P<column>\d+))?$"
)
_SOURCE_COLON_LINE_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?$"
)


def user_facing_path_style(platform_name: str | None = None) -> str:
    """Return the path family intended for local locations shown to a user."""

    platform_value = str(platform_name or runtime_platform_name()).strip().lower()
    return (
        "windows_explorer"
        if platform_value in WINDOWS_PRESENTATION_PLATFORMS
        else "native_posix"
    )


@lru_cache(maxsize=1)
def _detected_wsl_unc_root() -> str | None:
    """Ask WSL for its registered Explorer root when env metadata is absent."""

    executable = shutil.which("wslpath")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-w", "/"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = str(result.stdout or "").strip().rstrip("\\/")
    return value if result.returncode == 0 and value.startswith("\\\\") else None


def _wsl_unc_root(distro_name: str | None = None) -> str | None:
    distro = str(
        distro_name
        if distro_name is not None
        else os.environ.get("WSL_DISTRO_NAME") or ""
    ).strip()
    if distro:
        cleaned = distro.strip("\\/")
        return rf"\\wsl.localhost\{cleaned}"
    return _detected_wsl_unc_root()


def display_user_path(
    path: str | Path,
    *,
    platform_name: str | None = None,
    distro_name: str | None = None,
) -> str:
    """Render one local path in the platform's user-facing path style.

    Mounted Windows files become drive paths under WSL, while files inside the
    distribution become ``\\wsl.localhost`` paths.  Native POSIX hosts retain
    POSIX paths.  The result is presentation-only and must not be fed back into
    Linux tool execution.
    """

    value = str(path or "").strip()
    platform_value = str(platform_name or runtime_platform_name()).strip().lower()
    if not value:
        return value

    mounted = _WSL_MOUNT_RE.match(value)
    if mounted and platform_value in WINDOWS_PRESENTATION_PLATFORMS:
        drive = mounted.group(1).upper()
        tail = str(mounted.group(2) or "").replace("/", "\\")
        return f"{drive}:\\{tail}" if tail else f"{drive}:\\"

    if platform_value == "windows_native":
        if _WINDOWS_DRIVE_RE.match(value) or _WINDOWS_UNC_RE.match(value):
            return value.replace("/", "\\")
        return value
    if platform_value != "windows_wsl":
        return value
    if _WINDOWS_DRIVE_RE.match(value) or _WINDOWS_UNC_RE.match(value):
        return value.replace("/", "\\")

    if value.startswith("/"):
        root = _wsl_unc_root(distro_name)
        if root:
            tail = value.lstrip("/").replace("/", "\\")
            return f"{root}\\{tail}" if tail else root + "\\"
    return value


def path_presentation_policy(
    *,
    platform_name: str | None = None,
    distro_name: str | None = None,
) -> str:
    """Build the instance-owned path presentation contract for model prompts."""

    platform_value = str(platform_name or runtime_platform_name()).strip().lower()
    if platform_value == "windows_wsl":
        example = display_user_path(
            "/home/user/project/report.md",
            platform_name=platform_value,
            distro_name=distro_name,
        )
        return (
            "This instance runs in WSL on Windows. Keep POSIX/WSL paths for tool "
            "calls and WSL commands. For every user-facing local file or folder "
            "location, show a Windows Explorer path: use drive paths for "
            f"/mnt/<drive> and distro UNC paths for Linux-side files (for example, "
            f"`{example}`). Put each displayed location in code formatting and do "
            "not show a raw /home, /mnt, or other WSL path as the location."
        )
    if platform_value == "windows_native":
        return (
            "This HASHI instance runs on native Windows. For every user-facing "
            "local file or folder location, use a Windows Explorer path and put it "
            "in inline code or a fenced text block. Keep command paths appropriate "
            "for the command's declared shell."
        )
    return (
        "This HASHI instance runs on a native POSIX platform. Report local file and "
        "folder locations using native POSIX paths in inline code or fenced text "
        "blocks; keep command paths appropriate for the command's declared shell."
    )


def _looks_local_absolute(value: str) -> bool:
    return bool(
        _WSL_ABSOLUTE_RE.match(value)
        or _WINDOWS_DRIVE_RE.match(value)
        or _WINDOWS_UNC_RE.match(value)
    )


def _split_source_reference(value: str) -> tuple[str, str]:
    for pattern in (_SOURCE_HASH_LINE_RE, _SOURCE_COLON_LINE_RE):
        match = pattern.match(value)
        if not match:
            continue
        path = match.group("path")
        if not _looks_local_absolute(path):
            continue
        line = match.group("line")
        column = match.group("column")
        location = f" (line {line}"
        if column:
            location += f", column {column}"
        return path, location + ")"
    return value, ""


def _split_prose_punctuation(value: str) -> tuple[str, str]:
    match = re.match(r"^(?P<path>.*?)(?P<suffix>[.,;，。；]+)$", value)
    if not match:
        return value, ""
    return match.group("path"), match.group("suffix")


def _display_reference(
    value: str,
    *,
    platform_name: str,
    distro_name: str | None,
) -> str | None:
    path, source_suffix = _split_source_reference(value.strip())
    if not _looks_local_absolute(path):
        return None
    displayed = display_user_path(
        path,
        platform_name=platform_name,
        distro_name=distro_name,
    )
    if displayed == path:
        return None
    return f"`{displayed}`{source_suffix}"


def _reference_without_ticks(reference: str) -> str:
    if not reference.startswith("`"):
        return reference
    closing = reference.find("`", 1)
    return reference[1:closing] + reference[closing + 1 :] if closing >= 1 else reference


def _rewrite_fenced_block(
    match: re.Match[str],
    *,
    platform_name: str,
    distro_name: str | None,
) -> str:
    language = match.group("language").strip().lower()
    if language not in {"", "text", "plaintext", "path", "paths"}:
        return match.group(0)

    changed = False
    rendered_lines: list[str] = []
    for line in match.group("body").splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        line_match = _STANDALONE_PATH_LINE_RE.match(content)
        if not line_match:
            rendered_lines.append(line)
            continue
        reference = _display_reference(
            line_match.group("path"),
            platform_name=platform_name,
            distro_name=distro_name,
        )
        if reference is None:
            rendered_lines.append(line)
            continue
        rendered_lines.append(
            line_match.group("prefix")
            + _reference_without_ticks(reference)
            + line_match.group("suffix")
            + newline
        )
        changed = True
    if not changed:
        return match.group(0)
    return match.group("open") + "".join(rendered_lines) + match.group("close")


def normalize_user_visible_paths(
    text: str,
    *,
    platform_name: str | None = None,
    distro_name: str | None = None,
) -> str:
    """Rewrite clear local locations at the final presentation boundary.

    Executable code blocks are left alone.  This catches common model output
    forms such as local Markdown links, standalone paths, and ``Path: /...``
    prose without altering paths inside Bash, PowerShell, or Python commands.
    """

    value = str(text or "")
    platform_value = str(platform_name or runtime_platform_name()).strip().lower()
    if platform_value not in WINDOWS_PRESENTATION_PLATFORMS:
        return value
    if "/" not in value and "\\" not in value:
        return value

    fenced_blocks: list[str] = []

    def save_fenced(match: re.Match[str]) -> str:
        fenced_blocks.append(
            _rewrite_fenced_block(
                match,
                platform_name=platform_value,
                distro_name=distro_name,
            )
        )
        return f"\x00HASHI_PATH_FENCE_{len(fenced_blocks) - 1}\x00"

    rendered = _FENCED_BLOCK_RE.sub(save_fenced, value)

    def replace_link(match: re.Match[str]) -> str:
        return (
            _display_reference(
                match.group("path"),
                platform_name=platform_value,
                distro_name=distro_name,
            )
            or match.group(0)
        )

    rendered = _MARKDOWN_ANGLE_LINK_RE.sub(replace_link, rendered)
    rendered = _MARKDOWN_LINK_RE.sub(replace_link, rendered)

    def replace_inline(match: re.Match[str]) -> str:
        return (
            _display_reference(
                match.group("value"),
                platform_name=platform_value,
                distro_name=distro_name,
            )
            or match.group(0)
        )

    rendered = _INLINE_CODE_RE.sub(replace_inline, rendered)

    def replace_cued(match: re.Match[str]) -> str:
        path, punctuation = _split_prose_punctuation(match.group("path"))
        reference = _display_reference(
            path,
            platform_name=platform_value,
            distro_name=distro_name,
        )
        return match.group("prefix") + (reference or path) + punctuation

    rendered = _LOCATION_CUE_RE.sub(replace_cued, rendered)

    rendered_lines: list[str] = []
    for line in rendered.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        line_match = _STANDALONE_PATH_LINE_RE.match(content)
        if line_match:
            reference = _display_reference(
                line_match.group("path"),
                platform_name=platform_value,
                distro_name=distro_name,
            )
            if reference:
                content = (
                    line_match.group("prefix")
                    + reference
                    + line_match.group("suffix")
                )
        rendered_lines.append(content + newline)
    rendered = "".join(rendered_lines)

    for index, block in enumerate(fenced_blocks):
        rendered = rendered.replace(f"\x00HASHI_PATH_FENCE_{index}\x00", block)
    return rendered
