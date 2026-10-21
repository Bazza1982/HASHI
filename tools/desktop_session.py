"""Manual desktop endpoint inside the existing device worker.

Each session owns its bounded preview. An OS lock is held for the whole human
control lease, including gaps between input calls. All legacy agent mutations
use the same OS lock. Native dependencies are imported only in the Windows worker.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import threading
import time
from collections import OrderedDict

from orchestrator.desktop_contract import CONTROL_TTL, SESSION_TTL, MAX_FRAME_BYTES, ULTRA_BYTES_PER_SECOND, ULTRA_FRAME_BYTES, DesktopError, fields, frame_interval_seconds, identifier, validate_input, view_options
from tools.windows_helper.desktop_capture import fit_size, jpeg_bytes


class SessionState:
    def __init__(self, last_contact):
        self.last_contact = last_contact
        self.view = None
        self.view_revision = ""
        self.display_revision = ""
        self.frame_cache = None
        self.frame_history = OrderedDict()
        self.last_capture = -100.0
        self.last_changed = -100.0
        self.frames = 0
        self.last_profile = None
        self.last_frame_bytes = 0
        self.last_work_seconds = 0.0


class DesktopController:
    def __init__(self, native, lock_factory, *, clock=time.monotonic, watchdog=True):
        self.native, self.lock_factory, self.clock = native, lock_factory, clock
        self.guard = threading.RLock()
        self.capture_guard = threading.Lock()
        self.owner = None
        self.device_lock = None
        self.deadline = 0.0
        self.last_contact = 0.0
        self.sessions = {}
        self.last_input = self.clock()
        self.last_seq = 0
        self.results = OrderedDict()
        self.rate_start, self.rate_count = self.clock(), 0
        self.stop_event = threading.Event()
        if watchdog:
            threading.Thread(target=self._watch, name="desktop-input-watchdog", daemon=True).start()

    def _get_session(self, session_id):
        sid = identifier(session_id, "session")
        now = self.clock()
        if sid not in self.sessions:
            self.sessions[sid] = SessionState(now)
        session = self.sessions[sid]
        session.last_contact = now
        return session

    def _watch(self):
        while not self.stop_event.wait(.5):
            self.expire()

    def expire(self):
        with self.guard:
            now = self.clock()
            if self.owner and now >= self.deadline:
                self._release()
            elif self.owner and now - self.last_contact > 3:
                try: self.native.reset()
                except Exception: pass
            for sid, session in list(self.sessions.items()):
                if now - session.last_contact >= SESSION_TTL:
                    if self.owner and self.owner[1] == sid:
                        self._release()
                    self.sessions.pop(sid, None)

    def close(self):
        self.stop_event.set()
        with self.guard:
            self._release()
            self.sessions.clear()

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
            view = view_options(value)
            rows, _ = self.native.displays()
            if not set(self._view_display_ids(view)).issubset({r["id"] for r in rows}):
                raise DesktopError("desktop_display_changed", 409)
            rev = self._display_revision(rows, view)
            sess = self._get_session(session_id)
            if view == sess.view and rev == sess.display_revision:
                return {"view_revision": sess.view_revision}
            if self.owner and self.owner[:2] == (actor, session_id):
                self.native.reset()
            sess.view = view
            sess.display_revision = rev
            sess.view_revision = self._view_id(session_id, view, rev)
            sess.frame_cache = None
            sess.frame_history.clear()
            sess.last_capture = -100.0
            return {"view_revision": sess.view_revision}

    def _view_id(self, session_id, view, revision):
        return hashlib.sha256(json.dumps([session_id, view, revision], sort_keys=True).encode()).hexdigest()[:24]

    @staticmethod
    def _view_display_ids(view):
        return list(view.get("display_ids") or [view["display_id"]])

    @classmethod
    def _selected_displays(cls, rows, view):
        by_id = {row["id"]: row for row in rows}
        return [by_id[display_id] for display_id in cls._view_display_ids(view) if display_id in by_id]

    @classmethod
    def _display_revision(cls, rows, view):
        # Input coordinates depend only on selected monitors. An unselected
        # monitor waking or sleeping must not revoke the current control lease.
        selected = cls._selected_displays(rows, view)
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()[:24]

    @classmethod
    def _surviving_view(cls, view, rows):
        available = {row["id"] for row in rows}
        selected = [display_id for display_id in cls._view_display_ids(view) if display_id in available]
        if not selected:
            selected = [rows[0]["id"]]
        value = {
            "small": view["small"],
            "crop": view["crop"],
            **({"display_id": selected[0]} if len(selected) == 1 else {"display_ids": selected}),
        }
        return view_options(value)

    @staticmethod
    def _view_geometry(selected, crop):
        left = min(display["x"] for display in selected)
        top = min(display["y"] for display in selected)
        right = max(display["x"] + display["width"] for display in selected)
        bottom = max(display["y"] + display["height"] for display in selected)
        width, height = right - left, bottom - top
        x, y = round(crop["x"] * width), round(crop["y"] * height)
        rect = {
            "x": left + x,
            "y": top + y,
            "width": max(1, min(width - x, round(crop["width"] * width))),
            "height": max(1, min(height - y, round(crop["height"] * height))),
        }
        regions = []
        for display in selected:
            region_left = max(rect["x"], display["x"])
            region_top = max(rect["y"], display["y"])
            region_right = min(rect["x"] + rect["width"], display["x"] + display["width"])
            region_bottom = min(rect["y"] + rect["height"], display["y"] + display["height"])
            if region_right > region_left and region_bottom > region_top:
                regions.append({"id": display["id"], "x": region_left, "y": region_top,
                                "width": region_right - region_left, "height": region_bottom - region_top})
        return rect, regions

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
            fields(args, {"after_frame", "refresh_profile"})
            return self.frame(str(args.get("after_frame", "")), session_id, args.get("refresh_profile", "standard"))
        if operation == "desktop_close":
            fields(args, set())
            with self.guard:
                if self.owner and self.owner[:2] == (actor, session_id): self._release()
                self.sessions.pop(session_id or "", None)
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
                self._get_session(session_id)
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

    def frame(self, after, session_id, refresh_profile="standard"):
        frame_interval_seconds(refresh_profile, idle=False)
        if not self.capture_guard.acquire(blocking=False):
            raise DesktopError("desktop_capture_busy", 429)
        try:
            self.native.available()
            with self.guard:
                now = self.clock()
                sess = self._get_session(session_id)
                interval = self._frame_interval(sess, refresh_profile, now)
                cached = sess.frame_cache
                size_profile_changed = (sess.last_profile == "ultra_smooth") != (refresh_profile == "ultra_smooth")
                if cached and not size_profile_changed and now-sess.last_capture < interval:
                    if refresh_profile == "ultra_smooth" and after != cached["meta"]["frame_id"]:
                        raise DesktopError("desktop_capture_busy", 429)
                    return self._project(cached, after, sess, refresh_profile)
                rows, revision = self.native.displays()
                if sess.view is None:
                    sess.view = view_options({"display_id": rows[0]["id"]})
                selected_revision = self._display_revision(rows, sess.view)
                if selected_revision != sess.display_revision:
                    if self.owner and self.owner[1] == (session_id or ""):
                        self._release()
                    sess.view = self._surviving_view(sess.view, rows)
                    sess.display_revision = self._display_revision(rows, sess.view)
                    sess.view_revision = self._view_id(session_id, sess.view, sess.display_revision)
                    sess.frame_cache = None; sess.frame_history.clear()
                view = dict(sess.view)
                view_revision = sess.view_revision
                selected = self._selected_displays(rows, view)
                display_ids = self._view_display_ids(view)
                rect, display_regions = self._view_geometry(selected, view["crop"])
            # Encoding never holds the input lock. Sessions have independent views.
            capture_at = self.clock()
            image = self.native.capture(rect, fit_size(rect["width"], rect["height"], view["small"] or refresh_profile == "ultra_smooth"))
            if len(selected) > 1:
                image = self._mask_unselected(image, rect, display_regions)
            digest = hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()
            with self.guard:
                cached = sess.frame_cache
                changed = not cached or cached["digest"] != digest or cached["meta"]["view_revision"] != view_revision or (cached["profile"] == "ultra_smooth") != (refresh_profile == "ultra_smooth")
            data = jpeg_bytes(image, max_bytes=ULTRA_FRAME_BYTES if refresh_profile == "ultra_smooth" else MAX_FRAME_BYTES) if changed else cached["data"]
            if changed:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as encoded: encoded_size = encoded.size
            else: encoded_size = cached["meta"]["width"], cached["meta"]["height"]
            work_seconds = max(0.0, self.clock() - capture_at)
            with self.guard:
                if self.sessions.get(session_id) is not sess:
                    raise DesktopError("desktop_session_expired", 410)
                if view_revision != sess.view_revision:
                    raise DesktopError("desktop_view_changed", 409)
                sess.last_capture = capture_at
                sess.last_profile = refresh_profile
                sess.last_work_seconds = work_seconds
                if changed:
                    sess.frames += 1
                    sess.last_changed = capture_at
                frame_id = f"frame-{view_revision}-{sess.frames}"
                sess.last_frame_bytes = len(data) if after != frame_id else 0
                meta = {"frame_id": frame_id, "view_revision": view_revision,
                        "display_revision": revision, "display_ids": display_ids,
                        "width": encoded_size[0], "height": encoded_size[1], "rect": rect,
                        "selected_displays": selected, "display_regions": display_regions,
                        "displays": rows, "checked_at": time.time(), "cursor": self.native.cursor()}
                if len(selected) == 1:
                    meta["display_id"] = display_ids[0]
                    meta["display"] = selected[0]
                if callable(getattr(self.native, 'desktop_state', None)):
                    status = self.native.desktop_state()
                    meta['windows_locked'] = bool(status.get('locked'))
                    meta['secure_desktop'] = bool(status.get('secure_desktop'))
                sess.frame_history[frame_id] = (sess.last_capture, rect, view_revision, display_regions)
                sess.frame_history.move_to_end(frame_id)
                while len(sess.frame_history) > 8: sess.frame_history.popitem(last=False)
                sess.frame_cache = {"meta": meta, "digest": digest, "data": data, "profile": refresh_profile}
                return self._project(sess.frame_cache, after, sess, refresh_profile)
        finally:
            self.capture_guard.release()

    @staticmethod
    def _mask_unselected(image, rect, display_regions):
        """Keep physical layout while blanking gaps and unselected monitors."""
        from PIL import Image
        masked = Image.new("RGB", image.size, (8, 13, 18))
        scale_x, scale_y = image.width / rect["width"], image.height / rect["height"]
        for region in display_regions:
            left = max(0, math.floor((region["x"] - rect["x"]) * scale_x))
            top = max(0, math.floor((region["y"] - rect["y"]) * scale_y))
            right = min(image.width, math.ceil((region["x"] + region["width"] - rect["x"]) * scale_x))
            bottom = min(image.height, math.ceil((region["y"] + region["height"] - rect["y"]) * scale_y))
            if right > left and bottom > top:
                masked.paste(image.crop((left, top, right, bottom)), (left, top))
        return masked

    def _project(self, cached, after, sess, refresh_profile):
        meta = dict(cached["meta"])
        now = self.clock()
        elapsed = max(0.0, now - sess.last_capture)
        meta["age_ms"] = round(elapsed * 1000)
        meta["cursor"] = self.native.cursor()
        meta["control_active"] = self.owner is not None
        interval = self._frame_interval(sess, refresh_profile, now)
        if refresh_profile == "ultra_smooth":
            # The interval is measured between capture starts. The completed
            # capture has already used part of that budget.
            interval = max(0.0, interval - elapsed)
        meta["next_poll_ms"] = round(interval * 1000)
        return {"meta": meta, "jpeg": None if after == meta["frame_id"] else base64.b64encode(cached["data"]).decode("ascii")}

    def _frame_interval(self, sess, profile, now):
        inactive = now - max(sess.last_changed, self.last_input)
        if profile == "ultra_smooth":
            if inactive > 2:
                return 0.5
            if inactive > 0.5:
                return 0.1
            return max(frame_interval_seconds(profile, idle=False), sess.last_frame_bytes / ULTRA_BYTES_PER_SECOND, sess.last_work_seconds * 2)
        return frame_interval_seconds(profile, idle=inactive > 10)

    def input(self, value, actor, session_id, lease_id):
        input_received = self.clock()
        event = validate_input(value)
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        with self.guard:
            self._own(actor, session_id, lease_id)
            sess = self._get_session(session_id)
            seq = event["seq"]
            if seq in self.results:
                old_digest, receipt = self.results[seq]
                if old_digest != digest: raise DesktopError("desktop_input_conflict", 409)
                return receipt
            if seq != self.last_seq+1: raise DesktopError("desktop_input_out_of_order", 409)
            release = event["kind"] in {"reset", "up", "key_up"}
            ignored = False
            if not release:
                record = sess.frame_history.get(event["frame_id"])
                if not record or event["view_revision"] != sess.view_revision or record[2] != sess.view_revision or self.clock()-record[0] > 3:
                    error = DesktopError("desktop_stale_frame", 409)
                    error.desktop_diagnostics = {
                        "frame_age_ms": round((self.clock()-record[0])*1000) if record else -1,
                        "input_wait_ms": round((self.clock()-input_received)*1000),
                    }
                    raise error
                # Close the display-change race before mapping input coordinates.
                rows, _ = self.native.displays()
                if self._display_revision(rows, sess.view) != sess.display_revision:
                    self._release()
                    raise DesktopError("desktop_display_changed", 409)
                if self.clock()-self.rate_start >= 1: self.rate_start, self.rate_count = self.clock(), 0
                self.rate_count += 1
                if self.rate_count > 60: raise DesktopError("desktop_input_rate", 429)
                if "x" in event:
                    r = record[1]
                    event["px"] = r["x"] + round(event["x"]*(r["width"]-1))
                    event["py"] = r["y"] + round(event["y"]*(r["height"]-1))
                    regions = record[3] if len(record) > 3 else [r]
                    ignored = not any(region["x"] <= event["px"] < region["x"] + region["width"]
                                      and region["y"] <= event["py"] < region["y"] + region["height"]
                                      for region in regions)
            if not ignored:
                try:
                    self.native.inject(event)
                except DesktopError:
                    self._release()
                    raise
                except Exception as exc:
                    self._release()
                    raise DesktopError("desktop_input_failed", 503) from exc
            self.last_seq = seq
            self.last_contact = self.last_input = self.clock()
            receipt = {"seq": seq, "injected": not ignored, **({"ignored": True} if ignored else {})}
            self.results[seq] = (digest, receipt)
            while len(self.results) > 64: self.results.popitem(last=False)
            return receipt
