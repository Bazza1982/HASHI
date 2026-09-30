"""Manual desktop endpoint inside the existing device worker.

One bounded preview per worker; an OS lock is held for the whole human control
lease, including gaps between input calls. All legacy agent mutations use the
same OS lock. Native dependencies are imported only in the Windows worker.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
from collections import OrderedDict

from orchestrator.desktop_contract import CONTROL_TTL, DesktopError, fields, identifier, validate_input, view_options
from tools.windows_helper.desktop_capture import fit_size, jpeg_bytes


class DesktopController:
    def __init__(self, native, lock_factory, *, clock=time.monotonic, watchdog=True):
        self.native, self.lock_factory, self.clock = native, lock_factory, clock
        self.guard = threading.RLock()
        self.capture_guard = threading.Lock()
        self.owner = None
        self.device_lock = None
        self.deadline = 0.0
        self.last_contact = 0.0
        self.view = None
        self.view_revision = ""
        self.display_revision = ""
        self.frame_cache = None
        self.frame_history = OrderedDict()
        self.last_capture = -100.0
        self.last_changed = self.last_input = self.clock()
        self.frames = 0
        self.last_seq = 0
        self.results = OrderedDict()
        self.rate_start, self.rate_count = self.clock(), 0
        self.stop_event = threading.Event()
        if watchdog:
            threading.Thread(target=self._watch, name="desktop-input-watchdog", daemon=True).start()

    def _watch(self):
        while not self.stop_event.wait(.5):
            self.expire()

    def expire(self):
        with self.guard:
            if self.owner and self.clock() >= self.deadline:
                self._release()
            elif self.owner and self.clock() - self.last_contact > 3:
                try: self.native.reset()
                except Exception: pass

    def close(self):
        self.stop_event.set()
        with self.guard:
            self._release()
            self.frame_cache = None
            self.frame_history.clear()

    def _release(self):
        try:
            if self.owner: self.native.reset()
        except Exception:
            # Windows may have switched to the secure desktop. No escalation.
            pass
        finally:
            if self.device_lock:
                self.device_lock.release()
            self.device_lock = self.owner = None
            self.deadline = 0
            self.last_seq = 0
            self.results.clear()

    def protects_input(self):
        self.expire()
        return self.owner is not None

    def _own(self, actor, session_id, lease_id):
        self.expire()
        if self.owner != (actor, session_id, lease_id):
            raise DesktopError("desktop_control_expired", 409)

    def _set_view(self, value, actor, session_id):
        with self.guard:
            self.expire()
            if self.owner and self.owner[:2] != (actor, session_id):
                raise DesktopError("desktop_control_busy", 409)
            view = view_options(value)
            rows, rev = self.native.displays()
            if view["display_id"] not in {r["id"] for r in rows}:
                raise DesktopError("desktop_display_changed", 409)
            if view == self.view and rev == self.display_revision:
                return {"view_revision": self.view_revision}
            if self.owner: self.native.reset()
            self.view = view
            self.display_revision = rev
            self.view_revision = self._view_id(rev)
            self.frame_cache = None
            self.frame_history.clear()
            self.last_capture = -100.0
            return {"view_revision": self.view_revision}

    def _view_id(self, revision):
        return hashlib.sha256(json.dumps([self.view, revision], sort_keys=True).encode()).hexdigest()[:24]

    def handle(self, operation, args, actor, session_id):
        if not isinstance(actor, str) or not 1 <= len(actor) <= 256:
            raise DesktopError("desktop_actor_invalid", 403)
        identifier(session_id, "session")
        self.expire()
        if operation == "desktop_info":
            fields(args, set())
            rows, rev = self.native.displays()
            return {"displays": rows, "display_revision": rev, "protocol_version": 1}
        if operation == "desktop_view":
            return self._set_view(args, actor, session_id)
        if operation == "desktop_frame":
            fields(args, {"after_frame"})
            return self.frame(str(args.get("after_frame", "")))
        if operation == "desktop_close":
            fields(args, set())
            with self.guard:
                if self.owner and self.owner[:2] == (actor, session_id): self._release()
            return {"closed": True}
        if operation == "desktop_control":
            fields(args, {"mode", "lease_id", "ttl"}, {"mode", "lease_id"})
            lease_id = identifier(args["lease_id"], "lease")
            key = (actor, session_id, lease_id)
            with self.guard:
                if args["mode"] == "release":
                    if self.owner == key: self._release()
                    return {"controlling": False}
                if args["mode"] not in {"acquire", "heartbeat"}:
                    raise DesktopError("desktop_invalid_control")
                self.native.available()
                if self.owner is None:
                    if args["mode"] != "acquire": raise DesktopError("desktop_control_expired", 409)
                    lock = self.lock_factory()
                    try: lock.acquire()
                    except Exception as exc: raise DesktopError("desktop_control_busy", 409) from exc
                    self.device_lock, self.owner = lock, key
                    self.last_seq = 0; self.results.clear()
                if self.owner != key: raise DesktopError("desktop_control_busy", 409)
                ttl = args.get("ttl", CONTROL_TTL)
                if isinstance(ttl, bool) or not isinstance(ttl, (float, int)) or not 0 < ttl <= CONTROL_TTL:
                    raise DesktopError("desktop_invalid_lease")
                self.last_contact = self.clock()
                self.deadline = self.last_contact + ttl
                return {"controlling": True, "lease_id": lease_id, "ttl_ms": round(ttl * 1000)}
        if operation == "desktop_input":
            fields(args, {"lease_id", "event"}, {"lease_id", "event"})
            return self.input(args["event"], actor, session_id, args["lease_id"])
        raise DesktopError("desktop_invalid_operation")

    def frame(self, after):
        if not self.capture_guard.acquire(blocking=False):
            raise DesktopError("desktop_capture_busy", 429)
        try:
            self.native.available()
            with self.guard:
                now = self.clock()
                interval = 2.0 if now-max(self.last_changed, self.last_input) > 10 else .5
                cached = self.frame_cache
                if cached and now-self.last_capture < interval:
                    return self._project(cached, after)
                rows, revision = self.native.displays()
                if self.view is None:
                    self.view = view_options({"display_id": rows[0]["id"]})
                if revision != self.display_revision:
                    if self.owner: self._release()
                    self.display_revision = revision
                    if self.view["display_id"] not in {r["id"] for r in rows}:
                        self.view = view_options({"display_id": rows[0]["id"]})
                    self.view_revision = self._view_id(revision)
                    self.frame_cache = None; self.frame_history.clear()
                view = dict(self.view)
                view_revision = self.view_revision
                display = next(r for r in rows if r["id"] == view["display_id"])
                c = view["crop"]
                x, y = round(c["x"]*display["width"]), round(c["y"]*display["height"])
                rect = {"x": display["x"]+x, "y": display["y"]+y,
                        "width": max(1, min(display["width"]-x, round(c["width"]*display["width"]))),
                        "height": max(1, min(display["height"]-y, round(c["height"]*display["height"])))}
            # Encoding never holds the input lock. One capture, even with many tabs.
            image = self.native.capture(rect, fit_size(rect["width"], rect["height"], view["small"]))
            digest = hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()
            with self.guard:
                cached = self.frame_cache
                changed = not cached or cached["digest"] != digest or cached["meta"]["view_revision"] != view_revision
            data = jpeg_bytes(image) if changed else cached["data"]
            if changed:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as encoded: encoded_size = encoded.size
            else: encoded_size = cached["meta"]["width"], cached["meta"]["height"]
            with self.guard:
                if view_revision != self.view_revision:
                    raise DesktopError("desktop_view_changed", 409)
                self.last_capture = self.clock()
                if changed:
                    self.frames += 1
                    self.last_changed = self.last_capture
                frame_id = f"frame-{view_revision}-{self.frames}"
                meta = {"frame_id": frame_id, "view_revision": view_revision,
                        "display_revision": revision, "display_id": view["display_id"],
                        "width": encoded_size[0], "height": encoded_size[1], "rect": rect,
                        "display": display, "displays": rows, "checked_at": time.time(), "cursor": self.native.cursor()}
                self.frame_history[frame_id] = (self.last_capture, rect, view_revision)
                self.frame_history.move_to_end(frame_id)
                while len(self.frame_history) > 8: self.frame_history.popitem(last=False)
                self.frame_cache = {"meta": meta, "digest": digest, "data": data}
                return self._project(self.frame_cache, after)
        finally:
            self.capture_guard.release()

    def _project(self, cached, after):
        meta = dict(cached["meta"])
        meta["age_ms"] = round(max(0, self.clock()-self.last_capture)*1000)
        meta["cursor"] = self.native.cursor()
        meta["control_active"] = self.owner is not None
        meta["next_poll_ms"] = 2000 if self.clock()-max(self.last_changed, self.last_input) > 10 else 500
        return {"meta": meta, "jpeg": None if after == meta["frame_id"] else base64.b64encode(cached["data"]).decode("ascii")}

    def input(self, value, actor, session_id, lease_id):
        event = validate_input(value)
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        with self.guard:
            self._own(actor, session_id, lease_id)
            seq = event["seq"]
            if seq in self.results:
                old_digest, receipt = self.results[seq]
                if old_digest != digest: raise DesktopError("desktop_input_conflict", 409)
                return receipt
            if seq != self.last_seq+1: raise DesktopError("desktop_input_out_of_order", 409)
            release = event["kind"] in {"reset", "up", "key_up"}
            if not release:
                record = self.frame_history.get(event["frame_id"])
                if not record or event["view_revision"] != self.view_revision or record[2] != self.view_revision or self.clock()-record[0] > 3:
                    raise DesktopError("desktop_stale_frame", 409)
                # Close the display-change race before mapping input coordinates.
                _, revision = self.native.displays()
                if revision != self.display_revision:
                    self._release()
                    raise DesktopError("desktop_display_changed", 409)
                if self.clock()-self.rate_start >= 1: self.rate_start, self.rate_count = self.clock(), 0
                self.rate_count += 1
                if self.rate_count > 60: raise DesktopError("desktop_input_rate", 429)
                if "x" in event:
                    r = record[1]
                    event["px"] = r["x"] + round(event["x"]*(r["width"]-1))
                    event["py"] = r["y"] + round(event["y"]*(r["height"]-1))
            try:
                self.native.inject(event)
            except Exception as exc:
                self._release()
                raise DesktopError("desktop_input_failed", 503) from exc
            self.last_seq = seq
            self.last_contact = self.last_input = self.clock()
            receipt = {"seq": seq, "injected": True}
            self.results[seq] = (digest, receipt)
            while len(self.results) > 64: self.results.popitem(last=False)
            return receipt
