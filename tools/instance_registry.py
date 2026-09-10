"""User-scoped HASHI instance registry.

The installed program is shared inside one operating-system environment while
configuration, credentials, workspaces, logs, and process state remain inside
an instance-specific ``bridge_home``.  This module is deliberately stdlib-only
so ``hashi instance ...`` can still diagnose an incomplete product runtime.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

REGISTRY_SCHEMA_VERSION = 1
INSTANCE_MARKER_SCHEMA_VERSION = 1
REGISTRY_LOCK_WAIT_SECONDS = 10.0
REGISTRY_LOCK_STALE_SECONDS = 60.0
INSTANCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InstanceRegistryError(RuntimeError):
    """Registry data is invalid or a requested operation is unsafe."""


class InstanceSelectionError(InstanceRegistryError):
    """No unambiguous instance can be selected."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_environment_id() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        distro = str(os.environ.get("WSL_DISTRO_NAME") or "").strip()
        if not distro:
            try:
                release = Path("/proc/sys/kernel/osrelease").read_text(
                    encoding="utf-8"
                )
            except OSError:
                release = ""
            if "microsoft" in release.casefold():
                distro = "unknown"
        return f"wsl:{distro.casefold()}" if distro else "linux"
    return sys.platform.casefold()


def default_data_root() -> Path:
    explicit = str(os.environ.get("HASHI_DATA_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if sys.platform == "win32":
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return (base / "HASHI").resolve()
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "hashi").resolve()


def default_registry_root() -> Path:
    explicit = str(os.environ.get("HASHI_REGISTRY_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    if sys.platform == "win32":
        return default_data_root()
    xdg = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / "hashi").resolve()


def instance_key(name: str) -> str:
    selected = str(name or "").strip()
    if INSTANCE_NAME_PATTERN.fullmatch(selected) is None:
        raise InstanceRegistryError(
            "Instance names must be 1-64 ASCII letters, digits, '.', '_' or '-', "
            "and must begin with a letter or digit."
        )
    return selected.casefold()


def _canonical_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _lexical_absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _path_identity(value: str | Path) -> str:
    rendered = str(_canonical_path(value))
    return os.path.normcase(rendered) if sys.platform == "win32" else rendered


def _empty_registry(environment_id: str) -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "environment_id": environment_id,
        "default_instance": None,
        "instances": {},
        "bindings": {},
        "removed": {},
    }


def _atomic_json(path: Path, payload: dict[str, Any], *, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if private:
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{uuid4().hex}"
    deadline = time.monotonic() + REGISTRY_LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, token.encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > REGISTRY_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise InstanceRegistryError("Instance registry is busy; try again.")
            time.sleep(0.025)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if path.read_text(encoding="ascii") == token:
                path.unlink()
        except OSError:
            pass


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstanceRegistryError(f"Cannot read JSON file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InstanceRegistryError(f"JSON object required in {path}")
    return payload


def configured_identity(code_root: Path, bridge_home: Path) -> tuple[str, int, int]:
    config_path = bridge_home / "agents.json"
    if not config_path.is_file():
        config_path = code_root / "agents.json"
    payload = _read_json_object(config_path) if config_path.is_file() else None
    global_config = payload.get("global") if isinstance(payload, dict) else None
    global_config = global_config if isinstance(global_config, dict) else {}
    runtime_id = str(global_config.get("instance_id") or "HASHI").strip() or "HASHI"
    try:
        api_port = int(global_config.get("workbench_port") or 18800)
    except (TypeError, ValueError):
        api_port = 18800
    try:
        gateway_port = int(global_config.get("api_gateway_port") or (api_port + 1))
    except (TypeError, ValueError):
        gateway_port = api_port + 1
    return runtime_id, api_port, gateway_port


def _port_available(port: int) -> bool:
    if not 1 <= int(port) <= 65534:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class InstanceRegistry:
    def __init__(
        self,
        *,
        program_root: str | Path,
        program_version: str,
        registry_root: str | Path | None = None,
        data_root: str | Path | None = None,
        environment_id: str | None = None,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.program_root = _canonical_path(program_root)
        self.program_version = str(program_version or "unknown").strip() or "unknown"
        self.registry_root = _canonical_path(registry_root or default_registry_root())
        self.data_root = _canonical_path(data_root or default_data_root())
        self.environment_id = environment_id or current_environment_id()
        self.now = now
        self.path = self.registry_root / "instances.json"
        self.lock_path = self.registry_root / "instances.json.lock"
        self.instances_root = self.data_root / "instances"
        self.trash_root = self.data_root / "trash"
        for label, root in (
            ("registry", self.registry_root),
            ("instance data", self.data_root),
        ):
            if root == self.program_root or root.is_relative_to(self.program_root):
                raise InstanceRegistryError(
                    f"HASHI {label} root must be outside the installed program directory."
                )

    def _validate_record(self, key: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise InstanceRegistryError(f"Invalid instance record: {key}")
        name = str(value.get("name") or "")
        if instance_key(name) != key:
            raise InstanceRegistryError(f"Invalid instance name key: {key}")
        instance_id = str(value.get("instance_id") or "").strip()
        if not instance_id:
            raise InstanceRegistryError(f"Missing instance id for {name}")
        raw_code_root = str(value.get("code_root") or "").strip()
        raw_bridge_home = str(value.get("bridge_home") or "").strip()
        if not raw_code_root or not raw_bridge_home:
            raise InstanceRegistryError(f"Missing instance paths for {name}")
        code_root = _canonical_path(raw_code_root)
        lexical_home = _lexical_absolute(raw_bridge_home)
        bridge_home = _canonical_path(raw_bridge_home)
        managed = value.get("managed") is True
        expected_managed_home = (self.instances_root / key).resolve()
        if managed and (
            lexical_home != expected_managed_home
            or lexical_home.is_symlink()
            or bridge_home.parent != self.instances_root.resolve()
        ):
            raise InstanceRegistryError(
                f"Managed instance {name} does not match its exact data directory."
            )
        return {
            **value,
            "name": name,
            "instance_id": instance_id,
            "code_root": str(code_root),
            "bridge_home": str(bridge_home),
            "managed": managed,
        }

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_registry(self.environment_id)
        payload = _read_json_object(self.path)
        if (
            payload is None
            or payload.get("schema_version") != REGISTRY_SCHEMA_VERSION
            or payload.get("environment_id") != self.environment_id
            or not isinstance(payload.get("instances"), dict)
            or not isinstance(payload.get("bindings"), dict)
            or not isinstance(payload.get("removed"), dict)
        ):
            raise InstanceRegistryError(
                "Instance registry schema/environment mismatch; refusing to reset it."
            )
        payload["instances"] = {
            key: self._validate_record(str(key), value)
            for key, value in payload["instances"].items()
        }
        default = payload.get("default_instance")
        if default is not None and default not in payload["instances"]:
            raise InstanceRegistryError("Default instance points to a missing record.")
        for binding, key in payload["bindings"].items():
            if not Path(str(binding)).is_absolute() or key not in payload["instances"]:
                raise InstanceRegistryError("Invalid current-directory binding.")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        _atomic_json(self.path, payload)

    def records(self) -> list[dict[str, Any]]:
        payload = self.load()
        default = payload.get("default_instance")
        result = []
        for key in sorted(payload["instances"]):
            item = dict(payload["instances"][key])
            item["key"] = key
            item["default"] = key == default
            item["update_pending"] = bool(
                item.get("managed")
                and item.get("adopted_program_version") != self.program_version
            )
            result.append(item)
        return result

    def get(self, name: str) -> dict[str, Any]:
        key = instance_key(name)
        record = self.load()["instances"].get(key)
        if record is None:
            raise InstanceRegistryError(f"Unknown HASHI instance: {name}")
        return dict(record)

    def resolved_code_root(self, record: dict[str, Any]) -> Path:
        return (
            self.program_root
            if record.get("managed")
            else _canonical_path(record["code_root"])
        )

    def _used_ports(self, payload: dict[str, Any]) -> set[int]:
        used: set[int] = set()
        for value in payload["instances"].values():
            try:
                used.add(int(value.get("api_port")))
                used.add(int(value.get("gateway_port")))
            except (TypeError, ValueError):
                pass
        return used

    def _allocate_ports(self, payload: dict[str, Any]) -> tuple[int, int]:
        used = self._used_ports(payload)
        for api_port in range(18800, 20000, 2):
            gateway_port = api_port + 1
            if (
                api_port not in used
                and gateway_port not in used
                and _port_available(api_port)
                and _port_available(gateway_port)
            ):
                return api_port, gateway_port
        raise InstanceRegistryError("No free HASHI API port pair is available.")

    def create(
        self,
        name: str,
        *,
        source_root: str | Path | None = None,
        bridge_home: str | Path | None = None,
        bind_path: str | Path | None = None,
        make_default: bool = False,
    ) -> dict[str, Any]:
        key = instance_key(name)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            if key in payload["instances"]:
                raise InstanceRegistryError(f"Instance already exists: {name}")
            created_at = self.now()
            record_id = uuid4().hex
            created_home: Path | None = None
            if source_root is None:
                if bridge_home is not None:
                    raise InstanceRegistryError(
                        "--home is only valid together with --from for existing instances."
                    )
                code_root = self.program_root
                home = (self.instances_root / key).resolve()
                if home.exists():
                    raise InstanceRegistryError(
                        f"Managed instance directory already exists: {home}"
                    )
                api_port, gateway_port = self._allocate_ports(payload)
                home.mkdir(parents=True, exist_ok=False)
                created_home = home
                marker = {
                    "schema_version": INSTANCE_MARKER_SCHEMA_VERSION,
                    "record_id": record_id,
                    "name": name,
                    "environment_id": self.environment_id,
                    "created_at": created_at,
                }
                try:
                    _atomic_json(home / ".hashi-instance.json", marker)
                    _atomic_json(
                        home / "agents.json",
                        {
                            "global": {
                                "instance_id": name,
                                "ui_language": "en",
                                "workbench_port": api_port,
                                "api_gateway_port": gateway_port,
                            },
                            "agents": [],
                        },
                    )
                except Exception:
                    if created_home.is_relative_to(self.instances_root):
                        shutil.rmtree(created_home, ignore_errors=True)
                    raise
                runtime_id = name
                managed = True
                source_kind = "managed"
            else:
                code_root = _canonical_path(source_root)
                home = _canonical_path(bridge_home or code_root)
                if home == self.program_root or home.is_relative_to(self.program_root):
                    raise InstanceRegistryError(
                        "An instance bridge home must be outside the installed program directory."
                    )
                if (
                    not (code_root / "main.py").is_file()
                    or not (code_root / "runtime-entry.json").is_file()
                    or not (code_root / "scripts" / "check_runtime_contract.py").is_file()
                ):
                    raise InstanceRegistryError(
                        f"Existing code root is not a HASHI checkout: {code_root}"
                    )
                if not home.is_dir() or not (home / "agents.json").is_file():
                    raise InstanceRegistryError(
                        "Existing bridge home has no agents.json; registration made no changes."
                    )
                runtime_id, api_port, gateway_port = configured_identity(code_root, home)
                managed = False
                source_kind = "git" if (code_root / ".git").exists() else "external"

            for existing in payload["instances"].values():
                if _path_identity(existing["bridge_home"]) == _path_identity(home):
                    if created_home is not None:
                        shutil.rmtree(created_home, ignore_errors=True)
                    raise InstanceRegistryError(
                        f"Bridge home is already registered as {existing['name']}."
                    )
                if str(existing.get("instance_id") or "").casefold() == str(
                    runtime_id
                ).casefold():
                    if created_home is not None:
                        shutil.rmtree(created_home, ignore_errors=True)
                    raise InstanceRegistryError(
                        f"Runtime identity is already registered as {existing['name']}."
                    )
            if not (
                1 <= int(api_port) <= 65534
                and 1 <= int(gateway_port) <= 65535
                and int(api_port) != int(gateway_port)
            ):
                if created_home is not None:
                    shutil.rmtree(created_home, ignore_errors=True)
                raise InstanceRegistryError("Instance API ports are invalid.")
            if not managed and {int(api_port), int(gateway_port)}.intersection(
                self._used_ports(payload)
            ):
                raise InstanceRegistryError(
                    "Instance API ports conflict with another registered instance."
                )

            record = {
                "record_id": record_id,
                "name": name,
                "instance_id": runtime_id,
                "code_root": str(code_root),
                "bridge_home": str(home),
                "managed": managed,
                "source_kind": source_kind,
                "api_port": api_port,
                "gateway_port": gateway_port,
                "created_at": created_at,
                "adopted_program_version": (
                    self.program_version if managed else None
                ),
            }
            payload["instances"][key] = record
            paths_to_bind = []
            if source_root is not None:
                paths_to_bind.extend((code_root, home))
            if bind_path is not None:
                paths_to_bind.append(_canonical_path(bind_path))
            for path in paths_to_bind:
                payload["bindings"][_path_identity(path)] = key
            if make_default:
                payload["default_instance"] = key
            try:
                self._write(payload)
            except Exception:
                if created_home is not None and created_home.is_relative_to(
                    self.instances_root
                ):
                    shutil.rmtree(created_home, ignore_errors=True)
                raise
            return dict(record)

    def set_default(self, name: str) -> dict[str, Any]:
        key = instance_key(name)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            if key not in payload["instances"]:
                raise InstanceRegistryError(f"Unknown HASHI instance: {name}")
            payload["default_instance"] = key
            self._write(payload)
            return dict(payload["instances"][key])

    def bind(self, name: str, path: str | Path) -> dict[str, Any]:
        key = instance_key(name)
        canonical = _canonical_path(path)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            if key not in payload["instances"]:
                raise InstanceRegistryError(f"Unknown HASHI instance: {name}")
            payload["bindings"][_path_identity(canonical)] = key
            self._write(payload)
            return dict(payload["instances"][key])

    def unbind(self, path: str | Path) -> dict[str, Any]:
        canonical = _path_identity(path)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            previous = payload["bindings"].pop(canonical, None)
            if previous is not None:
                self._write(payload)
            return {"path": canonical, "previous_instance": previous, "removed": previous is not None}

    def adopt(self, name: str) -> dict[str, Any]:
        key = instance_key(name)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            record = payload["instances"].get(key)
            if record is None:
                raise InstanceRegistryError(f"Unknown HASHI instance: {name}")
            if not record.get("managed"):
                raise InstanceRegistryError(
                    "External/Git instances adopt code through their own checkout."
                )
            record["adopted_program_version"] = self.program_version
            record["code_root"] = str(self.program_root)
            record["adopted_at"] = self.now()
            self._write(payload)
            return dict(record)

    def select(
        self,
        *,
        explicit: str | None = None,
        cwd: str | Path | None = None,
        interactive: bool = False,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> tuple[dict[str, Any], str]:
        payload = self.load()
        instances = payload["instances"]
        if explicit:
            key = instance_key(explicit)
            if key not in instances:
                raise InstanceSelectionError(f"Unknown HASHI instance: {explicit}")
            return dict(instances[key]), "explicit"

        if cwd is not None:
            current = _canonical_path(cwd)
            matches: list[tuple[int, str]] = []
            for raw_path, key in payload["bindings"].items():
                bound = _canonical_path(raw_path)
                try:
                    current.relative_to(bound)
                except ValueError:
                    continue
                matches.append((len(bound.parts), key))
            if matches:
                key = max(matches)[1]
                return dict(instances[key]), "cwd_binding"

        default = payload.get("default_instance")
        if default:
            return dict(instances[default]), "default"
        if len(instances) == 1:
            return dict(next(iter(instances.values()))), "only_instance"
        if not instances:
            raise InstanceSelectionError("No HASHI instances are registered.")
        if not interactive:
            names = ", ".join(sorted(item["name"] for item in instances.values()))
            raise InstanceSelectionError(
                f"Multiple HASHI instances are available ({names}); use --instance."
            )
        ordered = [instances[key] for key in sorted(instances)]
        output_fn("Select a HASHI instance:")
        for index, record in enumerate(ordered, 1):
            output_fn(f"  [{index}] {record['name']} ({record['instance_id']})")
        raw = input_fn("> ").strip()
        try:
            index = int(raw) - 1
            if not 0 <= index < len(ordered):
                raise IndexError
            selected = ordered[index]
        except (ValueError, IndexError):
            raise InstanceSelectionError("Invalid HASHI instance selection.") from None
        return dict(selected), "interactive"

    def _verified_managed_home(self, record: dict[str, Any]) -> Path:
        lexical_home = _lexical_absolute(record["bridge_home"])
        home = _canonical_path(record["bridge_home"])
        expected = (self.instances_root / instance_key(record["name"])).resolve()
        if (
            lexical_home != expected
            or lexical_home.is_symlink()
            or home.parent != self.instances_root.resolve()
        ):
            raise InstanceRegistryError("Managed data path is not the exact instance root.")
        marker = _read_json_object(home / ".hashi-instance.json")
        if (
            marker is None
            or marker.get("schema_version") != INSTANCE_MARKER_SCHEMA_VERSION
            or marker.get("record_id") != record.get("record_id")
            or marker.get("name") != record.get("name")
            or marker.get("environment_id") != self.environment_id
        ):
            raise InstanceRegistryError(
                "Managed data marker does not match; refusing to move or delete it."
            )
        return home

    def remove(
        self,
        name: str,
        *,
        purge: bool = False,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        key = instance_key(name)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            record = payload["instances"].get(key)
            if record is None:
                raise InstanceRegistryError(f"Unknown HASHI instance: {name}")
            if purge and str(confirmation or "") != str(record["name"]):
                raise InstanceRegistryError(
                    "Permanent purge requires an exact --confirm <instance-name>."
                )
            if purge and not record.get("managed"):
                raise InstanceRegistryError(
                    "Refusing to purge external/Git data; unregister it and delete it manually."
                )

            removal_id = f"{int(time.time())}-{uuid4().hex[:12]}"
            moved_to: Path | None = None
            original_home: Path | None = None
            if record.get("managed"):
                original_home = self._verified_managed_home(record)
                moved_to = (self.trash_root / removal_id / "instance").resolve()
                if not moved_to.is_relative_to(self.trash_root) or moved_to.exists():
                    raise InstanceRegistryError("Unsafe or occupied recovery path.")
                moved_to.parent.mkdir(parents=True, exist_ok=False)
                original_home.replace(moved_to)

            removed = {
                "removal_id": removal_id,
                "record": dict(record),
                "removed_at": self.now(),
                "trash_path": str(moved_to) if moved_to is not None else None,
                "purged_at": None,
                "was_default": payload.get("default_instance") == key,
                "bindings": [
                    path
                    for path, bound_key in payload["bindings"].items()
                    if bound_key == key
                ],
            }
            del payload["instances"][key]
            payload["bindings"] = {
                path: bound_key
                for path, bound_key in payload["bindings"].items()
                if bound_key != key
            }
            if payload.get("default_instance") == key:
                payload["default_instance"] = None
            payload["removed"][removal_id] = removed
            try:
                self._write(payload)
            except Exception:
                if moved_to is not None and original_home is not None and moved_to.exists():
                    moved_to.replace(original_home)
                raise

            if purge and moved_to is not None:
                if not moved_to.is_relative_to(self.trash_root):
                    raise InstanceRegistryError("Refusing unsafe purge target.")
                shutil.rmtree(moved_to.parent)
                payload = self.load()
                payload["removed"][removal_id]["trash_path"] = None
                payload["removed"][removal_id]["purged_at"] = self.now()
                self._write(payload)
                removed = dict(payload["removed"][removal_id])
            return removed

    def restore(self, name: str) -> dict[str, Any]:
        key = instance_key(name)
        with _exclusive_lock(self.lock_path):
            payload = self.load()
            if key in payload["instances"]:
                raise InstanceRegistryError(f"Instance already exists: {name}")
            candidates = [
                value
                for value in payload["removed"].values()
                if instance_key(str((value.get("record") or {}).get("name") or ""))
                == key
                and not value.get("purged_at")
            ]
            if not candidates:
                raise InstanceRegistryError(f"No recoverable removal found for: {name}")
            removed = max(candidates, key=lambda item: str(item.get("removed_at") or ""))
            record = self._validate_record(key, removed.get("record"))
            trash_path = removed.get("trash_path")
            restored_home: Path | None = None
            if record.get("managed"):
                if not trash_path:
                    raise InstanceRegistryError("Managed recovery data is missing.")
                source = _canonical_path(trash_path)
                target = _canonical_path(record["bridge_home"])
                if (
                    not source.is_relative_to(self.trash_root)
                    or not target.is_relative_to(self.instances_root)
                    or not source.exists()
                    or target.exists()
                ):
                    raise InstanceRegistryError("Managed recovery paths are unsafe.")
                marker = _read_json_object(source / ".hashi-instance.json")
                if (
                    marker is None
                    or marker.get("schema_version")
                    != INSTANCE_MARKER_SCHEMA_VERSION
                    or marker.get("record_id") != record.get("record_id")
                    or marker.get("name") != record.get("name")
                    or marker.get("environment_id") != self.environment_id
                ):
                    raise InstanceRegistryError(
                        "Managed recovery marker does not match; refusing to restore it."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
                restored_home = target
            else:
                if not _canonical_path(record["bridge_home"]).exists():
                    raise InstanceRegistryError("External instance path no longer exists.")
            payload["instances"][key] = record
            for path in removed.get("bindings") or []:
                payload["bindings"][str(path)] = key
            if removed.get("was_default"):
                payload["default_instance"] = key
            del payload["removed"][removed["removal_id"]]
            try:
                self._write(payload)
            except Exception:
                if restored_home is not None:
                    restored_home.replace(_canonical_path(trash_path))
                raise
            return dict(record)


__all__ = [
    "InstanceRegistry",
    "InstanceRegistryError",
    "InstanceSelectionError",
    "configured_identity",
    "current_environment_id",
    "default_data_root",
    "default_registry_root",
    "instance_key",
]
