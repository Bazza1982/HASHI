"""ETE v1.0 — Reference codec for reading, writing, and validating ETE bundles."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from .ete_types import (
    ETE_VERSION,
    KB_ALLOWED_EXTENSIONS,
    KB_MAX_BLOCK_BYTES,
    KB_MAX_FILE_BYTES,
    KB_MAX_SECTION_BYTES,
    LEGACY_MIME_MAP,
    ETEArtefactMeta,
    ETEBundle,
    ETEImportError,
    ETEManifest,
    ETEProject,
    ETETask,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECTION_DETECTABLE_DIRS = {"tasks", "artefacts", "workflows", "knowledge_blocks"}
_WINDOWS_ILLEGAL_CHARS = set('<>:"/\\|?*')


def _resolve_mime(mime_type: str) -> str:
    """Map a legacy MIME type to its canonical form, or return as-is."""
    return LEGACY_MIME_MAP.get(mime_type, mime_type)


def _err(
    code: str,
    message: str,
    severity: str = "fatal",
    section: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> ETEImportError:
    return ETEImportError(
        code=code,
        message=message,
        severity=severity,  # type: ignore[arg-type]
        section=section,
        entity_id=entity_id,
    )


def _read_json(path: Path) -> dict:
    """Read and parse a JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as JSON with sorted keys for deterministic output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def _has_path_traversal(p: Path, root: Path) -> bool:
    """Return True if *p* escapes *root* via ``..`` segments."""
    try:
        p.resolve().relative_to(root.resolve())
        return False
    except ValueError:
        return True


def _validate_filename(name: str) -> bool:
    """Return True if *name* is safe on all major OSes."""
    if len(name) > 200:
        return False
    if any(ch in _WINDOWS_ILLEGAL_CHARS for ch in name):
        return False
    return True


# ---------------------------------------------------------------------------
# validate_ete
# ---------------------------------------------------------------------------


def validate_ete(ete_path: Path) -> list[ETEImportError]:
    """Validate an ETE bundle directory without importing it.

    Returns an empty list when the bundle is valid.  Otherwise each issue is
    represented as an :class:`ETEImportError` with an appropriate severity.
    """
    errors: list[ETEImportError] = []
    ete_path = ete_path.resolve()

    # --- ete.json exists and parses -----------------------------------------
    manifest_path = ete_path / "ete.json"
    if not manifest_path.is_file():
        errors.append(_err("E_MANIFEST_INVALID", "ete.json is missing"))
        return errors  # nothing else to check

    try:
        raw_manifest = _read_json(manifest_path)
        manifest = ETEManifest(**raw_manifest)
    except Exception as exc:
        errors.append(_err("E_MANIFEST_INVALID", f"ete.json failed validation: {exc}"))
        return errors

    # --- ete_version major == 1 ---------------------------------------------
    try:
        major = int(manifest.ete_version.split(".")[0])
    except (ValueError, IndexError):
        errors.append(_err("E_MANIFEST_INVALID", f"Cannot parse ete_version: {manifest.ete_version}"))
        return errors

    if major != 1:
        errors.append(
            _err("E_MANIFEST_INVALID", f"Unsupported ETE major version {major} (expected 1)")
        )
        return errors

    # --- declared sections vs reality ---------------------------------------
    declared = set(manifest.contents)

    for section in declared:
        if section == "project":
            pj = ete_path / "project.json"
            if not pj.is_file():
                errors.append(
                    _err("E_SECTION_DECLARED_BUT_MISSING", "project.json missing", section="project")
                )
            else:
                try:
                    ETEProject(**_read_json(pj))
                except Exception as exc:
                    errors.append(
                        _err("E_SCHEMA_VALIDATION", f"project.json invalid: {exc}", section="project")
                    )

        elif section == "tasks":
            tasks_dir = ete_path / "tasks"
            if not tasks_dir.is_dir():
                errors.append(
                    _err("E_SECTION_DECLARED_BUT_MISSING", "tasks/ directory missing", section="tasks")
                )
            else:
                task_files = list(tasks_dir.glob("*.json"))
                if not task_files:
                    errors.append(
                        _err(
                            "E_SECTION_DECLARED_BUT_MISSING",
                            "tasks/ exists but contains no .json files",
                            section="tasks",
                        )
                    )
                for tf in task_files:
                    try:
                        ETETask(**_read_json(tf))
                    except Exception as exc:
                        errors.append(
                            _err(
                                "E_SCHEMA_VALIDATION",
                                f"{tf.name} invalid: {exc}",
                                section="tasks",
                                entity_id=tf.stem,
                            )
                        )

        elif section == "artefacts":
            art_dir = ete_path / "artefacts"
            if not art_dir.is_dir():
                errors.append(
                    _err(
                        "E_SECTION_DECLARED_BUT_MISSING",
                        "artefacts/ directory missing",
                        section="artefacts",
                    )
                )
            else:
                for sub in sorted(art_dir.iterdir()):
                    if not sub.is_dir():
                        continue
                    meta_file = sub / "meta.json"
                    if not meta_file.is_file():
                        errors.append(
                            _err(
                                "E_ARTEFACT_NO_META",
                                f"artefacts/{sub.name}/ has no meta.json",
                                section="artefacts",
                                entity_id=sub.name,
                            )
                        )
                        continue
                    try:
                        ETEArtefactMeta(**_read_json(meta_file))
                    except Exception as exc:
                        errors.append(
                            _err(
                                "E_SCHEMA_VALIDATION",
                                f"artefacts/{sub.name}/meta.json invalid: {exc}",
                                section="artefacts",
                                entity_id=sub.name,
                            )
                        )
                    payload_files = [
                        f for f in sub.iterdir() if f.is_file() and f.name != "meta.json"
                    ]
                    if not payload_files:
                        errors.append(
                            _err(
                                "E_ARTEFACT_NO_PAYLOAD",
                                f"artefacts/{sub.name}/ has meta.json but no payload",
                                section="artefacts",
                                entity_id=sub.name,
                            )
                        )

        elif section == "workflows":
            wf_dir = ete_path / "workflows"
            if not wf_dir.is_dir():
                errors.append(
                    _err(
                        "E_SECTION_DECLARED_BUT_MISSING",
                        "workflows/ directory missing",
                        section="workflows",
                    )
                )
            else:
                yaml_files = list(wf_dir.glob("*.yaml")) + list(wf_dir.glob("*.yml"))
                if not yaml_files:
                    errors.append(
                        _err(
                            "E_SECTION_DECLARED_BUT_MISSING",
                            "workflows/ exists but contains no .yaml files",
                            section="workflows",
                        )
                    )

        elif section == "knowledge_blocks":
            kb_dir = ete_path / "knowledge_blocks"
            if not kb_dir.is_dir():
                errors.append(
                    _err(
                        "E_SECTION_DECLARED_BUT_MISSING",
                        "knowledge_blocks/ directory missing",
                        section="knowledge_blocks",
                    )
                )
            else:
                errors.extend(_validate_knowledge_blocks(ete_path, kb_dir))

    # --- undeclared but present (warning) -----------------------------------
    for dirname in _SECTION_DETECTABLE_DIRS - declared:
        candidate = ete_path / dirname
        if candidate.exists():
            errors.append(
                _err(
                    "E_SECTION_UNDECLARED_BUT_PRESENT",
                    f"{dirname}/ exists but is not listed in contents",
                    severity="warning",
                    section=dirname,
                )
            )
    if "project" not in declared and (ete_path / "project.json").exists():
        errors.append(
            _err(
                "E_SECTION_UNDECLARED_BUT_PRESENT",
                "project.json exists but 'project' is not listed in contents",
                severity="warning",
                section="project",
            )
        )

    return errors


def _validate_knowledge_blocks(
    ete_root: Path, kb_dir: Path
) -> list[ETEImportError]:
    """Check knowledge-block constraints (allowed extensions, symlinks, sizes, etc.)."""
    errors: list[ETEImportError] = []
    total_section_bytes = 0

    for block_dir in sorted(kb_dir.iterdir()):
        if not block_dir.is_dir():
            continue

        # Must contain bundle_manifest.json
        bm = block_dir / "bundle_manifest.json"
        if not bm.is_file():
            errors.append(
                _err(
                    "E_KB_NO_MANIFEST",
                    f"knowledge_blocks/{block_dir.name}/ has no bundle_manifest.json",
                    section="knowledge_blocks",
                    entity_id=block_dir.name,
                )
            )

        block_bytes = 0
        for item in block_dir.rglob("*"):
            if not item.is_file():
                continue

            rel = item.relative_to(block_dir)

            # No symlinks
            if item.is_symlink():
                errors.append(
                    _err(
                        "E_KB_SYMLINK",
                        f"Symlink not allowed: knowledge_blocks/{block_dir.name}/{rel}",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # No path traversal
            if _has_path_traversal(item, block_dir):
                errors.append(
                    _err(
                        "E_KB_PATH_TRAVERSAL",
                        f"Path traversal detected: knowledge_blocks/{block_dir.name}/{rel}",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # No hidden files
            if any(part.startswith(".") for part in rel.parts):
                errors.append(
                    _err(
                        "E_KB_HIDDEN_FILE",
                        f"Hidden file not allowed: knowledge_blocks/{block_dir.name}/{rel}",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # Max one level of subdirectory
            if len(rel.parts) > 2:
                errors.append(
                    _err(
                        "E_KB_TOO_DEEP",
                        f"Exceeds max depth: knowledge_blocks/{block_dir.name}/{rel}",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # Filename safety
            if not _validate_filename(item.name):
                errors.append(
                    _err(
                        "E_KB_INVALID_FILENAME",
                        f"Unsafe filename: {item.name}",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # Allowed extensions
            if item.suffix.lower() not in KB_ALLOWED_EXTENSIONS:
                errors.append(
                    _err(
                        "E_KB_DISALLOWED_EXT",
                        f"File type not allowed: {item.suffix} ({rel})",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            # Size limits
            file_size = item.stat().st_size
            if file_size > KB_MAX_FILE_BYTES:
                errors.append(
                    _err(
                        "E_KB_FILE_TOO_LARGE",
                        f"File exceeds {KB_MAX_FILE_BYTES} bytes: {rel} ({file_size})",
                        severity="recoverable",
                        section="knowledge_blocks",
                        entity_id=block_dir.name,
                    )
                )
                continue

            block_bytes += file_size

        if block_bytes > KB_MAX_BLOCK_BYTES:
            errors.append(
                _err(
                    "E_KB_BLOCK_TOO_LARGE",
                    f"Knowledge block {block_dir.name} exceeds {KB_MAX_BLOCK_BYTES} bytes ({block_bytes})",
                    severity="recoverable",
                    section="knowledge_blocks",
                    entity_id=block_dir.name,
                )
            )

        total_section_bytes += block_bytes

    if total_section_bytes > KB_MAX_SECTION_BYTES:
        errors.append(
            _err(
                "E_KB_SECTION_TOO_LARGE",
                f"knowledge_blocks/ section exceeds {KB_MAX_SECTION_BYTES} bytes ({total_section_bytes})",
                severity="recoverable",
                section="knowledge_blocks",
            )
        )

    return errors


# ---------------------------------------------------------------------------
# export_ete
# ---------------------------------------------------------------------------


def export_ete(
    output_dir: Path,
    *,
    manifest: ETEManifest,
    project: ETEProject | None = None,
    tasks: list[ETETask] | None = None,
    artefacts: list[tuple[ETEArtefactMeta, Path]] | None = None,
    workflows: list[Path] | None = None,
    knowledge_blocks: list[Path] | None = None,
) -> Path:
    """Write an ETE bundle directory.

    Creates *output_dir* (and parents) if it does not exist, populates it
    with the given data, validates the result, and returns *output_dir*.

    Raises ``ValueError`` if post-export validation finds fatal errors.
    """
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- compute actual contents based on what was provided -----------------
    actual_contents: list[str] = []
    if project is not None:
        actual_contents.append("project")
    if tasks:
        actual_contents.append("tasks")
    if artefacts:
        actual_contents.append("artefacts")
    if workflows:
        actual_contents.append("workflows")
    if knowledge_blocks:
        actual_contents.append("knowledge_blocks")

    # --- ete.json -----------------------------------------------------------
    manifest_dump = manifest.model_dump()
    manifest_dump["contents"] = actual_contents
    _write_json(output_dir / "ete.json", manifest_dump)

    # --- project.json -------------------------------------------------------
    if project is not None:
        _write_json(output_dir / "project.json", project.model_dump())

    # --- tasks/ -------------------------------------------------------------
    if tasks:
        tasks_dir = output_dir / "tasks"
        tasks_dir.mkdir(exist_ok=True)
        for task in tasks:
            _write_json(tasks_dir / f"{task.task_id}.json", task.model_dump())

    # --- artefacts/ ---------------------------------------------------------
    if artefacts:
        art_dir = output_dir / "artefacts"
        art_dir.mkdir(exist_ok=True)
        for meta, payload_path in artefacts:
            sub = art_dir / meta.artefact_id
            sub.mkdir(exist_ok=True)
            # Resolve legacy MIME before writing
            dump = meta.model_dump()
            dump["mime_type"] = _resolve_mime(dump["mime_type"])
            _write_json(sub / "meta.json", dump)
            dest = sub / payload_path.name
            shutil.copy2(payload_path, dest)

    # --- workflows/ ---------------------------------------------------------
    if workflows:
        wf_dir = output_dir / "workflows"
        wf_dir.mkdir(exist_ok=True)
        for wf in workflows:
            shutil.copy2(wf, wf_dir / wf.name)

    # --- knowledge_blocks/ --------------------------------------------------
    if knowledge_blocks:
        kb_dir = output_dir / "knowledge_blocks"
        kb_dir.mkdir(exist_ok=True)
        for kb_src in knowledge_blocks:
            if kb_src.is_dir():
                dest = kb_dir / kb_src.name
                shutil.copytree(kb_src, dest, dirs_exist_ok=True)

    # --- post-export validation ---------------------------------------------
    post_errors = validate_ete(output_dir)
    fatal = [e for e in post_errors if e.severity == "fatal"]
    if fatal:
        msgs = "; ".join(e.message for e in fatal)
        raise ValueError(f"Exported bundle failed validation: {msgs}")

    return output_dir


# ---------------------------------------------------------------------------
# import_ete
# ---------------------------------------------------------------------------


def import_ete(ete_path: Path) -> ETEBundle:
    """Read an ETE bundle directory, returning a fully-parsed :class:`ETEBundle`.

    Calls :func:`validate_ete` first and raises ``ValueError`` if any fatal
    errors are found.
    """
    ete_path = ete_path.resolve()

    # --- validate first -----------------------------------------------------
    errors = validate_ete(ete_path)
    fatal = [e for e in errors if e.severity == "fatal"]
    if fatal:
        msgs = "; ".join(e.message for e in fatal)
        raise ValueError(f"ETE bundle has fatal validation errors: {msgs}")

    # --- read manifest ------------------------------------------------------
    manifest = ETEManifest(**_read_json(ete_path / "ete.json"))
    declared = set(manifest.contents)

    # --- project ------------------------------------------------------------
    project: ETEProject | None = None
    if "project" in declared:
        project = ETEProject(**_read_json(ete_path / "project.json"))

    # --- tasks --------------------------------------------------------------
    task_list: list[ETETask] = []
    if "tasks" in declared:
        tasks_dir = ete_path / "tasks"
        for tf in sorted(tasks_dir.glob("*.json")):
            task_list.append(ETETask(**_read_json(tf)))

    # --- artefacts ----------------------------------------------------------
    artefact_metas: list[ETEArtefactMeta] = []
    artefact_payloads: dict[str, Path] = {}
    if "artefacts" in declared:
        art_dir = ete_path / "artefacts"
        for sub in sorted(art_dir.iterdir()):
            if not sub.is_dir():
                continue
            meta_file = sub / "meta.json"
            if not meta_file.is_file():
                continue
            meta = ETEArtefactMeta(**_read_json(meta_file))
            # Resolve legacy MIME on read
            meta.mime_type = _resolve_mime(meta.mime_type)
            artefact_metas.append(meta)
            # Record the first non-meta file as the payload path
            for f in sorted(sub.iterdir()):
                if f.is_file() and f.name != "meta.json":
                    artefact_payloads[meta.artefact_id] = f
                    break

    # --- workflows ----------------------------------------------------------
    workflow_paths: list[Path] = []
    if "workflows" in declared:
        wf_dir = ete_path / "workflows"
        workflow_paths = sorted(
            p for p in wf_dir.iterdir() if p.is_file() and p.suffix in {".yaml", ".yml"}
        )

    # --- knowledge_blocks ---------------------------------------------------
    kb_dirs: list[Path] = []
    if "knowledge_blocks" in declared:
        kb_dir = ete_path / "knowledge_blocks"
        kb_dirs = sorted(d for d in kb_dir.iterdir() if d.is_dir())

    return ETEBundle(
        manifest=manifest,
        project=project,
        tasks=task_list,
        artefacts=artefact_metas,
        artefact_payloads=artefact_payloads,
        workflow_paths=workflow_paths,
        knowledge_block_dirs=kb_dirs,
    )
