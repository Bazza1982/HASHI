from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.her_v2.models import effort_display_label
from orchestrator.her_v2.request_policy import (
    HER_V2_JOB_EFFORT_FIELD,
    job_effort_policy,
    normalize_job_effort,
    normalize_job_effort_in_place,
)
from orchestrator.job_ownership import ownership_mismatch_label


@dataclass
class SkillDefinition:
    id: str
    name: str
    description: str
    body: str
    skill_dir: Path
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None
    source_type: str = "project"
    source: str | None = None
    scope: str = "project"
    managed: bool = False
    installed_at: str | None = None
    content_sha256: str | None = None

    @property
    def version(self) -> str | None:
        return self.metadata.get("version") or None


class SkillValidationError(ValueError):
    """Raised when a HASHI Skill package violates the public package contract."""


class SkillManager:
    ACTIVE_HEARTBEAT_DEFAULT_MINUTES = 10
    SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    SKILL_FRONTMATTER_KEYS = frozenset(
        {
            "name",
            "description",
            "license",
            "compatibility",
            "metadata",
            "allowed-tools",
        }
    )
    RUNTIME_TOGGLE_IDS = frozenset({"debug", "recall"})

    def __init__(self, project_root: Path, tasks_path: Path):
        self.project_root = Path(project_root)
        self.skills_dir = self.project_root / "skills"
        self.tasks_path = Path(tasks_path)
        self.active_heartbeats_path = (
            self.project_root / "managed_active_heartbeats.json"
        )
        self.skill_registry_path = self.project_root / "state" / "skill_registry.json"
        self.skill_recovery_dir = self.project_root / "state" / "skill_recovery"
        self.skill_usage_path = self.project_root / "state" / "skill_usage.jsonl"
        self._skill_validation_errors: list[str] = []

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    def _parse_frontmatter(
        self, text: str, *, source: Path
    ) -> tuple[dict[str, Any], str]:
        lines = (text or "").splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillValidationError(
                f"{source}: SKILL.md must start with YAML frontmatter"
            )
        try:
            closing_index = next(
                index
                for index, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
        except StopIteration as exc:
            raise SkillValidationError(
                f"{source}: YAML frontmatter is not closed"
            ) from exc

        try:
            parsed = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
        except yaml.YAMLError as exc:
            raise SkillValidationError(
                f"{source}: invalid YAML frontmatter: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SkillValidationError(f"{source}: YAML frontmatter must be a mapping")

        unknown = sorted(str(key) for key in set(parsed) - self.SKILL_FRONTMATTER_KEYS)
        if unknown:
            raise SkillValidationError(
                f"{source}: unsupported frontmatter keys: {', '.join(str(key) for key in unknown)}"
            )
        for key in ("name", "description"):
            value = parsed.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SkillValidationError(
                    f"{source}: `{key}` must be a non-empty string"
                )
            parsed[key] = value.strip()

        optional_strings = ("license", "allowed-tools")
        for key in optional_strings:
            if key in parsed and not isinstance(parsed[key], str):
                raise SkillValidationError(f"{source}: `{key}` must be a string")

        if "compatibility" in parsed:
            compatibility = parsed["compatibility"]
            if not isinstance(compatibility, str):
                raise SkillValidationError(
                    f"{source}: `compatibility` must be a string"
                )
            if len(compatibility) > 500:
                raise SkillValidationError(
                    f"{source}: `compatibility` must be 500 characters or fewer"
                )

        if "metadata" in parsed:
            metadata = parsed["metadata"]
            if not isinstance(metadata, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()
            ):
                raise SkillValidationError(
                    f"{source}: `metadata` must map string keys to string values"
                )

        return parsed, "\n".join(lines[closing_index + 1 :]).strip()

    def _load_skill_definition(
        self,
        skill_dir: Path,
        *,
        registry_entry: dict[str, Any] | None = None,
    ) -> SkillDefinition:
        skill_md = skill_dir / "SKILL.md"
        frontmatter, body = self._parse_frontmatter(
            self._read_text(skill_md), source=skill_md
        )
        skill_id = frontmatter["name"]
        if len(skill_id) > 64:
            raise SkillValidationError(
                f"{skill_md}: `name` must be 64 characters or fewer"
            )
        if not self.SKILL_NAME_PATTERN.fullmatch(skill_id):
            raise SkillValidationError(
                f"{skill_md}: `name` must be lowercase kebab-case (letters, digits, hyphens)"
            )
        if skill_dir.name != skill_id:
            raise SkillValidationError(
                f"{skill_md}: directory `{skill_dir.name}` must match skill name `{skill_id}`"
            )
        if len(frontmatter["description"]) > 1024:
            raise SkillValidationError(
                f"{skill_md}: `description` must be 1024 characters or fewer"
            )
        if not body:
            raise SkillValidationError(
                f"{skill_md}: instruction body must not be empty"
            )
        registry_entry = registry_entry if isinstance(registry_entry, dict) else {}
        if registry_entry:
            source_type = str(registry_entry.get("source_type") or "installed")
            source = str(registry_entry.get("source") or skill_dir)
            scope = str(registry_entry.get("scope") or "project")
            managed = source_type in {"installed", "linked"}
            installed_at = str(registry_entry.get("installed_at") or "") or None
            content_sha256 = str(registry_entry.get("content_sha256") or "") or None
        elif skill_dir.is_symlink():
            source_type = "linked"
            try:
                source = str(skill_dir.resolve(strict=True))
            except OSError:
                source = str(skill_dir)
            scope = "project"
            managed = False
            installed_at = None
            content_sha256 = None
        else:
            source_type = "project"
            source = str(skill_dir)
            scope = "project"
            managed = False
            installed_at = None
            content_sha256 = None
        return SkillDefinition(
            id=skill_id,
            name=skill_id,
            description=frontmatter["description"],
            body=body,
            skill_dir=skill_dir,
            license=frontmatter.get("license"),
            compatibility=frontmatter.get("compatibility"),
            metadata=dict(frontmatter.get("metadata") or {}),
            allowed_tools=frontmatter.get("allowed-tools"),
            source_type=source_type,
            source=source,
            scope=scope,
            managed=managed,
            installed_at=installed_at,
            content_sha256=content_sha256,
        )

    def _skill_state_path(self, workspace_dir: Path) -> Path:
        return Path(workspace_dir) / "skill_state.json"

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save_json(self, path: Path, payload: Any):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )

    def _load_skill_registry(self) -> dict[str, Any]:
        payload = self._load_json(
            self.skill_registry_path, {"version": 1, "skills": {}}
        )
        if not isinstance(payload, dict):
            return {"version": 1, "skills": {}}
        entries = payload.get("skills")
        if not isinstance(entries, dict):
            entries = {}
        return {"version": 1, "skills": dict(entries)}

    def _save_skill_registry(self, payload: dict[str, Any]) -> None:
        """Atomically persist install provenance without touching Skill packages."""

        self.skill_registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.skill_registry_path.with_name(
            f".{self.skill_registry_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.skill_registry_path)
        finally:
            temporary.unlink(missing_ok=True)

    def list_skills(self) -> list[SkillDefinition]:
        registry_entries = self._load_skill_registry().get("skills", {})
        if not self.skills_dir.exists():
            self._skill_validation_errors = [
                f"Managed Skill `{skill_id}` is missing from {self.skills_dir}"
                for skill_id in sorted(registry_entries)
            ]
            return []
        skills: list[SkillDefinition] = []
        errors: list[str] = []
        for skill_dir in sorted(p for p in self.skills_dir.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                skills.append(
                    self._load_skill_definition(
                        skill_dir,
                        registry_entry=registry_entries.get(skill_dir.name),
                    )
                )
            except (OSError, UnicodeError, SkillValidationError) as exc:
                errors.append(str(exc))
                continue
        loaded_ids = {skill.id for skill in skills}
        for skill_id in sorted(set(registry_entries) - loaded_ids):
            expected = self.skills_dir / skill_id
            if not expected.exists():
                errors.append(f"Managed Skill `{skill_id}` is missing from {expected}")
        self._skill_validation_errors = errors
        return skills

    def skill_validation_errors(self) -> list[str]:
        self.list_skills()
        return list(self._skill_validation_errors)

    def validate_skill(self, skill_id: str) -> tuple[bool, list[str]]:
        canonical = self._canonical_skill_id(skill_id)
        skill_dir = self.skills_dir / canonical
        if not skill_dir.is_dir():
            return False, [f"Skill package is missing: {skill_dir}"]
        registry_entry = self._load_skill_registry().get("skills", {}).get(canonical)
        try:
            self._load_skill_definition(skill_dir, registry_entry=registry_entry)
        except (OSError, UnicodeError, SkillValidationError) as exc:
            return False, [str(exc)]
        return True, []

    def _canonical_skill_id(self, value: str) -> str:
        return (value or "").strip().lower().replace("_", "-")

    def get_skill(self, skill_id: str) -> SkillDefinition | None:
        wanted = self._canonical_skill_id(skill_id)
        for skill in self.list_skills():
            if skill.id == wanted:
                return skill
        return None

    def skill_callback_key(self, skill_id: str) -> str:
        canonical = self._canonical_skill_id(skill_id)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def get_skill_by_callback_key(self, callback_key: str) -> SkillDefinition | None:
        matches = [
            skill
            for skill in self.list_skills()
            if self.skill_callback_key(skill.id) == str(callback_key or "")
        ]
        return matches[0] if len(matches) == 1 else None

    def get_disabled_skill_ids(self, workspace_dir: Path) -> set[str]:
        state = self._load_json(self._skill_state_path(workspace_dir), {})
        disabled = state.get("disabled_skills", {}) if isinstance(state, dict) else {}
        if isinstance(disabled, list):
            raw_ids = {str(item) for item in disabled}
        elif isinstance(disabled, dict):
            raw_ids = {str(key) for key, value in disabled.items() if value}
        else:
            raw_ids = set()
        return {self._canonical_skill_id(item) for item in raw_ids}

    def is_skill_enabled(self, workspace_dir: Path, skill_id: str) -> bool:
        return self._canonical_skill_id(skill_id) not in self.get_disabled_skill_ids(
            workspace_dir
        )

    def set_skill_enabled(
        self,
        workspace_dir: Path,
        skill_id: str,
        *,
        enabled: bool,
        actor: str = "user",
    ) -> tuple[bool, str]:
        skill = self.get_skill(skill_id)
        if skill is None:
            return False, f"Unknown Skill: {skill_id}"
        state_path = self._skill_state_path(workspace_dir)
        state = self._load_json(state_path, {})
        if not isinstance(state, dict):
            state = {}
        disabled = state.get("disabled_skills", {})
        if not isinstance(disabled, dict):
            disabled = {}
        if enabled:
            disabled.pop(skill.id, None)
        else:
            disabled[skill.id] = {
                "disabled_at": self._now(),
                "disabled_by": actor,
            }
        state["disabled_skills"] = disabled
        self._save_json(state_path, state)
        return (
            True,
            f"Skill '{skill.id}' {'enabled' if enabled else 'disabled'} for this agent.",
        )

    def skill_resource_counts(self, skill: SkillDefinition) -> dict[str, int]:
        counts = {"scripts": 0, "references": 0, "assets": 0, "other": 0}
        category_roots = {
            category: skill.skill_dir / category
            for category in ("scripts", "references", "assets")
        }
        for category, root in category_roots.items():
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                base = Path(dirpath)
                dirnames[:] = [
                    name for name in dirnames if not (base / name).is_symlink()
                ]
                counts[category] += len(filenames)
        known_roots = set(category_roots.values())
        try:
            for child in skill.skill_dir.iterdir():
                if child in known_roots or child.name == "SKILL.md":
                    continue
                counts["other"] += 1
        except OSError:
            pass
        return counts

    def record_skill_usage(
        self,
        skill_id: str,
        *,
        agent: str,
        request_id: str,
        source: str,
        task_id: str | None = None,
    ) -> str | None:
        """Append one privacy-bounded Skill invocation event.

        The event intentionally excludes the user prompt and Skill body.  A single
        ``os.write`` against an ``O_APPEND`` descriptor keeps concurrent agent
        processes from sharing a read/modify/write counter file.
        """

        canonical_id = self._canonical_skill_id(skill_id)
        if not canonical_id or not self.SKILL_NAME_PATTERN.fullmatch(canonical_id):
            return None
        invocation_id = f"skill-use-{uuid4().hex}"
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "skill_invoked",
            "invocation_id": invocation_id,
            "skill_id": canonical_id,
            "agent": str(agent or "unknown"),
            "request_id": str(request_id or ""),
            "source": str(source or "unknown"),
        }
        if task_id:
            payload["task_id"] = str(task_id)
        encoded = (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            self.skill_usage_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.skill_usage_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                written = os.write(descriptor, encoded)
                if written != len(encoded):
                    return None
            finally:
                os.close(descriptor)
        except OSError:
            return None
        return invocation_id

    def skill_usage_stats(self, skill_id: str) -> dict[str, Any]:
        """Return cumulative project-wide usage, including legacy token audits."""

        wanted = self._canonical_skill_id(skill_id)
        total = 0
        tracked = 0
        historical = 0
        by_agent: dict[str, int] = {}
        last_used_at = ""
        invocation_ids: set[str] = set()

        def add(record: dict[str, Any], *, legacy: bool) -> None:
            nonlocal total, tracked, historical, last_used_at
            total += 1
            if legacy:
                historical += 1
            else:
                tracked += 1
            agent = str(record.get("agent") or "unknown")
            by_agent[agent] = by_agent.get(agent, 0) + 1
            timestamp = str(record.get("ts") or "")
            last_used_at = max(last_used_at, timestamp)

        if self.skill_usage_path.is_file():
            try:
                with self.skill_usage_path.open("r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        try:
                            record = json.loads(raw_line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        if record.get("event") != "skill_invoked":
                            continue
                        recorded_id = self._canonical_skill_id(
                            str(record.get("skill_id") or "")
                        )
                        if recorded_id != wanted:
                            continue
                        invocation_id = str(record.get("invocation_id") or "")
                        if invocation_id:
                            invocation_ids.add(invocation_id)
                        add(record, legacy=False)
            except OSError:
                pass

        # Before the dedicated usage ledger existed, successful prompt Skills
        # were already identifiable through ``source=skill:<id>`` in each
        # agent's token audit.  Merge those records while excluding new events
        # linked to an invocation already counted above.
        workspaces_root = self.project_root / "workspaces"
        if workspaces_root.is_dir():
            for audit_path in sorted(workspaces_root.glob("*/token_audit.jsonl")):
                try:
                    with audit_path.open("r", encoding="utf-8") as handle:
                        for raw_line in handle:
                            try:
                                record = json.loads(raw_line)
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if not isinstance(record, dict):
                                continue
                            linked_invocation = str(
                                record.get("skill_usage_event_id") or ""
                            )
                            if linked_invocation and linked_invocation in invocation_ids:
                                continue
                            explicit_id = self._canonical_skill_id(
                                str(record.get("skill_id") or "")
                            )
                            source = str(record.get("source") or "")
                            legacy_id = (
                                self._canonical_skill_id(source.split(":", 1)[1])
                                if source.startswith("skill:")
                                else ""
                            )
                            if explicit_id != wanted and legacy_id != wanted:
                                continue
                            if not record.get("agent"):
                                record["agent"] = audit_path.parent.name
                            add(record, legacy=True)
                except OSError:
                    continue

        return {
            "total": total,
            "tracked": tracked,
            "historical": historical,
            "agents": len(by_agent),
            "by_agent": dict(sorted(by_agent.items())),
            "last_used_at": last_used_at or None,
        }

    def skill_dependencies(
        self,
        skill_id: str,
        *,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        wanted = self._canonical_skill_id(skill_id)
        dependencies: list[dict[str, Any]] = []
        tasks = self._load_tasks()
        job_groups = {
            "heartbeat": [
                *tasks.get("heartbeats", []),
                *self._load_active_heartbeats(),
            ],
            "cron": tasks.get("crons", []),
            "nudge": tasks.get("nudges", []),
        }
        for kind, jobs in job_groups.items():
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                enabled = bool(job.get("enabled", False))
                if enabled_only and not enabled:
                    continue
                action = str(job.get("action") or "")
                if ":" not in action:
                    continue
                action_kind, action_target = action.split(":", 1)
                if action_kind not in {"skill", "automation"}:
                    continue
                if self._canonical_skill_id(action_target) != wanted:
                    continue
                dependencies.append(
                    {
                        "kind": kind,
                        "id": str(job.get("id") or "unknown"),
                        "agent": str(job.get("agent") or "unknown"),
                        "enabled": enabled,
                        "action": action,
                    }
                )
        return dependencies

    def can_uninstall_skill(self, skill: SkillDefinition) -> bool:
        return skill.source_type in {"project", "installed", "linked"}

    def _package_digest(self, package_dir: Path) -> str:
        digest = hashlib.sha256()
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(package_dir, followlinks=False):
            base = Path(dirpath)
            linked_directories = [
                name for name in dirnames if (base / name).is_symlink()
            ]
            if linked_directories:
                raise SkillValidationError(
                    f"{base / linked_directories[0]}: symbolic links are not allowed in copied packages"
                )
            for filename in filenames:
                path = base / filename
                if path.is_symlink():
                    raise SkillValidationError(
                        f"{path}: symbolic links are not allowed in copied packages"
                    )
                files.append(path)
        for path in sorted(
            files, key=lambda item: item.relative_to(package_dir).as_posix()
        ):
            relative = path.relative_to(package_dir).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    def install_skill(
        self,
        source: str | Path,
        *,
        link: bool = False,
        actor: str = "user",
    ) -> tuple[bool, str, SkillDefinition | None]:
        raw_source = str(source or "").strip().strip("\"'")
        if not raw_source:
            return False, "A local Skill package directory is required.", None
        source_path = Path(raw_source).expanduser()
        if not source_path.is_absolute():
            source_path = self.project_root / source_path
        try:
            source_path = source_path.resolve(strict=True)
        except OSError as exc:
            return False, f"Skill source is unavailable: {exc}", None
        if not source_path.is_dir():
            return False, f"Skill source is not a directory: {source_path}", None
        try:
            source_skill = self._load_skill_definition(source_path)
            content_sha256 = self._package_digest(source_path)
        except (OSError, UnicodeError, SkillValidationError) as exc:
            return False, f"Skill package validation failed: {exc}", None

        destination = self.skills_dir / source_skill.id
        if destination.exists() or destination.is_symlink():
            return False, f"Skill '{source_skill.id}' already exists.", None
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        registry = self._load_skill_registry()
        if source_skill.id in registry["skills"]:
            return (
                False,
                f"Skill '{source_skill.id}' already has a registry entry.",
                None,
            )

        source_type = "linked" if link else "installed"
        registry_entry = {
            "source_type": source_type,
            "source": str(source_path),
            "scope": "project",
            "installed_at": self._now(),
            "installed_by": actor,
            "content_sha256": content_sha256,
        }
        temporary_root: Path | None = None
        try:
            if link:
                temporary_link = self.skills_dir / f".link-{uuid4().hex}"
                temporary_link.symlink_to(source_path, target_is_directory=True)
                os.replace(temporary_link, destination)
            else:
                temporary_root = Path(
                    tempfile.mkdtemp(prefix=".install-", dir=self.skills_dir)
                )
                staged_package = temporary_root / source_skill.id
                shutil.copytree(source_path, staged_package, symlinks=False)
                self._load_skill_definition(staged_package)
                os.replace(staged_package, destination)
            registry["skills"][source_skill.id] = registry_entry
            self._save_skill_registry(registry)
        except Exception as exc:
            if destination.is_symlink():
                destination.unlink(missing_ok=True)
            elif destination.exists():
                rollback_root = self.skill_recovery_dir / "failed-installs"
                rollback_root.mkdir(parents=True, exist_ok=True)
                destination.rename(
                    rollback_root / f"{source_skill.id}-{uuid4().hex[:8]}"
                )
            return False, f"Skill installation failed: {exc}", None
        finally:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)

        installed = self.get_skill(source_skill.id)
        if installed is None:
            return (
                False,
                f"Skill '{source_skill.id}' installed but failed final validation.",
                None,
            )
        action = "linked" if link else "installed"
        return True, f"Skill '{source_skill.id}' {action} successfully.", installed

    def uninstall_skill(
        self,
        skill_id: str,
    ) -> tuple[bool, str, Path | None]:
        skill = self.get_skill(skill_id)
        if skill is None:
            return False, f"Unknown Skill: {skill_id}", None
        if not self.can_uninstall_skill(skill):
            return (
                False,
                f"Skill '{skill.id}' has an unsupported package source.",
                None,
            )
        dependencies = self.skill_dependencies(skill.id)
        if dependencies:
            labels = ", ".join(item["id"] for item in dependencies[:5])
            if len(dependencies) > 5:
                labels += f", +{len(dependencies) - 5} more"
            return (
                False,
                f"Skill '{skill.id}' is still referenced by Jobs: {labels}.",
                None,
            )

        registry = self._load_skill_registry()
        old_entry = registry["skills"].pop(skill.id, None)
        recovery_path: Path | None = None
        linked_target: Path | None = None
        try:
            if skill.skill_dir.is_symlink():
                linked_target = skill.skill_dir.resolve(strict=False)
                skill.skill_dir.unlink()
            else:
                self.skill_recovery_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                recovery_path = self.skill_recovery_dir / (
                    f"{skill.id}-{timestamp}-{uuid4().hex[:8]}"
                )
                skill.skill_dir.rename(recovery_path)
            self._save_skill_registry(registry)
        except Exception as exc:
            if linked_target is not None and not skill.skill_dir.exists():
                skill.skill_dir.symlink_to(linked_target, target_is_directory=True)
            elif (
                recovery_path is not None
                and recovery_path.exists()
                and not skill.skill_dir.exists()
            ):
                recovery_path.rename(skill.skill_dir)
            if old_entry is not None:
                registry["skills"][skill.id] = old_entry
            return False, f"Skill uninstall failed: {exc}", None

        if skill.source_type == "linked":
            return True, f"Skill '{skill.id}' unlinked; source files were kept.", None
        if skill.source_type == "project":
            return (
                True,
                f"Skill '{skill.id}' deleted to the recovery area.",
                recovery_path,
            )
        return (
            True,
            f"Skill '{skill.id}' uninstalled to the recovery area.",
            recovery_path,
        )

    def get_active_toggle_ids(self, workspace_dir: Path) -> set[str]:
        state = self._load_json(self._skill_state_path(workspace_dir), {})
        active = state.get("active_skills", {})
        if isinstance(active, list):
            raw_ids = {str(item) for item in active}
        elif isinstance(active, dict):
            raw_ids = {str(key) for key, value in active.items() if value}
        else:
            raw_ids = set()
        return {
            canonical
            for item in raw_ids
            if (canonical := self._canonical_skill_id(item)) in self.RUNTIME_TOGGLE_IDS
        }

    def get_active_toggle_skills(self, workspace_dir: Path) -> list[SkillDefinition]:
        active_ids = self.get_active_toggle_ids(workspace_dir)
        debug = self.get_skill("debug")
        return [debug] if debug is not None and "debug" in active_ids else []

    def set_toggle_state(
        self,
        workspace_dir: Path,
        skill_id: str,
        enabled: bool,
        actor: str = "user",
    ) -> tuple[bool, str]:
        canonical_id = self._canonical_skill_id(skill_id)
        if canonical_id not in self.RUNTIME_TOGGLE_IDS:
            return False, f"Unknown runtime setting: {skill_id}"

        state_path = self._skill_state_path(workspace_dir)
        state = self._load_json(state_path, {})
        active = state.get("active_skills", {})
        if not isinstance(active, dict):
            active = {}
        if enabled:
            active[canonical_id] = {"enabled_at": self._now(), "enabled_by": actor}
        else:
            active.pop(canonical_id, None)
        state["active_skills"] = active
        self._save_json(state_path, state)
        label = "Debug" if canonical_id == "debug" else "Recall"
        return True, f"{label} is now {'ON' if enabled else 'OFF'}."

    def describe_skill(self, skill: SkillDefinition, workspace_dir: Path) -> str:
        lines = [
            skill.name,
            skill.description or "No description.",
            f"Usage: /skill {skill.id} <request>",
        ]
        body = (skill.body or "").strip()
        if body:
            preview = (
                body if len(body) <= 700 else body[:700].rstrip() + "\n\n[truncated]"
            )
            lines.extend(["", preview])
        return "\n".join(lines)

    def build_toggle_sections(self, workspace_dir: Path) -> list[tuple[str, str, str]]:
        sections = []
        for skill in self.get_active_toggle_skills(workspace_dir):
            if not skill.body.strip():
                continue
            sections.append((skill.id, skill.name, skill.body.strip()))
        return sections

    def build_prompt_for_skill(self, skill: SkillDefinition, user_prompt: str) -> str:
        body = (skill.body or "").strip()
        if not body:
            return user_prompt
        return (
            f"--- SKILL INSTRUCTIONS [{skill.id}] ---\n"
            f"Skill package root: {skill.skill_dir}\n"
            "Resolve relative resource paths against that package root.\n\n"
            f"{body}\n\n"
            f"--- USER REQUEST ---\n"
            f"{user_prompt}"
        )

    def _load_tasks(self) -> dict[str, Any]:
        tasks = self._load_json(
            self.tasks_path, {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
        )
        tasks.setdefault("heartbeats", [])
        tasks.setdefault("crons", [])
        tasks.setdefault("nudges", [])
        return tasks

    def _save_tasks(self, payload: dict[str, Any]):
        self._save_json(self.tasks_path, payload)

    def _task_key_for_kind(self, kind: str) -> str:
        if kind == "cron":
            return "crons"
        if kind == "nudge":
            return "nudges"
        return "heartbeats"

    def _is_managed_active_heartbeat(self, job: dict[str, Any]) -> bool:
        if not isinstance(job, dict):
            return False
        return job.get("managed_by") == "active-command" or str(
            job.get("id", "")
        ).endswith("-active-heartbeat")

    def _load_active_heartbeats(self) -> list[dict[str, Any]]:
        payload = self._load_json(self.active_heartbeats_path, {"heartbeats": []})
        if isinstance(payload, list):
            jobs = payload
        else:
            jobs = payload.get("heartbeats", [])
        return [dict(job) for job in jobs if isinstance(job, dict)]

    def _save_active_heartbeats(self, jobs: list[dict[str, Any]]):
        self._save_json(self.active_heartbeats_path, {"heartbeats": jobs})

    def _ensure_active_heartbeats_migrated(self):
        tasks = self._load_tasks()
        heartbeats = list(tasks.get("heartbeats", []))
        migrated = [
            dict(job) for job in heartbeats if self._is_managed_active_heartbeat(job)
        ]
        if not migrated:
            return

        remaining = [
            job for job in heartbeats if not self._is_managed_active_heartbeat(job)
        ]
        existing = {job.get("id"): dict(job) for job in self._load_active_heartbeats()}
        for job in migrated:
            existing[job.get("id")] = job

        tasks["heartbeats"] = remaining
        self._save_tasks(tasks)
        self._save_active_heartbeats(list(existing.values()))

    def list_jobs(
        self, kind: str, agent_name: str | None = None
    ) -> list[dict[str, Any]]:
        self._ensure_active_heartbeats_migrated()
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        jobs = list(tasks.get(key, []))
        if kind == "heartbeat":
            jobs.extend(self._load_active_heartbeats())
        if agent_name:
            jobs = [job for job in jobs if job.get("agent") == agent_name]
        return jobs

    def get_job(self, kind: str, task_id: str) -> dict[str, Any] | None:
        for job in self.list_jobs(kind):
            if job.get("id") == task_id:
                return job
        return None

    def upsert_cron_job(
        self,
        *,
        task_id: str,
        agent_name: str,
        schedule: str,
        action: str,
        enabled: bool,
        note: str,
        her_v2_effort: str | None = None,
    ) -> dict[str, Any]:
        """Create or update one owned fixed-wall-clock cron definition."""

        tasks = self._load_tasks()
        crons = tasks.setdefault("crons", [])
        existing = next((job for job in crons if job.get("id") == task_id), None)
        if existing is not None and existing.get("agent") != agent_name:
            raise ValueError(f"Cron task {task_id} belongs to another agent")
        job = existing if existing is not None else {"id": task_id}
        job.update(
            {
                "agent": agent_name,
                "enabled": bool(enabled),
                "schedule": str(schedule).strip(),
                "action": str(action).strip(),
                "note": str(note).strip(),
                "updated_at": self._now(),
            }
        )
        if her_v2_effort is not None:
            normalized_effort = normalize_job_effort(her_v2_effort)
            if normalized_effort is None:
                job.pop(HER_V2_JOB_EFFORT_FIELD, None)
            else:
                job[HER_V2_JOB_EFFORT_FIELD] = normalized_effort.value
        job.pop("time", None)
        if existing is None:
            job["created_at"] = job["updated_at"]
            crons.append(job)
        self._save_tasks(tasks)
        return dict(job)

    def migrate_legacy_dream_cron(
        self,
        *,
        agent_name: str,
        new_task_id: str,
        backend_is_her: bool,
    ) -> dict[str, Any]:
        """Retire enabled generic Dream jobs without touching legacy data."""

        tasks = self._load_tasks()
        crons = tasks.setdefault("crons", [])
        legacy_id = f"dream-{agent_name}-nightly"
        legacy_jobs = [
            job
            for job in crons
            if job.get("agent") == agent_name
            and (job.get("id") == legacy_id or job.get("action") == "skill:dream")
        ]
        enabled_legacy = [job for job in legacy_jobs if bool(job.get("enabled"))]
        canonical = next((job for job in crons if job.get("id") == new_task_id), None)
        changed = False
        created = False
        now = self._now()

        if enabled_legacy and backend_is_her and canonical is None:
            source = enabled_legacy[0]
            schedule = str(source.get("schedule") or "").strip()
            if not schedule:
                legacy_time = str(source.get("time") or "01:30").strip()
                parts = legacy_time.split(":", 1)
                if len(parts) == 2 and all(part.isdigit() for part in parts):
                    schedule = f"{int(parts[1])} {int(parts[0])} * * *"
                else:
                    schedule = "30 1 * * *"
            canonical = {
                "id": new_task_id,
                "agent": agent_name,
                "enabled": True,
                "schedule": schedule,
                "action": "her:dream",
                "note": f"[HER Dream] Habit maintenance for {agent_name}",
                "created_at": now,
                "updated_at": now,
                "migrated_from": str(source.get("id") or legacy_id),
            }
            crons.append(canonical)
            changed = True
            created = True

        for job in legacy_jobs:
            if job.get("enabled"):
                job["enabled"] = False
                job["updated_at"] = now
                job["migration_note"] = (
                    f"Migrated to {new_task_id}."
                    if backend_is_her
                    else "Disabled because HER Dream requires the HER backend."
                )
                changed = True

        if changed:
            self._save_tasks(tasks)
        return {
            "changed": changed,
            "created": created,
            "legacy_enabled_count": len(enabled_legacy),
            "backend_is_her": bool(backend_is_her),
            "new_job": dict(canonical) if canonical is not None else None,
        }

    def describe_jobs(self, kind: str, agent_name: str | None = None) -> str:
        jobs = self.list_jobs(kind, agent_name=agent_name)
        title = (
            "Cron Jobs"
            if kind == "cron"
            else "Nudge Jobs"
            if kind == "nudge"
            else "Heartbeat Jobs"
        )
        if not jobs:
            suffix = f" for {agent_name}" if agent_name else ""
            return f"{title}{suffix}\n\nNo jobs configured."
        lines = [title]
        if agent_name:
            lines.append(f"Agent: {agent_name}")
        lines.append("")
        for job in jobs:
            enabled = "ON" if job.get("enabled", False) else "OFF"
            schedule = (
                (job.get("schedule") or job.get("time", "?"))
                if kind == "cron"
                else f"{job.get('interval_seconds', 0)}s"
            )
            action = job.get("action", "enqueue_prompt")
            note = job.get("note") or ""
            lines.append(f"- {job.get('id')} [{enabled}] {schedule} -> {action}")
            if kind in {"cron", "heartbeat"}:
                try:
                    effort_policy = job_effort_policy(job)
                    lines.append(
                        "  HER execution mode: "
                        f"{effort_display_label(effort_policy['effective'])} "
                        f"({effort_policy['source']})"
                    )
                except ValueError as exc:
                    lines.append(f"  HER execution mode: INVALID ({exc})")
            if note:
                lines.append(f"  {note}")
        return "\n".join(lines)

    def set_job_enabled(
        self, kind: str, task_id: str, enabled: bool
    ) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        if kind == "heartbeat":
            jobs = self._load_active_heartbeats()
            for job in jobs:
                if job.get("id") != task_id:
                    continue
                if enabled:
                    try:
                        normalize_job_effort_in_place(job)
                    except ValueError as exc:
                        return False, f"Refusing to enable {task_id}: {exc}."
                    mismatch = ownership_mismatch_label(job)
                    if mismatch:
                        return False, f"Refusing to enable {task_id}: {mismatch}."
                job["enabled"] = enabled
                self._save_active_heartbeats(jobs)
                return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        for job in tasks.get(key, []):
            if job.get("id") != task_id:
                continue
            if enabled:
                if kind in {"cron", "heartbeat"}:
                    try:
                        normalize_job_effort_in_place(job)
                    except ValueError as exc:
                        return False, f"Refusing to enable {task_id}: {exc}."
                mismatch = ownership_mismatch_label(job)
                if mismatch:
                    return False, f"Refusing to enable {task_id}: {mismatch}."
            job["enabled"] = enabled
            self._save_tasks(tasks)
            return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        return False, f"Unknown {kind} task: {task_id}"

    def set_nudge_max(self, task_id: str, max_nudges: int) -> tuple[bool, str]:
        tasks = self._load_tasks()
        max_value = max(0, int(max_nudges or 0))
        for job in tasks.get("nudges", []):
            if job.get("id") != task_id:
                continue
            meta = job.setdefault("nudge_meta", {})
            meta["max"] = max_value
            meta.pop("stopped_reason", None)
            self._save_tasks(tasks)
            label = "unlimited" if max_value == 0 else str(max_value)
            return True, f"{task_id} max is now {label}."
        return False, f"Unknown nudge task: {task_id}"

    def adjust_nudge_max(self, task_id: str, delta: int) -> tuple[bool, str]:
        tasks = self._load_tasks()
        for job in tasks.get("nudges", []):
            if job.get("id") != task_id:
                continue
            meta = job.setdefault("nudge_meta", {})
            current = max(0, int(meta.get("max", 0) or 0))
            new_value = max(0, current + int(delta))
            meta["max"] = new_value
            meta.pop("stopped_reason", None)
            self._save_tasks(tasks)
            label = "unlimited" if new_value == 0 else str(new_value)
            return True, f"{task_id} max is now {label}."
        return False, f"Unknown nudge task: {task_id}"

    def delete_job(self, kind: str, task_id: str) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        if kind == "heartbeat":
            jobs = self._load_active_heartbeats()
            for i, job in enumerate(jobs):
                if job.get("id") == task_id:
                    jobs.pop(i)
                    self._save_active_heartbeats(jobs)
                    return True, f"Deleted {task_id}."
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        jobs = tasks.get(key, [])
        for i, job in enumerate(jobs):
            if job.get("id") == task_id:
                jobs.pop(i)
                self._save_tasks(tasks)
                return True, f"Deleted {task_id}."
        return False, f"Unknown {kind} task: {task_id}"

    def transfer_job(
        self, kind: str, task_id: str, new_agent: str
    ) -> tuple[bool, str, dict | None]:
        """Disable original job and create a copy owned by new_agent.

        Returns (ok, message, new_job_dict).
        The new job is enabled=False so the recipient can review before enabling.
        """
        import copy
        from uuid import uuid4

        job = self.get_job(kind, task_id)
        if not job:
            return False, f"Job {task_id} not found.", None

        # Disable original
        ok, msg = self.set_job_enabled(kind, task_id, enabled=False)
        if not ok:
            return False, msg, None

        # Create copy for new owner
        new_job = copy.deepcopy(job)
        new_job["id"] = f"{new_agent}-{uuid4().hex[:8]}"
        new_job["agent"] = new_agent
        new_job["enabled"] = False
        new_job["note"] = (
            job.get("note") or job["id"]
        ) + f" [transferred from {job.get('agent', '?')}]"
        mismatch = ownership_mismatch_label(new_job)
        if mismatch:
            new_job["note"] += f" [{mismatch}; review before enabling]"

        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        tasks.setdefault(key, []).append(new_job)
        self._save_tasks(tasks)
        return (
            True,
            f"Transferred to {new_agent} (disabled, review before enabling).",
            new_job,
        )

    def import_job(self, kind: str, job: dict) -> tuple[bool, str]:
        """Import a job dict into local tasks.json (used for cross-instance transfer)."""
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        job = dict(job)
        if kind in {"cron", "heartbeat"}:
            try:
                normalize_job_effort_in_place(job)
            except ValueError as exc:
                return False, f"Invalid imported job: {exc}."
        mismatch = ownership_mismatch_label(job)
        if mismatch:
            job["enabled"] = False
            note = job.get("note") or job.get("id") or ""
            suffix = f"[{mismatch}; imported disabled]"
            job["note"] = f"{note} {suffix}".strip()
        # Avoid duplicate IDs
        existing_ids = {j.get("id") for j in tasks.get(key, [])}
        if job.get("id") in existing_ids:
            from uuid import uuid4

            job["id"] = f"{job.get('agent', 'imported')}-{uuid4().hex[:8]}"
        tasks.setdefault(key, []).append(job)
        self._save_tasks(tasks)
        return True, f"Imported job {job['id']} for {job.get('agent', '?')}."

    def _nudge_prompt(self, task_id: str, exit_condition: str) -> str:
        return (
            "SYSTEM: Idle nudge continuation.\n"
            "You are being nudged because the agent is idle and this nudge job is still enabled.\n\n"
            f"Nudge job id: {task_id}\n"
            f"Exit condition: {exit_condition}\n\n"
            "Instructions:\n"
            "1. Review the current task state and recent work.\n"
            "2. If the exit condition is NOT satisfied, continue the work with a concrete next step.\n"
            "3. If the exit condition IS satisfied, say so clearly and include this exact marker on its own line:\n"
            f"NUDGE_COMPLETE:{task_id}\n"
            "4. Do not emit the completion marker unless the exit condition is genuinely satisfied.\n"
        )

    def create_nudge_job(
        self,
        *,
        agent_name: str,
        interval_minutes: int,
        exit_condition: str,
        max_nudges: int = 0,
    ) -> dict[str, Any]:
        tasks = self._load_tasks()
        task_id = f"{agent_name}-nudge-{uuid4().hex[:6]}"
        interval_minutes = max(1, int(interval_minutes))
        exit_condition = str(exit_condition or "").strip()
        job = {
            "id": task_id,
            "agent": agent_name,
            "enabled": True,
            "interval_seconds": interval_minutes * 60,
            "action": "enqueue_prompt",
            "prompt": self._nudge_prompt(task_id, exit_condition),
            "note": f"Nudge: {exit_condition[:80]}",
            "exit_condition": exit_condition,
            "nudge_meta": {
                "count": 0,
                "max": int(max_nudges),
                "created_at": self._now(),
            },
        }
        tasks.setdefault("nudges", []).append(job)
        self._save_tasks(tasks)
        return job

    def get_active_heartbeat_job_id(self, agent_name: str) -> str:
        return f"{agent_name}-active-heartbeat"

    def get_active_heartbeat_job(self, agent_name: str) -> dict[str, Any] | None:
        self._ensure_active_heartbeats_migrated()
        task_id = self.get_active_heartbeat_job_id(agent_name)
        for job in self._load_active_heartbeats():
            if job.get("id") == task_id:
                return job
        return None

    def describe_active_heartbeat(self, agent_name: str) -> str:
        job = self.get_active_heartbeat_job(agent_name)
        default_minutes = self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        if not job:
            return (
                f"Active mode: OFF\n"
                f"Interval: {default_minutes} min (default)\n"
                "HER execution mode: Fast path (low) (scheduled job default)\n"
                f"Usage: /active on [{default_minutes}] | /active off"
            )
        interval_minutes = max(
            1, int(job.get("interval_seconds", default_minutes * 60) // 60)
        )
        state = "ON" if job.get("enabled", False) else "OFF"
        reset_note = (
            " (default reset)"
            if not job.get("enabled", False) and interval_minutes == default_minutes
            else ""
        )
        try:
            effort_policy = job_effort_policy(job)
            effort_source = (
                "job override"
                if effort_policy["source"] == "job_override"
                else "scheduled job default"
            )
            effort_label = (
                f"{effort_display_label(effort_policy['effective'])} "
                f"({effort_source})"
            )
        except ValueError as exc:
            effort_label = f"INVALID ({exc})"
        return (
            f"Active mode: {state}\n"
            f"Interval: {interval_minutes} min{reset_note}\n"
            f"Job: {job.get('id')}\n"
            f"HER execution mode: {effort_label}\n"
            f"Usage: /active on [{default_minutes}] | /active off"
        )

    def _active_heartbeat_prompt(self, interval_minutes: int) -> str:
        return (
            "SYSTEM: Active follow-up heartbeat. You are in proactive mode. "
            f"About {interval_minutes} minutes have passed since the user's recent activity. "
            "Review the most recent conversation context, workspace evidence, queued/running work, and any obvious signs of progress or blockage. "
            "Then proactively help the user: report meaningful progress, warn about concrete problems or stalls, ask a concise unblock question if needed, or remind the user about unfinished work that likely still matters. "
            "Be concise, specific, and useful. Do not pretend to have done work you have not done. If there is nothing meaningful to report, say that briefly instead of inventing activity."
        )

    def set_active_heartbeat(
        self, agent_name: str, enabled: bool, minutes: int | None = None
    ) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        heartbeats = self._load_active_heartbeats()
        task_id = self.get_active_heartbeat_job_id(agent_name)
        interval_minutes = max(1, int(minutes or self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES))
        if not enabled:
            interval_minutes = self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        interval_seconds = interval_minutes * 60

        job = None
        for entry in heartbeats:
            if entry.get("id") == task_id:
                job = entry
                break

        if job is None and not enabled:
            return True, (
                f"Active mode is already OFF. Interval remains "
                f"{self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES} min."
            )

        if job is None:
            job = {
                "id": task_id,
                "agent": agent_name,
                "enabled": enabled,
                "interval_seconds": interval_seconds,
                "action": "enqueue_prompt",
                "prompt": self._active_heartbeat_prompt(interval_minutes),
                "note": f"Managed proactive follow-up heartbeat for {agent_name}",
                "managed_by": "active-command",
            }
            heartbeats.append(job)
        else:
            job["enabled"] = enabled
            job["interval_seconds"] = interval_seconds
            job["action"] = "enqueue_prompt"
            job["prompt"] = self._active_heartbeat_prompt(interval_minutes)
            job["note"] = f"Managed proactive follow-up heartbeat for {agent_name}"
            job["managed_by"] = "active-command"

        self._save_active_heartbeats(heartbeats)

        if enabled:
            return (
                True,
                f"Active mode is now ON. Proactive heartbeat set to every {interval_minutes} min.",
            )
        return True, (
            f"Active mode is now OFF. Proactive heartbeat disabled and interval reset to "
            f"{self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES} min."
        )
