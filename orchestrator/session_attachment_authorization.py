"""Current-Run Session attachment grants for backend byte consumption only.

These exact files never become workspace, CLI add-dir, or tool permissions.
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class AttachmentReadGrant:
    attachment_id: str
    path: Path
    device: int
    inode: int
    size_bytes: int
    sha256: str
    ancestors: tuple[tuple[str, int, int], ...]


@dataclass
class _ConsumptionScope:
    backend: object
    grants: tuple[AttachmentReadGrant, ...]
    active: bool = True


_current: ContextVar[_ConsumptionScope | None] = ContextVar(
    "hashi_backend_session_attachment_reads", default=None
)


def _deny(attachment_id: str = "", *, integrity: bool = False):
    from orchestrator.multimodal_contract import MultimodalContractError
    raise MultimodalContractError(
        "Session attachment no longer matches the authorized current Run",
        code="MEDIA_INTEGRITY_CHANGED" if integrity else "MEDIA_PATH_NOT_AUTHORIZED",
        attachment_id=attachment_id,
    )


def _open_no_symlinks(path: Path, flags: int) -> tuple[int, tuple[tuple[str, int, int], ...]]:
    """Walk directory fds; a replaced/symlink parent cannot redirect the open."""
    absolute = path.expanduser().absolute()
    if ".." in absolute.parts:
        raise OSError("non-canonical attachment path")
    if not session_attachment_grants_supported():
        # This owner operates on HASHI1 Linux. Fail closed rather than emulate
        # the directory-fd fence on unsupported platforms.
        raise OSError("safe Session attachment open is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(absolute.anchor, directory_flags)
    ancestors = []
    current = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            current /= component
            observed = os.fstat(directory_fd)
            ancestors.append((str(current), int(observed.st_dev), int(observed.st_ino)))
        fd = os.open(absolute.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        return fd, tuple(ancestors)
    finally:
        os.close(directory_fd)


def session_attachment_grants_supported() -> bool:
    return os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


def open_granted_media(part: Mapping[str, Any], flags: int) -> int | None:
    """Open only a task-bound exact file and verify the frozen inode fence."""
    bound = _current.get()
    if bound is None or not bound.active:
        return None
    path = Path(str(part["local_ref"])).expanduser().absolute()
    matching = [grant for grant in bound.grants if grant.path == path]
    if not matching:
        return None
    grant = matching[0]
    if (str(part["attachment_id"]) != grant.attachment_id
            or int(part["size_bytes"]) != grant.size_bytes
            or str(part["sha256"]) != grant.sha256):
        _deny(grant.attachment_id, integrity=True)
    try:
        fd, ancestors = _open_no_symlinks(path, flags)
    except OSError:
        _deny(grant.attachment_id, integrity=True)
    observed = os.fstat(fd)
    if (ancestors != grant.ancestors or not stat.S_ISREG(observed.st_mode)
            or (int(observed.st_dev), int(observed.st_ino)) != (grant.device, grant.inode)):
        os.close(fd)
        _deny(grant.attachment_id, integrity=True)
    return fd


def backend_attachment_roots(backend: object) -> tuple[Path, ...]:
    bound = _current.get()
    return tuple(grant.path for grant in bound.grants) if bound and bound.active and bound.backend is backend else ()


@contextmanager
def bind_backend_attachment_reads(backend: object, grants: tuple[AttachmentReadGrant, ...]):
    scope = _ConsumptionScope(backend, grants)
    token = _current.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _current.reset(token)


def current_run_attachment_grants(manager: Any, request_id: str, content: Mapping[str, Any] | None):
    from orchestrator.multimodal_contract import MultimodalContractError, normalize_request_content
    from orchestrator.audio_assets import AudioAssetError
    from orchestrator.session_store import SessionStoreError

    normalized = normalize_request_content(content)
    parts = [part for part in (normalized or {}).get("parts", ()) if part["type"] == "media"]
    if not parts:
        return ()
    secure_grants = session_attachment_grants_supported()
    runtime = getattr(manager, "runtime", None)
    store = getattr(runtime, "session_store", None)
    registered = getattr(runtime, "_request_meta_by_id", {})
    meta = dict(registered.get(request_id) or {}) if isinstance(registered, dict) else {}
    if not meta:
        current = getattr(runtime, "current_request_meta", {}) or {}
        if current.get("request_id") == request_id:
            meta = dict(current)
    # Legacy non-Session inputs retain their existing roots; they receive no
    # Session grants. A declared Session must have authoritative persistent scope.
    if not meta.get("hashi_session_id"):
        return ()
    attachment_id = str(parts[0]["attachment_id"])
    if store is None:
        _deny(attachment_id)
    agent = str(getattr(manager.config, "name", "")).lower()
    if str(getattr(runtime, "name", "")).lower() != agent:
        _deny(attachment_id)
    try:
        run = store.get_run_by_request(request_id, agent_id=agent)
        session = store.get_session(run["session_id"], agent_id=agent, include_deleted=False)
        expected = {
            "request_id": request_id, "hashi_session_id": run["session_id"],
            "hashi_run_id": run["run_id"], "hashi_message_id": run["user_message_id"],
            "hashi_fencing_token": run["fencing_token"], "owner_id": session["owner_id"],
            "context_generation": run["context_generation"],
        }
        if (run["state"] != "running" or run["context_generation"] != session["context_generation"]
                or any(str(meta.get(key)) != str(value) for key, value in expected.items())):
            _deny(attachment_id)
        message = store.get_message(run["user_message_id"], session_id=run["session_id"], owner_id=session["owner_id"])
        if (message["run_id"] != run["run_id"] or message["role"] != "user"
                or message["context_generation"] != run["context_generation"]):
            _deny(attachment_id)
        references = {str(block.get("attachment_id")) for block in message["content"]
                      if block.get("type") in {"attachment", "media", "audio"}}
        grants = []
        for part in parts:
            attachment_id = str(part["attachment_id"])
            if attachment_id not in references:
                _deny(attachment_id)
            with store._lock, store._connection() as connection:
                row = connection.execute("SELECT * FROM session_attachments WHERE attachment_id=? AND session_id=? AND owner_id=? AND state='committed'",
                    (attachment_id, run["session_id"], session["owner_id"])).fetchone()
            if row is None:
                _deny(attachment_id)
            row = dict(row)
            if str(row["media_type"]).startswith("audio/"):
                assets = store.audio_assets
                with assets._lock:
                    raw = assets._read(str(row["asset_id"]))
                    assets._authorize(raw, owner_id=session["owner_id"], session_id=run["session_id"])
                storage_name = f"{row['asset_id']}.{raw['format']}"
                if raw.get("asset_id") != row["asset_id"] or raw.get("storage_name") != storage_name:
                    _deny(attachment_id)
                path = assets.files_root / storage_name
            else:
                path = store._attachment_file_path(attachment_id, row["filename"])
            if secure_grants:
                fd, ancestors = _open_no_symlinks(path, os.O_RDONLY)
                try:
                    observed = os.fstat(fd)
                    if not stat.S_ISREG(observed.st_mode):
                        _deny(attachment_id)
                finally:
                    os.close(fd)
            else:
                # Preserve the established Windows reader for existing roots;
                # only additional outside-root Session grants are unavailable.
                roots = tuple(Path(root).resolve() for root in manager.current_backend.authorized_media_roots())
                if not any(path.resolve().is_relative_to(root) for root in roots):
                    raise MultimodalContractError(
                        "Exact-file Session attachment grants are unavailable on this platform",
                        code="MEDIA_PATH_NOT_AUTHORIZED", attachment_id=attachment_id,
                    )
            canonical = store.attachment_canonical_part(session_id=run["session_id"], owner_id=session["owner_id"],
                attachment_id=attachment_id, item_index=part["item_index"])
            fields = ("attachment_id", "local_ref", "modality", "mime_type", "size_bytes", "sha256")
            if (any(part[key] != canonical[key] for key in fields)
                    or canonical["size_bytes"] != row["size_bytes"] or canonical["sha256"] != row["sha256"]
                    or (part.get("modality") == "audio" and part.get("semantic_role") != canonical.get("semantic_role"))
                    or str(path.absolute()) != canonical["local_ref"]):
                _deny(attachment_id, integrity=True)
            if secure_grants:
                grants.append(AttachmentReadGrant(attachment_id, path.absolute(), int(observed.st_dev), int(observed.st_ino),
                    int(row["size_bytes"]), str(row["sha256"]), ancestors))
        return tuple(grants)
    except MultimodalContractError:
        raise
    except (SessionStoreError, AudioAssetError, OSError, ValueError, KeyError, TypeError):
        _deny(attachment_id)


async def released_codex_voice_input(manager: Any, request_id: str, prompt: str, content):
    """Consume the existing same-Run STT gate; never initiate transcription.

Only Codex's text CLI needs this materialization. HER owns its native/audio
route and Safe Voice stage decisions. Ordinary audio files retain routing.
"""
    from orchestrator.multimodal_contract import (
        MultimodalContractError, canonical_request_content, normalize_request_content,
        validate_authorized_media_references,
    )
    from orchestrator.voice_transcript_gate import await_authorized_transcript
    from adapters.codex_cli import CodexCLIAdapter

    if not isinstance(manager.current_backend, CodexCLIAdapter):
        return prompt, content, ()
    normalized = normalize_request_content(content)
    voice = [part for part in (normalized or {}).get("parts", ())
             if part.get("type") == "media" and part.get("modality") == "audio"
             and part.get("semantic_role") == "voice_message"]
    if not voice:
        return prompt, content, ()
    attachment_ids = {str(part["attachment_id"]) for part in voice}
    runtime = manager.runtime
    registry = getattr(runtime, "_native_voice_transcripts", {})
    state = registry.get(request_id) if isinstance(registry, dict) else None
    if (not isinstance(state, dict) or state.get("request_id") != request_id
            or set(state.get("attachment_ids") or ()) != attachment_ids
            or any(registry.get(identifier) is not state for identifier in attachment_ids)):
        _deny(str(voice[0]["attachment_id"]))
    # Check intake integrity before trusting derived input, and once again
    # after a potentially long confirmation wait. The fd fence remains active.
    validate_authorized_media_references(normalized, authorized_roots=manager.current_backend.authorized_media_roots())
    _, status = await await_authorized_transcript(state, require_confirmation=True)
    if status != "released":
        raise MultimodalContractError(
            f"Voice transcript is {status}; no Codex model was invoked",
            code="MEDIA_FALLBACK_UNAVAILABLE", attachment_id=str(voice[0]["attachment_id"]),
        )
    # Re-resolve Run/fence/session after waiting; a reset/cancelled Run cannot
    # authorize its stale transcript even if the in-memory gate was released.
    current_run_attachment_grants(manager, request_id, normalized)
    validate_authorized_media_references(normalized, authorized_roots=manager.current_backend.authorized_media_roots())
    store = runtime.session_store
    run = store.get_run_by_request(request_id, agent_id=manager.config.name)
    with store._lock, store._connection() as connection:
        rows = connection.execute("SELECT * FROM voice_transcripts WHERE run_id=? AND session_id=? AND message_id=? ORDER BY created_at,transcript_id",
            (run["run_id"], run["session_id"], run["user_message_id"])).fetchall()
    by_attachment = {str(row["attachment_id"]): dict(row) for row in rows}
    if (not attachment_ids.issubset(by_attachment)
            or any(by_attachment[identifier]["safe_voice_state"] != "released"
                   or by_attachment[identifier]["provenance"] != "local_stt"
                   or not str(by_attachment[identifier]["text"]).strip() for identifier in attachment_ids)):
        _deny(str(voice[0]["attachment_id"]))
    transcript = "\n".join(str(by_attachment[str(part["attachment_id"])]["text"]).strip() for part in voice)
    remaining = [part for part in normalized["parts"] if part not in voice]
    remaining.append({"type": "text", "text": transcript})
    remaining = [dict(part, item_index=index) for index, part in enumerate(remaining, 1)]
    routes = tuple({"attachment_id": str(part["attachment_id"]), "item_index": part["item_index"],
        "modality": "audio", "route": "local_transcript", "reason": "local_stt_released",
        "transport": "text", "transcript_id": by_attachment[str(part["attachment_id"])]["transcript_id"]} for part in voice)
    return prompt + "\n\nLOCAL_VOICE_TRANSCRIPT (authorized same-Run user input):\n" + transcript, canonical_request_content(remaining), routes
