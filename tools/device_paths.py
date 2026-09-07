"""Explicit Windows/WSL path handoff for platform capability Workers."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


_DRIVE_PATH = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<tail>.*)$")
_WSL_SHARE = re.compile(
    r"^\\\\(?:wsl\.localhost|wsl\$)\\(?P<distro>[^\\]+)(?:\\(?P<tail>.*))?$",
    re.IGNORECASE,
)
_PATH_KEYS = frozenset({"file_path", "save_path"})


class DevicePathError(ValueError):
    """A cross-platform path is malformed or outside authorized roots."""


@dataclass(frozen=True)
class ResolvedDevicePath:
    original: str
    windows_path: str | None
    wsl_path: str | None
    target_path: str
    authorized_root: str


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    if not text:
        raise DevicePathError("device path is required")
    if "\x00" in text:
        raise DevicePathError("device path contains a NUL byte")
    return text


def normalize_windows_path(value: Any) -> str:
    text = _clean(value).replace("/", "\\")
    if not (_DRIVE_PATH.match(text) or text.startswith("\\\\")):
        raise DevicePathError(f"Windows path must be absolute: {text}")
    normalized = ntpath.normpath(text)
    if _DRIVE_PATH.match(normalized):
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


def normalize_wsl_path(value: Any) -> str:
    text = _clean(value).replace("\\", "/")
    if not text.startswith("/"):
        raise DevicePathError(f"WSL path must be absolute: {text}")
    return posixpath.normpath(text)


def windows_to_wsl_path(value: Any, *, distro: str | None = None) -> str | None:
    path = normalize_windows_path(value)
    drive = _DRIVE_PATH.match(path)
    if drive:
        tail = drive.group("tail").replace("\\", "/")
        return posixpath.normpath(f"/mnt/{drive.group('drive').lower()}/{tail}")
    share = _WSL_SHARE.match(path)
    if share:
        expected = str(distro or "").strip().casefold()
        actual = share.group("distro").strip().casefold()
        if expected and expected != actual:
            return None
        tail = str(share.group("tail") or "").replace("\\", "/")
        return posixpath.normpath("/" + tail)
    return None


def wsl_to_windows_path(value: Any, *, distro: str | None = None) -> str:
    path = normalize_wsl_path(value)
    match = re.match(r"^/mnt/(?P<drive>[A-Za-z])(?:/(?P<tail>.*))?$", path)
    if match:
        tail = str(match.group("tail") or "").replace("/", "\\")
        suffix = f"\\{tail}" if tail else "\\"
        return f"{match.group('drive').upper()}:{suffix}"
    resolved_distro = str(distro or os.environ.get("WSL_DISTRO_NAME") or "").strip()
    if not resolved_distro:
        raise DevicePathError(
            "a WSL distribution identity is required for non-mounted Linux paths"
        )
    tail = path.lstrip("/").replace("/", "\\")
    return rf"\\wsl.localhost\{resolved_distro}\{tail}".rstrip("\\")


def _representations(
    value: Any,
    *,
    distro: str | None,
) -> tuple[str | None, str | None]:
    text = _clean(value)
    if text.startswith("/"):
        wsl_path = normalize_wsl_path(text)
        return wsl_to_windows_path(wsl_path, distro=distro), wsl_path
    windows_path = normalize_windows_path(text)
    return windows_path, windows_to_wsl_path(windows_path, distro=distro)


def _windows_within(path: str, root: str) -> bool:
    try:
        return ntpath.commonpath([path.casefold(), root.casefold()]) == root.casefold()
    except ValueError:
        return False


def _wsl_within(path: str, root: str) -> bool:
    try:
        return posixpath.commonpath([path, root]) == root
    except ValueError:
        return False


def resolve_device_path(
    value: Any,
    *,
    authorized_roots: Iterable[str | Path],
    target_platform: str,
    distro: str | None = None,
    require_exists: bool = False,
) -> ResolvedDevicePath:
    """Resolve one path and prove it stays within an explicit authority root."""

    original = _clean(value)
    windows_path, wsl_path = _representations(original, distro=distro)
    roots = [_clean(root) for root in authorized_roots]
    if not roots:
        raise DevicePathError("device path handoff has no authorized roots")

    authorized_root = ""
    for raw_root in roots:
        try:
            root_windows, root_wsl = _representations(raw_root, distro=distro)
        except DevicePathError:
            continue
        if (
            windows_path
            and root_windows
            and _windows_within(windows_path, root_windows)
        ) or (wsl_path and root_wsl and _wsl_within(wsl_path, root_wsl)):
            authorized_root = raw_root
            break
    if not authorized_root:
        raise DevicePathError(
            f"device path is outside the authorized roots: {original}"
        )

    platform_name = str(target_platform or "").strip().casefold()
    if platform_name.startswith("win"):
        if windows_path is None:
            raise DevicePathError(f"path has no Windows representation: {original}")
        target_path = windows_path
    elif platform_name in {"linux", "wsl", "posix"}:
        if wsl_path is None:
            raise DevicePathError(f"path has no WSL representation: {original}")
        target_path = wsl_path
    else:
        raise DevicePathError(f"unsupported device target platform: {target_platform}")

    if require_exists and not Path(target_path).exists():
        raise DevicePathError(f"device input path does not exist: {target_path}")
    return ResolvedDevicePath(
        original=original,
        windows_path=windows_path,
        wsl_path=wsl_path,
        target_path=target_path,
        authorized_root=authorized_root,
    )


def resolve_device_path_arguments(
    arguments: Mapping[str, Any] | None,
    *,
    target_platform: str,
    distro: str | None = None,
    require_inputs_exist: bool = True,
) -> dict[str, Any]:
    """Resolve file-bearing action arguments, including browser session steps."""

    payload = dict(arguments or {})
    roots = payload.pop("_authorized_roots", ())
    if isinstance(roots, (str, Path)):
        roots = [roots]

    def resolve_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            if key == "_authorized_roots":
                continue
            if key in _PATH_KEYS and item:
                resolved[key] = resolve_device_path(
                    item,
                    authorized_roots=roots,
                    target_platform=target_platform,
                    distro=distro,
                    require_exists=(key == "file_path" and require_inputs_exist),
                ).target_path
            elif key == "files" and isinstance(item, list):
                resolved[key] = [
                    resolve_device_path(
                        path,
                        authorized_roots=roots,
                        target_platform=target_platform,
                        distro=distro,
                        require_exists=require_inputs_exist,
                    ).target_path
                    for path in item
                ]
            elif isinstance(item, Mapping):
                resolved[key] = resolve_mapping(item)
            elif isinstance(item, list):
                resolved[key] = [
                    resolve_mapping(child) if isinstance(child, Mapping) else child
                    for child in item
                ]
            else:
                resolved[key] = item
        return resolved

    return resolve_mapping(payload)
