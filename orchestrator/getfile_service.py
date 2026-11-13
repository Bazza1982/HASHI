"""PAO acquisition and durable selection binding for the /getfile Function."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from orchestrator import runtime_session
from orchestrator.session_store import MAX_SESSION_ATTACHMENT_BYTES

MENU_TTL = 900
TELEGRAM_MAX_BYTES = 50_000_000


class GetFileError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _identity(runtime, update):
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if not callable(checker) or not checker(actor):
        raise GetFileError("denied")
    # Existing Telegram outbox routes identify a chat, not a forum topic.
    # Fail closed rather than delivering a topic request into General.
    message = getattr(update, "effective_message", None)
    if getattr(message, "message_thread_id", None) is not None:
        raise GetFileError("unsupported_topic")
    session = runtime_session.current_session_for_update(runtime, update)
    surface, channel, owner, _ = runtime_session._update_session_route(runtime, update)
    if surface not in {"telegram", "workbench"} or not channel or (surface == "telegram" and channel == "default"):
        raise GetFileError("denied")
    expected = getattr(update, "_hashi_session_context_generation", None)
    if expected is not None and int(expected) != int(session["context_generation"]):
        raise GetFileError("stale")
    if str(session["agent_id"]).casefold() != str(runtime.name).casefold():
        raise GetFileError("denied")
    profile = runtime_session.agent_workzone_state(runtime, owner=owner)
    return {"workzone_revision": int(profile.get("revision") or 0),
            "instance": runtime_session.ensure_store(runtime).instance_id,
            "agent": runtime.name, "owner": owner, "session": session["session_id"],
            "generation": int(session["context_generation"]), "surface": surface,
            "channel": channel, "actor": str(actor)}


def current_roots(runtime, binding):
    from orchestrator.workzone import active_workzone_slots
    state = runtime_session.agent_workzone_state(runtime, owner=binding["owner"])
    roots = [Path(os.path.abspath(runtime.workspace_dir))]
    roots.extend(Path(os.path.abspath(slot["path"])) for slot in active_workzone_slots(state, available_only=True))
    return tuple(dict.fromkeys(roots))


def resolve_path(raw, roots, cwd):
    """Translate supported literal Windows/WSL representations without widening roots."""
    if not isinstance(raw, str):
        raise GetFileError("invalid_path")
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise GetFileError("invalid_path")
    from tools.device_paths import wsl_to_windows_path, windows_to_wsl_path
    try:
        if os.name == "nt" and value.startswith("/"):
            from pathlib import PureWindowsPath
            distros = {PureWindowsPath(root).drive.rsplit("\\", 1)[-1] for root in roots
                       if str(root).casefold().startswith(("\\\\wsl.localhost\\", "\\\\wsl$\\"))}
            value = wsl_to_windows_path(value, distro=next(iter(distros)) if len(distros) == 1 else None)
        elif os.name != "nt" and len(value) > 2 and value[1] == ":":
            value = windows_to_wsl_path(value) or value
        from tools.builtins import _resolve_path
        # Reuse scope resolution, but retain the lexical path: the secure opener
        # must see and reject symlinks/reparse points instead of following them.
        _resolve_path(value, roots, cwd)
        path = Path(value)
        path = Path(os.path.abspath(path if path.is_absolute() else Path(cwd) / path))
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("outside current roots")
        return path
    except (OSError, ValueError):
        raise GetFileError("denied") from None


def _guard(runtime, path):
    from orchestrator.live_runtime_protection import evaluate_live_runtime_request, load_live_runtime_policy
    try:
        owner = getattr(runtime, "orchestrator", None)
        policy = load_live_runtime_policy(runtime.global_config, runtime_prefix=None,
            core_runtime=getattr(owner, "core_runtime_fingerprint", None))
        if evaluate_live_runtime_request(policy, tool_name="file_read",
                arguments={"path": str(path)}, workspace_dir=runtime.workspace_dir) is not None:
            raise GetFileError("denied")
    except GetFileError:
        raise
    except (OSError, ValueError):
        raise GetFileError("denied") from None


def _signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def snapshot(runtime, path, *, limit, read=True):
    """Pin parents/file using the existing cross-platform attachment read fence."""
    _guard(runtime, path)
    from orchestrator.session_attachment_authorization import _open_no_symlinks
    try:
        fd, _ = _open_no_symlinks(path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise GetFileError("unavailable")
            if before.st_size > limit:
                raise GetFileError("too_large")
            if not read:
                return None, _signature(before)
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            if len(data) > limit:
                raise GetFileError("too_large")
            if _signature(before) != _signature(after) or len(data) != after.st_size:
                raise GetFileError("changing")
            return data, _signature(after)
    except GetFileError:
        raise
    except FileNotFoundError:
        raise GetFileError("missing") from None
    except PermissionError:
        raise GetFileError("denied") from None
    except OSError:
        raise GetFileError("unavailable") from None


class GetFileService:
    def __init__(self, runtime):
        self.runtime = runtime
        root = Path(runtime.global_config.bridge_home) / "state" / "getfile"
        root.mkdir(parents=True, exist_ok=True)
        # Instance/Agent isolation is explicit; names never confer identity.
        identity = hashlib.sha256((runtime_session.ensure_store(runtime).instance_id + "\0" + runtime.name).encode()).hexdigest()
        self.path = root / (identity + ".sqlite3")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS menus(
            token TEXT PRIMARY KEY, expires REAL NOT NULL, binding TEXT NOT NULL, items TEXT NOT NULL)""")
        return db

    def bind(self, update):
        return _identity(self.runtime, update)

    def menu(self, update):
        from orchestrator.getfile_index import RecentFileIndex
        binding = self.bind(update)
        audit = getattr(self.runtime, "canonical_audit", None)
        if audit is None:
            rows = []
        else:
            rows = RecentFileIndex(audit).recent(owner_id=binding["owner"])
        roots = current_roots(self.runtime, binding)
        items = []
        limit = TELEGRAM_MAX_BYTES if binding["surface"] == "telegram" else MAX_SESSION_ATTACHMENT_BYTES
        for row in rows:
            item = dict(row)
            try:
                path = resolve_path(str(Path(row["root_path"]) / row["relative_path"]), roots, roots[0])
                _, version = snapshot(self.runtime, path, limit=limit, read=False)
                item["version"] = version
            except GetFileError as exc:
                item["error"] = exc.code
            items.append(item)
        token = secrets.token_urlsafe(18)
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM menus WHERE expires<?", (time.time() - 86400,))
            db.execute("INSERT INTO menus VALUES(?,?,?,?)",
                       (token, time.time() + MENU_TTL, json.dumps(binding, sort_keys=True), json.dumps(items)))
            db.execute("DELETE FROM menus WHERE token NOT IN (SELECT token FROM menus ORDER BY expires DESC LIMIT 128)")
        return token, items

    def require(self, update, token):
        binding = self.bind(update)
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM menus WHERE token=?", (token,)).fetchone()
        if row is None or row["expires"] < time.time():
            raise GetFileError("expired")
        if json.loads(row["binding"]) != binding:
            raise GetFileError("stale")
        return binding, json.loads(row["items"])

    async def transfer(self, update, *, token=None, index=None, raw_path=None):
        """One selection -> one durable media publication. Never start an Engine."""
        binding, items = self.require(update, token) if token else (self.bind(update), [])
        if index is not None:
            if type(index) is not int or not 0 <= index < len(items):
                raise GetFileError("invalid_path")
            selected = items[index]
            raw_path = str(Path(selected["root_path"]) / selected["relative_path"])
        else:
            selected = {}
        if not token:
            # The ingress identity is stable across redelivery of the same command.
            message = getattr(update, "effective_message", None)
            invocation = (getattr(update, "_hashi_frontend_invocation_id", None)
                          or getattr(message, "message_id", None) or getattr(update, "update_id", None))
            if invocation is None:
                raise GetFileError("invalid_path")
            token = hashlib.sha256(json.dumps([binding, str(invocation), raw_path], sort_keys=True).encode()).hexdigest()
        operation = "getfile-" + hashlib.sha256((token + ":" + str(index if index is not None else raw_path)).encode()).hexdigest()
        key = f'{binding["surface"]}:{binding["channel"]}:publication:{operation}'
        runtime, store = self.runtime, runtime_session.ensure_store(self.runtime)
        publication = store.presentation_publication(session_id=binding["session"], owner_id=binding["owner"],
            agent_id=binding["agent"], idempotency_key=key)
        changed = False
        if publication is None:
            if selected.get("error"):
                raise GetFileError(selected["error"])
            roots = current_roots(runtime, binding)
            path = resolve_path(raw_path, roots, roots[1] if len(roots) > 1 else roots[0])
            limit = TELEGRAM_MAX_BYTES if binding["surface"] == "telegram" else MAX_SESSION_ATTACHMENT_BYTES
            import asyncio
            data, version = await asyncio.to_thread(snapshot, runtime, path, limit=limit)
            changed = bool(selected.get("version") and selected["version"] != version)
            # Fence a forget/Agent/owner switch that happened while reading bytes.
            if self.bind(update) != binding:
                raise GetFileError("stale")
            from orchestrator import ui_language
            caption = path.name + (" · " + ui_language.tr("getfile.changed") if changed else "")
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if media_type.startswith("audio/"):
                # File retrieval is not audio ingestion/transcoding. Preserve arbitrary bytes.
                media_type = "application/octet-stream"
            publication = runtime_session.publish_frontend_media_notification(
                runtime, filename=path.name, media_type=media_type,
                payload=data, sha256=hashlib.sha256(data).hexdigest(), caption=caption,
                publication_id=operation, surface=binding["surface"], channel_key=binding["channel"],
                presentation_role="document", explicit_owner_id=binding["owner"],
                explicit_session_id=binding["session"], expected_context_generation=binding["generation"],
                origin_only=True)
        attachment_id = str(publication.get("attachment_id") or publication["content"][0]["attachment_id"])
        part = store.visible_message_attachment(binding["session"], owner_id=binding["owner"],
            message_id=publication["message_id"], attachment_id=attachment_id,
            context_generation=binding["generation"])
        state = "published"
        if binding["surface"] == "telegram":
            from orchestrator.frontend_connector_registry import endpoint_id_for
            endpoint = endpoint_id_for("telegram", ingress_transport="telegram", channel_key=binding["channel"])
            if getattr(runtime, "telegram_connected", False):
                claims = store.claim_delivery_outbox(session_id=binding["session"], owner_id=binding["owner"],
                    worker_id=operation, event_id=publication["delivery_event_id"], connector_id="telegram",
                    endpoint_id=endpoint, limit=1)
                if claims:
                    from orchestrator.runtime_delivery import dispatch_claimed_telegram_event
                    await dispatch_claimed_telegram_event(runtime, chat_id=int(binding["channel"]), store=store,
                        claim=claims[0], frontend_owner_id=binding["owner"], request_id=operation,
                        purpose="explicit-media-notification")
            receipts = store.frontend_delivery_receipts(session_id=binding["session"], owner_id=binding["owner"],
                event_id=publication["delivery_event_id"])
            matching = [r for r in receipts if r.get("endpoint_id") == endpoint]
            state = str(matching[-1]["status"]) if matching else "queued"
        from orchestrator.attachment_catalogue import source_identity
        identity = source_identity(agent_id=binding["agent"], session_id=binding["session"])
        return {"getfile": {**identity, "version": 1, "instance_id": binding["instance"],
                "created_at": publication["created_at"], "session_id": binding["session"],
                "context_generation": binding["generation"], "agent_id": binding["agent"],
                "message_id": publication["message_id"], "attachment_id": attachment_id,
                "delivery_event_id": publication["delivery_event_id"], "delivery_state": state,
                "changed": changed, "filename": part.get("filename", part.get("name", "")),
                "sha256": part.get("sha256"), "size_bytes": part.get("size_bytes")}}
