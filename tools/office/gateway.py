"""PAO-owned Office gateway: scoped handles, immutable versions, safe commit.

The sidecar never acquires user paths or delivers files. It receives copies in
one invocation directory. Canonical ToolRegistry audit and Frontend services
remain the owners of execution evidence and conversation delivery.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path
from contextlib import nullcontext, contextmanager
from uuid import uuid4

from .contract import OfficeInputError, validate_office_arguments
from .runtime import OfficeRuntime, RuntimeUnavailable, canonical, digest
from .limits import DEFAULT_BUDGETS

SUFFIXES = {".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm", ".pdf", ".csv"}


class GatewayError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def checks(**changes):
    return {"saved": False, "structure": "not_checked",
            "recalculation": "not_performed", "visual": "not_checked",
            "delivered": False, **changes}


def result(status="ok", code="OK", file_id=None, summary="", data=None, next_call=None, **verified):
    return {"status": status, "code": code, "file_id": file_id, "summary": summary,
            "data": data or {}, "next": next_call, "checks": checks(**verified)}


def next_tool(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


def _no_links(path):
    current = path
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise GatewayError("PATH_DENIED", "Linked files/directories are not supported.")
        if current.parent == current:
            break
        current = current.parent


class OfficeGateway:
    def __init__(self, runtime: OfficeRuntime, *, read_roots, write_roots, cwd, context,
                 path_guard=None, attachment_loader=None, delivery=None, effect_guard=None,
                 working_copy_loader=None, working_copy_lister=None, working_copy_commit_guard=None):
        self.runtime = runtime
        self.file_budget = getattr(runtime, "budgets", DEFAULT_BUDGETS)["file_bytes"]
        self.read_roots = tuple(Path(p).resolve() for p in read_roots)
        self.write_roots = tuple(Path(p).resolve() for p in write_roots)
        self.cwd = Path(cwd).resolve()
        self.context = dict(context)
        self.path_guard = path_guard
        self.attachment_loader = attachment_loader
        self.working_copy_loader = working_copy_loader
        self.working_copy_lister = working_copy_lister
        self.working_copy_commit_guard = working_copy_commit_guard
        self.delivery = delivery
        self.effect_guard = effect_guard or nullcontext
        config = self.context.get("global_config")
        identity = [str(self.context.get("instance_id") or getattr(config, "instance_id", "")),
                    str(self.context.get("agent_name") or ""),
                    str(self.context.get("hashi_session_id") or "")]
        if not all(identity):
            raise GatewayError("CONTEXT_REQUIRED", "Office tools require exact instance, Agent and Session authority.")
        self.scope = hashlib.sha256(canonical(identity)).hexdigest()
        if not runtime.options.get("state_root") or not runtime.state_root.is_absolute():
            raise RuntimeUnavailable("Office state_root must be explicitly configured and absolute.")
        self.root = runtime.state_root / self.scope
        _no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "office.sqlite"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files(
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, suffix TEXT NOT NULL,
                    sha256 TEXT NOT NULL, revision INTEGER NOT NULL,
                    origin TEXT, origin_sha256 TEXT, output TEXT, saved TEXT,
                    facts TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS operations(
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
                    file_id TEXT, result TEXT, plan TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS read_cursors(
                    id TEXT PRIMARY KEY, file_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL, target TEXT NOT NULL, view TEXT NOT NULL,
                    state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS working_copy_cursors(
                    id TEXT PRIMARY KEY, source_session TEXT NOT NULL, access_scope TEXT NOT NULL,
                    after_id TEXT NOT NULL, UNIQUE(source_session, access_scope, after_id));
            """)
            if "facts" not in {r[1] for r in db.execute("PRAGMA table_info(files)")}:
                db.execute("ALTER TABLE files ADD COLUMN facts TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def resolve(self, raw, *, write=False):
        from tools.builtins import _resolve_path
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        # Read aliases are resolved to an authorized real path and opened
        # through a pinned, no-follow handle in read_bytes. Publication still
        # retains the existing stricter path rule until its own atomic binding
        # supports aliases as well.
        if write:
            _no_links(candidate)
        try:
            resolved = _resolve_path(str(candidate), self.write_roots if write else self.read_roots, self.cwd)
        except ValueError:
            raise GatewayError("PATH_DENIED", "Path is outside this request's authorized Workzones.") from None
        # Reject Windows alternate streams/devices even on cross-platform tests.
        if "\x00" in str(raw) or (os.name == "nt" and ":" in str(resolved)[3:]):
            raise GatewayError("PATH_DENIED", "Alternate streams and device paths are unsupported.")
        if self.path_guard:
            self.path_guard(resolved, write)
        return resolved

    def read_bytes(self, path):
        from orchestrator.session_attachment_authorization import _open_no_symlinks

        path = self.resolve(str(path))
        try:
            descriptor, _ancestors = _open_no_symlinks(path, os.O_RDONLY)
        except OSError as error:
            raise GatewayError("SOURCE_CHANGED", "Input could not be opened safely; inspect it again.") from error

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

        # Read through the already-bound file, not a second path lookup.  The
        # helper pins each directory while opening and refuses replacement
        # links; Windows additionally prevents writes to the open handle.
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise GatewayError("FILE_LIMIT", "Input must be a regular file.")
            if before.st_size > self.file_budget:
                raise GatewayError("FILE_LIMIT", f"Input {before.st_size} bytes exceeds file_bytes budget {self.file_budget}; increase the configured resource budget.")
            data = source.read(self.file_budget + 1)
            after = os.fstat(source.fileno())
            try:
                current = path.stat()
            except OSError as error:
                raise GatewayError("SOURCE_CHANGED", "Input changed while being read; inspect it again.") from error
            if (len(data) > self.file_budget or identity(before) != identity(after)
                    or identity(after) != identity(current) or before.st_ctime_ns != after.st_ctime_ns):
                raise GatewayError("SOURCE_CHANGED", "Input changed while being read; inspect it again.")
        return data

    def handle(self, file_id, *, check_origin=False):
        with self.db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise GatewayError("FILE_NOT_FOUND", "File handle is absent in this Agent/Session. Inspect the input first.")
        item = dict(row)
        path = Path(item["path"])
        _no_links(path)
        if not path.is_relative_to(self.root) or not path.is_file() or digest(path) != item["sha256"]:
            raise GatewayError("WORKING_COPY_CHANGED", "Working copy integrity failed. Original retained; inspect the source again.")
        if check_origin:
            origins = json.loads(item["facts"] or "{}").get("origins", [])
            if item["origin"] and not origins:
                origins = [{"path": item["origin"], "sha256": item["origin_sha256"]}]
            for source in origins:
                if hashlib.sha256(self.read_bytes(Path(source["path"]))).hexdigest() != source["sha256"]:
                    raise GatewayError("SOURCE_CHANGED", "An original/template/asset changed outside Office. Inspect current sources before editing.")
        return item

    def source_provenance(self, item):
        """Describe source freshness without vetoing use of the bound working copy."""
        origins = json.loads(item["facts"] or "{}").get("origins", [])
        if item["origin"] and not origins:
            origins = [{"path": item["origin"], "sha256": item["origin_sha256"]}]
        if not origins:
            return "independent"
        unavailable = False
        for source in origins:
            try:
                actual = hashlib.sha256(self.read_bytes(Path(source["path"]))).hexdigest()
            except (GatewayError, OSError, KeyError, TypeError, ValueError):
                unavailable = True
                continue
            if actual != source.get("sha256"):
                return "changed"
        return "unavailable" if unavailable else "current"

    def read_cursor(self, token, handle):
        with self.db() as db:
            row = db.execute("SELECT * FROM read_cursors WHERE id=?", (token,)).fetchone()
        if not row or row["file_id"] != handle["id"]:
            raise GatewayError("INVALID_CURSOR", "Continuation does not belong to this file or session.")
        if row["revision"] != handle["revision"] or row["sha256"] != handle["sha256"]:
            raise GatewayError("STALE_CURSOR", "The working file changed; start a new read.")
        return dict(row)

    def save_read_cursor(self, handle, target, view, state):
        token = "read_" + uuid4().hex
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision,sha256 FROM files WHERE id=?", (handle["id"],)).fetchone()
            if not current or current["revision"] != handle["revision"] or current["sha256"] != handle["sha256"]:
                raise GatewayError("STALE_CURSOR", "The working file changed; start a new read.")
            db.execute("INSERT INTO read_cursors VALUES(?,?,?,?,?,?,?)",
                       (token, handle["id"], handle["sha256"], handle["revision"],
                        target, view, json.dumps(state)))
        return token

    def op_key(self, name, call_id):
        return hashlib.sha256(canonical([self.context.get("request_id"), call_id or uuid4().hex, name])).hexdigest()

    def _begin(self, op, name, args):
        fingerprint = hashlib.sha256(canonical([name, args])).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed owner cannot later commit a retired intent: both the
            # version transaction and save preparation compare this state.
            expired = db.execute("SELECT id,file_id FROM operations WHERE state='running' AND created<?",
                                 (time.time()-max(300,self.runtime.timeout+30),)).fetchall()
            for old in expired:
                retired = result("error","OPERATION_ABORTED",old["file_id"],
                                 "An abandoned invocation expired without committing a version. Inspect the current file before continuing.")
                db.execute("UPDATE operations SET state='done',result=? WHERE id=? AND state='running'",
                           (json.dumps(retired),old["id"]))
            row = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
            if row:
                if row["digest"] != fingerprint:
                    raise GatewayError("IDEMPOTENCY_CONFLICT", "This tool call ID is already bound to different arguments.")
                return dict(row)
            if args.get("file_id") and name in {"office_edit", "office_finish"}:
                pending = db.execute("SELECT * FROM operations WHERE file_id=? AND state IN ('running','prepared') ORDER BY created LIMIT 1", (args["file_id"],)).fetchone()
                if pending and pending["state"] == "prepared" and name == "office_finish":
                    db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                               (op, fingerprint, "running", args["file_id"], None, None, time.time()))
                    return {**dict(pending), "source_op": pending["id"]}
                if pending:
                    raise GatewayError("EDIT_CONFLICT", "This file has an active/unreconciled Office call. Inspect it before editing.")
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                       (op, fingerprint, "running", args.get("file_id"), None, None, time.time()))
        return None

    def _record(self, op, response, *, handle=None, expected_revision=None,
                commit_guard=None, revalidate=None):
        with self.effect_guard(), (commit_guard or nullcontext()), self.db() as db:
            if revalidate is not None:
                revalidate()
            db.execute("BEGIN IMMEDIATE")
            intent = db.execute("SELECT state FROM operations WHERE id=?", (op,)).fetchone()
            if not intent or intent["state"] not in {"running","prepared"}:
                raise GatewayError("EDIT_CONFLICT", "This Office intent has been retired; its candidate cannot commit.")
            if handle is not None:
                if expected_revision is None:
                    # Retained working copies do not impose a count limit;
                    # actual storage capacity governs new files.
                    db.execute("INSERT INTO files(id,path,suffix,sha256,revision,origin,origin_sha256,output,saved,facts) VALUES(:id,:path,:suffix,:sha256,:revision,:origin,:origin_sha256,:output,:saved,:facts)", handle)
                else:
                    changed = db.execute("""UPDATE files SET path=:path,suffix=:suffix,sha256=:sha256,
                         revision=:revision,output=:output,saved=:saved,facts=:facts WHERE id=:id AND revision=:expected""",
                         {**handle, "expected": expected_revision}).rowcount
                    if changed != 1:
                        raise GatewayError("EDIT_CONFLICT", "Another Office call changed this file. Inspect its latest version.")
            db.execute("UPDATE operations SET state='done',result=?,file_id=? WHERE id=?",
                       (json.dumps(response, ensure_ascii=False), response.get("file_id"), op))

    def _authorize_working_copy_origins(self, item):
        try:
            facts = json.loads(item["facts"] or "{}")
            if not isinstance(facts, dict) or not isinstance(facts.get("origins", []), list):
                raise ValueError("invalid origins")
            origins = [entry["path"] for entry in facts.get("origins", [])]
            if item["origin"]:
                origins.append(item["origin"])
            if any(not isinstance(path, str) or not path for path in origins):
                raise ValueError("invalid source path")
        except (KeyError, TypeError, ValueError):
            raise GatewayError("WORKING_COPY_CHANGED", "The source working-file metadata is invalid.") from None
        # Original inputs and assets remain subject to the current request's
        # selected Workzones, including when a completed result is replayed.
        for origin in dict.fromkeys(origins):
            self.resolve(origin)

    def list_working_copies(self, after):
        """List one bounded page; authorization is checked on each record."""
        with self.db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM files WHERE id>? ORDER BY id LIMIT 51", (after,),
            ).fetchall()]
        files = []
        for item in rows[:50]:
            try:
                self._authorize_working_copy_origins(item)
                suffix = str(item["suffix"]).lower()
                if suffix not in SUFFIXES or type(item["revision"]) is not int or item["revision"] < 1:
                    continue
                name = "Office" + suffix
                if item["origin"]:
                    name = Path(item["origin"]).name
                elif item["output"]:
                    try:
                        name = self.resolve(item["output"]).name
                    except (GatewayError, OSError, TypeError, ValueError):
                        # The working copy may still be authorized even when
                        # its proposed output path is outside this Workzone.
                        pass
                files.append({"file_id": item["id"], "revision": item["revision"],
                              "name": name, "format": suffix.lstrip("."),
                              "availability": "not_verified"})
            except (GatewayError, OSError, TypeError, ValueError):
                # Do not expose a name or ID from outside the selected scope.
                continue
        return {"files": files, "last_id": rows[49]["id"] if len(rows) > 50 else None}

    def working_copy_cursor(self, source_session, after=None, token=None):
        roots = sorted({os.path.normcase(str(root)) for root in self.read_roots})
        scope = hashlib.sha256(canonical(roots)).hexdigest()
        if token is not None:
            with self.db() as db:
                row = db.execute(
                    "SELECT after_id FROM working_copy_cursors WHERE id=? AND source_session=? AND access_scope=?",
                    (token, source_session, scope),
                ).fetchone()
            if row is None:
                raise GatewayError("INVALID_CURSOR", "Working-copy continuation belongs to another Session or Workzone.")
            return row["after_id"]
        if after is None:
            return None
        with self.effect_guard(), self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO working_copy_cursors(id,source_session,access_scope,after_id) VALUES(?,?,?,?)",
                ("wc_" + uuid4().hex, source_session, scope, after),
            )
            row = db.execute(
                "SELECT id FROM working_copy_cursors WHERE source_session=? AND access_scope=? AND after_id=?",
                (source_session, scope, after),
            ).fetchone()
            if row is None:
                raise GatewayError("WORKING_COPY_UNAVAILABLE", "Working-copy continuation is unavailable.")
            return row["id"]

    def snapshot_working_copy(self, file_id, expected_revision, job):
        """Copy the exact committed working version from this authorized Session."""
        from orchestrator.session_attachment_authorization import _open_no_symlinks

        with self.db() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                raise GatewayError("FILE_NOT_FOUND", "The selected working file is unavailable.")
            item = dict(row)
            if expected_revision is not None and item["revision"] != expected_revision:
                raise GatewayError("SOURCE_CHANGED", "The source working copy changed; inspect its current revision.")
            self._authorize_working_copy_origins(item)
            source = Path(item["path"])
            suffix = str(item["suffix"]).lower()
            if not source.is_absolute() or suffix not in SUFFIXES or source.suffix.lower() != suffix:
                raise GatewayError("WORKING_COPY_CHANGED", "The source working file is invalid.")
            _no_links(source)
            try:
                source = source.resolve(strict=True)
                if not source.is_relative_to(self.root.resolve(strict=True)):
                    raise GatewayError("WORKING_COPY_CHANGED", "The source working file is outside its Session.")
                descriptor, _ancestors = _open_no_symlinks(source, os.O_RDONLY)
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > self.file_budget:
                        raise GatewayError("FILE_LIMIT", "The source working file exceeds the selected file budget.")
                    data = stream.read(self.file_budget + 1)
                    after = os.fstat(stream.fileno())
                    current = source.stat()
            except OSError as exc:
                raise GatewayError("WORKING_COPY_CHANGED", "The source working file is unavailable or changed.") from exc

            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

            # Windows can report a different creation timestamp through a
            # pathname than through an open handle; compare handle timestamps
            # with each other, and file identity plus content across both.
            if (len(data) > self.file_budget or identity(before) != identity(after)
                    or before.st_ctime_ns != after.st_ctime_ns
                    or identity(after) != identity(current)
                    or hashlib.sha256(data).hexdigest() != item["sha256"]):
                raise GatewayError("WORKING_COPY_CHANGED", "The source working file failed version verification.")
            staged = Path(job) / ("input" + suffix)
            with staged.open("xb") as target:
                target.write(data)
            return {**item, "path": str(staged)}

    def stage_handle(self, handle, job, name="input"):
        source = Path(handle["path"])
        target = job / (name + handle["suffix"])
        target.write_bytes(source.read_bytes())
        return target.name

    def _assets(self, text, job, origins):
        if not isinstance(text, str) or not text.lstrip().startswith(("{", "[")):
            return text
        try:
            content = json.loads(text)
        except ValueError:
            return text
        def visit(value):
            if isinstance(value, dict):
                for key, item in list(value.items()):
                    if key == "image" and isinstance(item, str):
                        path = self.resolve(item)
                        data = self.read_bytes(path)
                        origins.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
                        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")) or len(data) > 8 * 1024 * 1024:
                            raise GatewayError("INVALID_IMAGE", "Only PNG/JPEG images up to 8 MiB are accepted.")
                        name = "asset-" + hashlib.sha256(data).hexdigest()[:16] + (".png" if data.startswith(b"\x89PNG") else ".jpg")
                        (job / name).write_bytes(data)
                        value[key] = name
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(content)
        return json.dumps(content, ensure_ascii=False)

    async def _invoke(self, operation, args, job):
        value = await self.runtime.invoke(operation, args, job)
        if value.get("status") != "ok":
            raise GatewayError(value.get("code", "OFFICE_FAILED"), value.get("summary", "Office operation failed."))
        return value

    def _new_handle(self, file_id, path, *, origin=None, output=None, origins=(), facts=None):
        return {"id": file_id, "path": str(path), "suffix": path.suffix.lower(),
                "sha256": digest(path), "revision": 1, "origin": str(origin) if origin else None,
                "origin_sha256": digest(path) if origin else None, "output": str(output) if output else None, "saved": None,
                "facts": json.dumps({**(facts or {}), "origins": list(origins) or ([{"path":str(origin),"sha256":digest(path)}] if origin else [])})}

    async def execute(self, name, arguments, call_id=""):
        file_id = str(arguments.get("file_id") or "") or None
        op = None
        owns_intent = False
        try:
            args = validate_office_arguments(name, arguments)
            self.runtime.manifest(verify=False)
            if name == "office_inspect" and args.get("source") == "list_working_copies":
                if self.working_copy_lister is None:
                    raise GatewayError("WORKING_COPY_UNAVAILABLE", "Authorized Session working copies are unavailable.")
                source_session = args["source_session_id"]
                after = (self.working_copy_cursor(source_session, token=args["cursor"])
                         if args.get("cursor") else "")
                page = self.working_copy_lister(source_session, after)
                cursor = self.working_copy_cursor(source_session, after=page["last_id"])
                return result(
                    summary="Listed authorized working copies; reopen a selected version to verify it.",
                    data={"files": page["files"], "next_cursor": cursor},
                    next_call=next_tool("office_inspect", source="list_working_copies",
                                        source_session_id=source_session, cursor=cursor) if cursor else None,
                )
            origins = []
            attachment_facts = {}
            reopened_revision = None
            # A reserve is an explicit policy for writes; reads can use their
            # staged snapshot while the filesystem has sufficient real space.
            minimum_free = max(0, int(self.runtime.options.get("minimum_free_bytes", 0)))
            if (name not in {"office_inspect", "office_read"} and minimum_free
                    and shutil.disk_usage(self.root).free < minimum_free):
                raise GatewayError("STORAGE_LIMIT", "The configured Office write reserve is unavailable.")
            op = self.op_key(name, call_id)
            prior = self._begin(op, name, args)
            if prior:
                if prior["state"] == "done":
                    previous = json.loads(prior["result"])
                    if (name == "office_inspect" and args.get("source") == "working_copy"
                            and previous.get("status") == "ok" and previous.get("file_id")):
                        self._authorize_working_copy_origins(self.handle(previous["file_id"]))
                    return previous
                if prior["state"] == "prepared" and name == "office_finish":
                    return await self._recover_finish(op, prior, args)
                raise GatewayError("OPERATION_UNCERTAIN", "Previous call has no completed receipt. Inspect the file; do not blindly repeat a mutation.")
            owns_intent = True
            job = self.root / "jobs" / op
            job.mkdir(parents=True, exist_ok=False)
            if name == "office_inspect":
                if args.get("source") == "list_attachments":
                    if self.attachment_loader is None:
                        raise GatewayError("ATTACHMENT_UNAVAILABLE", "No canonical attachment source is bound to this request.")
                    page = self.attachment_loader(list_only=True, cursor=args.get("cursor"))
                    next_cursor = page.get("next_cursor")
                    response = result(
                        summary="Listed visible Office attachments in this Session.", data=page,
                        next_call=next_tool("office_inspect", source="list_attachments", cursor=next_cursor)
                        if next_cursor else None,
                    )
                    self._record(op, response)
                    return response
                if args.get("source") == "working_copy":
                    if self.working_copy_loader is None:
                        raise GatewayError("WORKING_COPY_UNAVAILABLE", "Authorized Session working copies are unavailable.")
                    handle = self.working_copy_loader(
                        args["source_session_id"], args["source_file_id"], args.get("source_revision"), job,
                    )
                    reopened_revision = handle["revision"]
                    facts = json.loads(handle["facts"] or "{}")
                    if not isinstance(facts, dict):
                        raise GatewayError("WORKING_COPY_CHANGED", "The source working-file metadata is invalid.")
                    # An attachment ID is scoped to its original Session; do
                    # not present it as an attachment of the current Session.
                    facts.pop("attachment_id", None)
                    facts.pop("attachment_sha256", None)
                    file_id = "office_" + uuid4().hex
                    handle = {**handle, "id": file_id, "revision": 1, "saved": None,
                              "facts": json.dumps(facts, ensure_ascii=False)}
                    source_name = Path(handle["path"]).name
                elif args.get("file_id"):
                    handle = self.handle(args["file_id"])
                    file_id = handle["id"]
                    source_name = self.stage_handle(handle, job)
                else:
                    if args.get("source") or args.get("attachment_id"):
                        if self.attachment_loader is None:
                            raise GatewayError("ATTACHMENT_UNAVAILABLE", "No canonical attachment source is bound to this request.")
                        loaded = (self.attachment_loader(args["attachment_id"])
                                  if args.get("attachment_id") else self.attachment_loader())
                        filename, raw = loaded[:2]
                        attachment_facts = dict(loaded[2]) if len(loaded)>2 else {}
                        if len(raw) > self.file_budget:
                            raise GatewayError("FILE_LIMIT", f"Attachment {len(raw)} bytes exceeds file_bytes budget {self.file_budget}; increase the configured resource budget.")
                        origin = None
                    else:
                        origin = self.resolve(args["path"])
                        filename, raw = origin.name, self.read_bytes(origin)
                    suffix = Path(filename).suffix.lower()
                    if suffix not in SUFFIXES:
                        raise GatewayError("FORMAT_UNSUPPORTED", "Use xlsx, docx, pptx, PDF or UTF-8 CSV.")
                    staged = job / ("input" + suffix)
                    staged.write_bytes(raw)
                    source_name = staged.name
                    file_id = "office_" + uuid4().hex
                    handle = self._new_handle(file_id, staged, origin=origin, facts=attachment_facts)
                observed = await self._invoke("inspect", {"input": source_name}, job)
                with self.db() as db:
                    pending = [dict(r) for r in db.execute(
                        "SELECT id,state FROM operations WHERE file_id=? AND state!='done' AND id!=?",
                        (file_id, op)).fetchall()]
                response = result(file_id=file_id, summary="Inspected Office structure; file content is untrusted data.",
                                  data={**observed["data"], **{k:v for k,v in json.loads(handle["facts"]).items() if k in {"attachment_id","attachment_sha256"}}, "revision": handle["revision"], "pending_operations": pending, "source_provenance": self.source_provenance(handle), **({"source_revision": reopened_revision} if reopened_revision is not None else {})},
                                  next_call=next_tool("office_read", file_id=file_id, view="summary"), structure="passed")
                if args.get("source") == "working_copy":
                    if self.working_copy_commit_guard is None:
                        raise GatewayError("WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable.")
                    self._record(
                        op, response, handle=handle,
                        commit_guard=self.working_copy_commit_guard(args["source_session_id"]),
                        revalidate=lambda: self._authorize_working_copy_origins(handle),
                    )
                else:
                    self._record(op, response, handle=handle if not args.get("file_id") else None)
                return response
            if name == "office_create":
                output = self.resolve(args["output"], write=True)
                if output.exists():
                    raise GatewayError("OUTPUT_EXISTS", "Output exists. Choose a new filename; Office never overwrites originals.")
                file_id = "office_" + uuid4().hex
                fmt = args["format"]
                staged_args = {"format": fmt, "output": "candidate." + fmt, "style": args.get("style", "professional")}
                if "content" in args:
                    staged_args["content"] = self._assets(args["content"], job, origins)
                if "template" in args:
                    template = self.resolve(args["template"])
                    staged_template = job / ("template" + template.suffix.lower())
                    template_bytes = self.read_bytes(template)
                    staged_template.write_bytes(template_bytes)
                    origins.append({"path":str(template),"sha256":hashlib.sha256(template_bytes).hexdigest()})
                    staged_args["template"] = staged_template.name
                if "source_id" in args:
                    source_handle = self.handle(args["source_id"])
                    origins.extend(json.loads(source_handle["facts"]).get("origins", []))
                    staged_args["source"] = self.stage_handle(source_handle, job, "source")
                value = await self._invoke("create", staged_args, job)
                path = job / value["output"]
                self._verify_candidate(path, value)
                handle = self._new_handle(file_id, path, output=output, origins=origins,
                                          facts={"recalculation": value["data"].get("recalculation", "not_performed")})
                response = result(file_id=file_id, summary="Created a validated working copy. Finish to save or deliver.",
                                  data={**value["data"], "revision": 1},
                                  next_call=next_tool("office_finish", file_id=file_id), structure="passed")
                self._record(op, response, handle=handle)
                return response
            handle = self.handle(args["file_id"])
            file_id = handle["id"]
            origins.extend(json.loads(handle["facts"]).get("origins", []))
            staged_args = {"input": self.stage_handle(handle, job)}
            if name == "office_read":
                target, view = args.get("target", ""), args.get("view", "summary")
                cursor = self.read_cursor(args["cursor"], handle) if args.get("cursor") else None
                continuation = None
                if cursor:
                    if (("target" in args and target != cursor["target"])
                            or ("view" in args and view != cursor["view"])):
                        raise GatewayError("INVALID_CURSOR", "Continuation target or view does not match the original read.")
                    target, view = cursor["target"], cursor["view"]
                    state = json.loads(cursor["state"])
                    if state.get("kind") == "target":
                        staged_args["target"] = state["target"]
                    else:
                        continuation = state
                staged_args.setdefault("target", target)
                staged_args["view"] = view
                if continuation is not None:
                    staged_args["continuation"] = continuation
                value = await self._invoke("read", staged_args, job)
                data = value["data"]
                tables_target = data.get("tables_next_target")
                if tables_target:
                    tables_cursor = self.save_read_cursor(handle, tables_target, view,
                                                          {"kind": "target", "target": tables_target})
                    data["tables_next"] = next_tool("office_read", file_id=file_id, cursor=tables_cursor)
                next_state = data.pop("_continuation", None)
                if not next_state and data.get("next_target"):
                    next_state = {"kind": "target", "target": data["next_target"]}
                next_cursor = self.save_read_cursor(handle, target, view, next_state) if next_state else None
                if next_cursor:
                    data["next_cursor"] = next_cursor
                response = result(file_id=file_id, summary="Read requested Office part.",
                                  data={**data,**{k:v for k,v in json.loads(handle["facts"]).items() if k in {"attachment_id","attachment_sha256"}}, "source_provenance": self.source_provenance(handle)},
                                  next_call=next_tool("office_read", file_id=file_id, cursor=next_cursor) if next_cursor else None,
                                  structure="passed")
                self._record(op, response)
                return response
            if name == "office_edit":
                suffix = handle["suffix"]
                if args["action"] == "office_convert":
                    try:
                        content = json.loads(args["content"])
                    except ValueError:
                        content = args["content"]
                    fmt = content.get("format") if isinstance(content, dict) else content
                    if fmt not in {"pdf", "docx", "xlsx", "pptx", "csv"}:
                        raise GatewayError("FORMAT_UNSUPPORTED", "office_convert format: pdf, docx, xlsx, pptx or csv.")
                    suffix = "." + fmt
                staged_args.update(action=args["action"], target=args.get("target", ""),
                                   content=self._assets(args.get("content"), job, origins),
                                   output="candidate" + suffix)
                if args.get("source_id"):
                    source_handle = self.handle(args["source_id"])
                    origins.extend(json.loads(source_handle["facts"]).get("origins", []))
                    staged_args["source"] = self.stage_handle(source_handle, job, "source")
                value = await self._invoke("edit", staged_args, job)
                path = job / value["output"]
                self._verify_candidate(path, value)
                updated = {**handle, "path": str(path), "suffix": suffix, "sha256": value["sha256"],
                           "revision": handle["revision"] + 1, "saved": None,
                           "facts": json.dumps({"recalculation": value["data"].get("recalculation", "not_performed"),
                                                "conversion_fidelity": value["data"].get("conversion_fidelity"), "origins": origins})}
                if suffix != handle["suffix"]:
                    updated["output"] = str(Path(handle["output"]).with_suffix(suffix)) if handle["output"] else None
                response = result(file_id=file_id, summary="Edited working copy atomically; original retained.",
                                  data={**value["data"], "revision": updated["revision"], "source_provenance": self.source_provenance(updated)},
                                  next_call=next_tool("office_finish", file_id=file_id), structure="passed",
                                  recalculation=value["data"].get("recalculation", "not_performed"))
                self._record(op, response, handle=updated, expected_revision=handle["revision"])
                return response
            if name == "office_finish":
                return await self._finish(op, args, handle, staged_args, job)
            raise GatewayError("UNKNOWN_TOOL", "Unknown Office tool.")
        except asyncio.CancelledError:
            # Runtime cleanup finishes before propagation. Retire an uncommitted
            # edit; a prepared save remains available for hash reconciliation.
            if op and owns_intent:
                cancelled = result("error","OPERATION_CANCELLED",args.get("file_id"),
                                   "Invocation cancelled; no working version committed. Inspect the current file.")
                with self.db() as db:
                    db.execute("UPDATE operations SET state='done',result=? WHERE id=? AND state='running'",
                               (json.dumps(cancelled),op))
            raise
        except (GatewayError, RuntimeUnavailable, OfficeInputError, asyncio.TimeoutError, OSError, ValueError) as exc:
            disk_full = isinstance(exc, OSError) and (
                exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
                or getattr(exc, "winerror", None) in {39, 112}
            )
            code = getattr(exc, "code", None) or ("STORAGE_LIMIT" if disk_full else
                                                "OPERATION_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else
                                                "RUNTIME_UNAVAILABLE" if isinstance(exc, RuntimeUnavailable) else "OFFICE_FAILED")
            if file_id:
                with self.db() as db:
                    if not db.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
                        file_id = None
            response = result("error", code, file_id, str(exc) or "Office invocation timed out; original retained.",
                              next_call=next_tool("office_inspect", file_id=file_id) if file_id else None)
            if op and owns_intent:
                with self.db() as db:
                    row = db.execute("SELECT state FROM operations WHERE id=?", (op,)).fetchone()
                if row and row["state"] == "running":
                    self._record(op, response)
            return response

    def _verify_candidate(self, path, value):
        if not path.is_file() or path.stat().st_size > self.file_budget or digest(path) != value.get("sha256"):
            raise GatewayError("INVALID_RECEIPT", "Candidate file does not match the isolated runtime receipt.")

    @staticmethod
    def _atomic_rename_no_replace(source, target):
        """Publish a same-directory temporary file without replacing a target."""
        if os.name == "nt":
            # Python's Windows rename fails when the destination already exists.
            os.rename(source, target)
            return
        if sys.platform.startswith("linux"):
            import ctypes
            library = ctypes.CDLL(None, use_errno=True)
            rename = getattr(library, "renameat2", None)
            if rename is not None:
                rename.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                   ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                rename.restype = ctypes.c_int
                if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
                    return
                number = ctypes.get_errno()
                if number not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise OSError(number, os.strerror(number), str(target))
        raise GatewayError("ATOMIC_PUBLISH_UNSUPPORTED",
                           "This filesystem cannot safely publish without replacing an existing file.")

    @staticmethod
    def _atomic_new_file(source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        _no_links(target.parent)
        temporary = target.parent / (".office-" + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream, source.open("rb") as reader:
                shutil.copyfileobj(reader, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if digest(temporary) != digest(source):
                raise GatewayError("COMMIT_VALIDATION_FAILED", "Saved bytes differ; original retained.")
            # Prefer link publication; use a platform no-replace rename when
            # links are unsupported. Both paths keep an existing target intact.
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise
            except OSError as exc:
                retryable = {errno.EPERM, errno.EACCES, errno.EXDEV, errno.EINVAL,
                             errno.ENOSYS, errno.EOPNOTSUPP, errno.EMLINK}
                if exc.errno not in retryable and getattr(exc, "winerror", None) not in {1, 5, 17, 50, 1314}:
                    raise
                OfficeGateway._atomic_rename_no_replace(temporary, target)
        except FileExistsError:
            raise GatewayError("OUTPUT_EXISTS", "Output already exists. Choose a new filename.") from None
        finally:
            temporary.unlink(missing_ok=True)

    async def _finish(self, op, args, handle, staged_args, job):
        output = self.resolve(args.get("output") or handle["output"] or
                              ("Office-" + handle["id"][-8:] + handle["suffix"]), write=True)
        if output.suffix.lower() != handle["suffix"]:
            raise GatewayError("FORMAT_MISMATCH", "Output suffix must match the working copy.")
        if handle["origin"] and output == Path(handle["origin"]):
            raise GatewayError("ORIGINAL_PROTECTED", "Choose a new output filename; the original remains unchanged.")
        if output.exists() and (str(output) != handle["saved"] or digest(output) != handle["sha256"]):
            raise GatewayError("OUTPUT_EXISTS", "Output exists. Choose a new filename.")
        staged_args["preview"] = args.get("preview", False)
        value = await self._invoke("finish", staged_args, job)
        facts = json.loads(handle["facts"] or "{}")
        info = {**value["data"], "recalculation":facts.get("recalculation","not_performed"),
                "output": str(output), "sha256": handle["sha256"],
                "revision": handle["revision"], "source_provenance": self.source_provenance(handle)}
        if value.get("preview"):
            preview = value["preview"]
            info["preview"] = {**preview}
            for key in ("path", "image"):
                if preview.get(key):
                    info["preview"][key] = str(job / preview[key])
        response = result(file_id=handle["id"], summary="Saved and reopened Office output.",
                          data=info, saved=True, structure="passed",
                          visual=value.get("preview", {}).get("visual", "not_checked"),
                          recalculation=facts.get("recalculation", "not_performed"))
        with self.db() as db:
            changed = db.execute("UPDATE operations SET state='prepared',plan=?,file_id=? WHERE id=? AND state='running'",
                                 (json.dumps(response, ensure_ascii=False), handle["id"], op)).rowcount
            if changed != 1:
                raise GatewayError("EDIT_CONFLICT","Save intent was retired before publication; no file was published.")
        if not output.exists():
            with self.effect_guard():
                self.resolve(str(output), write=True)
                self._atomic_new_file(Path(handle["path"]), output)
        if digest(output) != handle["sha256"]:
            raise GatewayError("COMMIT_VALIDATION_FAILED", "Output readback failed. Inspect the receipt before retrying.")
        return await self._complete_finish(op, args, handle, response)

    async def _recover_finish(self, op, prior, args):
        response = json.loads(prior["plan"])
        handle = self.handle(prior["file_id"])
        output = self.resolve(response["data"]["output"], write=True)
        if response["data"]["revision"] != handle["revision"] or response["data"]["sha256"] != handle["sha256"]:
            raise GatewayError("OPERATION_UNCERTAIN","Prepared save no longer matches the verified working version.")
        if not output.exists():
            with self.effect_guard():
                self.resolve(str(output),write=True)
                self._atomic_new_file(Path(handle["path"]),output)
        if not output.is_file() or digest(output) != response["data"]["sha256"]:
            raise GatewayError("OPERATION_UNCERTAIN", "Prepared save has a different output. Original retained; reconcile before retrying.")
        if args.get("output") and self.resolve(args["output"], write=True) != output:
            raise GatewayError("EDIT_CONFLICT", "Reconcile the prepared save at its original output before choosing a different output.")
        recovered = await self._complete_finish(op, args, handle, response)
        if prior.get("source_op"):
            self._record(prior["source_op"], recovered)
        return recovered

    async def _complete_finish(self, op, args, handle, response):
        output = Path(response["data"]["output"])
        if args.get("deliver"):
            if self.delivery is None:
                response.update(status="partial", code="DELIVERY_UNAVAILABLE",
                                summary="File saved. Originating conversation delivery is unavailable.")
            else:
                publication = "office-" + handle["id"] + "-v" + str(handle["revision"]) + "-" + hashlib.sha256(str(output).encode()).hexdigest()[:12]
                try:
                    delivery = await self.delivery(output, publication)
                    if not isinstance(delivery, dict) or not delivery.get("ok"):
                        raise ValueError("Frontend did not confirm publication persistence.")
                    destinations = delivery.get("deliveries") or []
                    response["data"]["delivery"] = {key:delivery.get(key) for key in ("publication_id","event_id","persisted")}
                    response["data"]["delivery"]["deliveries"] = [{key:d.get(key) for key in ("connector_id","state","last_error_code")} for d in destinations]
                    if isinstance(destinations, dict):
                        destinations = list(destinations.values())
                    delivered = bool(destinations) and all(
                        isinstance(d, dict) and d.get("state") == "delivered" for d in destinations)
                    response["checks"]["delivered"] = delivered
                    response["summary"] = ("File saved; delivery confirmed." if delivered else
                                           "File saved and published; channel delivery is pending confirmation.")
                    if not delivered:
                        response.update(status="partial", code="DELIVERY_PENDING")
                except Exception:
                    response.update(status="partial", code="DELIVERY_UNCONFIRMED",
                                    summary="File saved; delivery outcome is unconfirmed. Reconcile the publication before retrying.")
        with self.db() as db:
            db.execute("UPDATE files SET saved=?,output=? WHERE id=? AND revision=?",
                       (str(output), str(output), handle["id"], handle["revision"]))
        self._record(op, response)
        return response
# HASHI-Fingerprint: hfp_13c5a1235893417ea50c33dbf41e3b46
