"""Independent monitor previews never mutate the controlled desktop view."""
from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.capability_broker import CapabilityBroker, CapabilityRegistration
from orchestrator.desktop_api import DesktopSessionService
from orchestrator.desktop_contract import ACTIONS, DesktopError
from tools.desktop_session import DesktopController
from tools.device_control_worker import _DeviceLock


class FourDisplays:
    def __init__(self):
        self.rev = "layout-a"
        self.rows = [
            {"id": "left", "x": -1920, "y": 0, "width": 1920, "height": 1080, "primary": False},
            {"id": "main", "x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True},
            {"id": "above", "x": 0, "y": -1200, "width": 1920, "height": 1200, "primary": False},
            {"id": "portrait", "x": 1920, "y": 0, "width": 1080, "height": 1920, "primary": False},
        ]
        self.captures, self.inputs, self.resets = [], [], 0
        self.after_capture = lambda: None
        self.locked = False

    def available(self):
        if self.locked:
            raise DesktopError("desktop_locked", 423)

    def displays(self):
        self.available()
        return self.rows, self.rev

    def cursor(self):
        return {"visible": False}

    def capture(self, rect, size):
        self.captures.append((dict(rect), size))
        image = Image.new("RGB", size, (abs(rect["x"]) % 255, abs(rect["y"]) % 255, 180))
        self.after_capture()
        return image

    def inject(self, event):
        self.inputs.append(dict(event))

    def reset(self):
        self.resets += 1


@pytest.fixture
def desktop(tmp_path):
    now, native = [100.0], FourDisplays()
    controller = DesktopController(native, lambda: _DeviceLock(tmp_path / "input.lock"),
                                   clock=lambda: now[0], watchdog=False)
    controller.handle("desktop_info", {}, "owner-a", "session-a")
    yield controller, native, now
    controller.close()


def previews(controller, ids=None, revision="layout-a"):
    return controller.handle("desktop_previews", {
        "display_ids": ids or ["left", "main", "above", "portrait"],
        "display_revision": revision,
    }, "owner-a", "session-a")


def broker_for(tmp_path, controller, *, optional=True):
    broker = CapabilityBroker(SimpleNamespace(
        global_cfg=SimpleNamespace(instance_id="TEST"),
        paths=SimpleNamespace(bridge_home=tmp_path, instance_id="TEST")))
    actions = ACTIONS | ({"desktop_previews"} if optional else set())
    registration = CapabilityRegistration(
        "cap-a", "computer_control", "TEST", "device-a", "user-a", "windows", "http",
        "http://127.0.0.1:19111", 1, tuple(actions), 123, "generation-a", "key-a",
        time.time(), time.time() + 60)
    broker._records[registration.capability_id] = (registration, "private")
    calls = []

    async def transport(reg, _token, payload, _timeout):
        calls.append(payload["action"])
        result = controller.handle(payload["action"], payload["args"],
                                   payload["actor"]["id"], payload["desktop_session_id"])
        return {"ok": True, "identity": payload["identity"],
                "worker_generation": reg.worker_generation, "result": result}

    broker.set_transport_for_testing(transport)
    return broker, registration, calls


def test_four_previews_are_bounded_memory_only_and_do_not_replace_main_frame(desktop, tmp_path):
    controller, native, _ = desktop
    controller.handle("desktop_view", {"display_id": "main"}, "owner-a", "session-a")
    frame = controller.frame("", "session-a")
    main = controller.sessions["session-a"]
    before = (dict(main.view), main.view_revision, main.frame_cache, list(main.frame_history))
    result = previews(controller)
    assert result["display_revision"] == "layout-a"
    assert 0 < result["ttl_ms"] <= 4000
    assert [item["display_id"] for item in result["previews"]] == ["left", "main", "above", "portrait"]
    for item in result["previews"]:
        data = base64.b64decode(item["jpeg"], validate=True)
        assert len(data) <= 40 * 1024
        with Image.open(io.BytesIO(data)) as image:
            assert image.width <= 320 and image.height <= 180
            assert image.size == (item["width"], item["height"])
        assert "frame_id" not in item and "view_revision" not in item
    assert (main.view, main.view_revision, main.frame_cache, list(main.frame_history)) == before
    assert main.frame_cache["meta"]["frame_id"] == frame["meta"]["frame_id"]
    assert len(native.captures) == 5
    assert not list(tmp_path.iterdir())


def test_preview_rate_is_bounded_and_never_reuses_other_session_content(desktop):
    controller, native, now = desktop
    first = previews(controller, ["left"])
    with pytest.raises(DesktopError, match="preview_busy"):
        previews(controller, ["main"])
    assert len(native.captures) == 1
    now[0] += 2.1
    second = previews(controller, ["main"])
    assert first["previews"][0]["display_id"] != second["previews"][0]["display_id"]
    assert controller.sessions["session-a"].frame_cache is None


@pytest.mark.parametrize("bad", [
    {"display_ids": []}, {"display_ids": ["left"] * 2},
    {"display_ids": ["left"] * 5}, {"display_ids": ["missing"]},
    {"display_revision": "old-layout"}, {"width": 99999},
    {"display_ids": "left"}, {"display_ids": [False]},
])
def test_preview_requests_are_validated_before_capture(desktop, bad):
    controller, native, _ = desktop
    options = {"display_ids": ["left"], "display_revision": "layout-a", **bad}
    with pytest.raises(DesktopError):
        controller.handle("desktop_previews", options, "owner-a", "session-a")
    assert native.captures == []


def test_layout_change_or_session_close_discards_prepared_previews(desktop):
    controller, native, now = desktop
    native.after_capture = lambda: setattr(native, "rev", "layout-b")
    with pytest.raises(DesktopError, match="display_changed"):
        previews(controller, ["left"])
    now[0] += 2.1
    native.rev = "layout-a"
    native.after_capture = lambda: controller.handle("desktop_close", {}, "owner-a", "session-a")
    with pytest.raises(DesktopError, match="session_expired"):
        previews(controller, ["left"])


def test_preview_capture_does_not_hold_input_guard_or_renew_control(desktop):
    controller, native, _ = desktop
    controller.frame("", "session-a")
    controller.handle("desktop_control", {"mode": "acquire", "lease_id": "lease-a"}, "owner-a", "session-a")
    deadline = controller.deadline
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def block_capture():
        entered.set()
        assert release.wait(2)

    native.after_capture = block_capture

    def capture():
        try:
            previews(controller, ["left"])
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        assert entered.wait(1)
        # Input and lease heartbeat must not wait for image encoding.
        started = time.monotonic()
        result = controller.handle("desktop_input", {
            "lease_id": "lease-a", "event": {"seq": 1, "kind": "reset"},
        }, "owner-a", "session-a")
        assert time.monotonic() - started < .25
        assert result["injected"] and native.inputs[-1]["kind"] == "reset"
        assert controller.deadline == deadline
    finally:
        release.set()
        thread.join(3)
    assert finished.is_set() and failures == []


def test_capture_busy_and_locked_never_capture_a_preview(desktop):
    controller, native, _ = desktop
    controller.capture_guard.acquire()
    try:
        with pytest.raises(DesktopError, match="capture_busy"):
            previews(controller, ["left"])
    finally:
        controller.capture_guard.release()
    native.locked = True
    with pytest.raises(DesktopError, match="locked"):
        previews(controller, ["left"])
    assert native.captures == []


async def test_preview_service_keeps_owner_client_and_worker_generation_fences(desktop, tmp_path):
    controller, native, _ = desktop
    broker, registration, calls = broker_for(tmp_path, controller)
    service = DesktopSessionService(lambda: broker)
    target = (await service.run("owner-a", {"operation": "targets", "client_id": "client-a"}))["targets"][0]
    opened = await service.run("owner-a", {"operation": "open", "client_id": "client-a", "target": target})
    request = {"operation": "previews", "client_id": "client-a", "session_id": opened["session_id"],
               "options": {"display_ids": ["left"], "display_revision": "layout-a"}}
    for owner, client in [("owner-b", "client-a"), ("owner-a", "other-tab")]:
        with pytest.raises(DesktopError, match="forbidden"):
            await service.run(owner, {**request, "client_id": client})
    assert native.captures == []
    result = await service.run("owner-a", request)
    assert result["previews"][0]["display_id"] == "left"
    assert calls == ["desktop_info", "desktop_previews"]
    broker._records[registration.capability_id] = (replace(registration, worker_generation="new-generation"), "private")
    with pytest.raises(DesktopError, match="target_changed"):
        await service.run("owner-a", request)
    assert len(native.captures) == 1


async def test_older_worker_stays_discoverable_but_does_not_receive_optional_preview(desktop, tmp_path):
    controller, native, _ = desktop
    broker, _, calls = broker_for(tmp_path, controller, optional=False)
    targets = broker.desktop_targets()
    assert len(targets) == 1
    with pytest.raises(DesktopError, match="preview_unsupported"):
        await broker.invoke_desktop(targets[0], actor_id="owner-a", session_id="session-a",
                                    operation="desktop_previews",
                                    args={"display_ids": ["left"], "display_revision": "layout-a"})
    assert calls == [] and native.captures == []

def test_queued_preview_cannot_recreate_a_closed_worker_session(desktop):
    controller, native, _ = desktop
    controller.handle("desktop_info", {}, "owner-a", "session-a")
    controller.handle("desktop_close", {}, "owner-a", "session-a")
    with pytest.raises(DesktopError, match="session_expired"):
        previews(controller, ["left"])
    assert native.captures == [] and "session-a" not in controller.sessions


async def test_worker_replacement_during_capture_discards_the_old_preview(desktop, tmp_path):
    controller, native, _ = desktop
    broker, registration, _ = broker_for(tmp_path, controller)
    pin = broker.desktop_targets()[0]
    await broker.invoke_desktop(pin, actor_id="owner-a", session_id="session-a", operation="desktop_info", args={})
    native.after_capture = lambda: broker._records.update({
        registration.capability_id: (replace(registration, worker_generation="replacement"), "private")
    })
    with pytest.raises(DesktopError, match="target_changed"):
        await broker.invoke_desktop(pin, actor_id="owner-a", session_id="session-a", operation="desktop_previews",
                                    args={"display_ids": ["left"], "display_revision": "layout-a"})


async def test_api_close_discards_a_preview_that_was_already_in_transport():
    started, finish = asyncio.Event(), asyncio.Event()
    class Broker:
        def desktop_targets(self):
            return [{"capability_id": "fixture"}]
        async def invoke_desktop(self, _pin, *, operation, **_kwargs):
            if operation == "desktop_previews":
                started.set()
                await finish.wait()
                return {"previews": [{"display_id": "left"}]}
            return {"displays": []}
    service = DesktopSessionService(Broker)
    opened = await service.run("owner", {"operation": "open", "client_id": "client",
                                        "target": {"capability_id": "fixture"}})
    base = {"client_id": "client", "session_id": opened["session_id"]}
    pending = asyncio.create_task(service.run("owner", {**base, "operation": "previews",
        "options": {"display_ids": ["left"], "display_revision": "layout-a"}}))
    await asyncio.wait_for(started.wait(), 1)
    await service.run("owner", {**base, "operation": "close"})
    finish.set()
    with pytest.raises(DesktopError, match="session_expired"):
        await asyncio.wait_for(pending, 1)

def test_view_sequence_prevents_a_late_request_from_reversing_the_final_target(desktop):
    controller, native, _ = desktop
    info = controller.handle("desktop_info", {}, "owner-a", "session-a")
    assert info["view_sequence_supported"] is True
    latest = controller.handle("desktop_view", {"display_id": "left", "sequence": 2}, "owner-a", "session-a")
    with pytest.raises(DesktopError, match="view_superseded"):
        controller.handle("desktop_view", {"display_id": "main", "sequence": 1}, "owner-a", "session-a")
    # The duplicate final request is harmless, but reusing its sequence for a
    # different target cannot silently overwrite the view.
    assert controller.handle("desktop_view", {"display_id": "left", "sequence": 2}, "owner-a", "session-a") == latest
    with pytest.raises(DesktopError, match="view_conflict"):
        controller.handle("desktop_view", {"display_id": "main", "sequence": 2}, "owner-a", "session-a")
    frame = controller.frame("", "session-a")["meta"]
    assert frame["display_ids"] == ["left"]
    controller.handle("desktop_control", {"mode": "acquire", "lease_id": "lease-a"}, "owner-a", "session-a")
    controller.handle("desktop_input", {"lease_id": "lease-a", "event": {
        "seq": 1, "kind": "down", "button": "left", "x": .5, "y": .5,
        "frame_id": frame["frame_id"], "view_revision": frame["view_revision"],
    }}, "owner-a", "session-a")
    assert native.inputs[-1]["px"] < 0


@pytest.mark.parametrize("sequence", [True, 0, -1, 2 ** 53, "1"])
def test_invalid_view_sequences_do_not_change_the_selected_display(desktop, sequence):
    controller, _, _ = desktop
    controller.handle("desktop_view", {"display_id": "main"}, "owner-a", "session-a")
    before = dict(controller.sessions["session-a"].view)
    with pytest.raises(DesktopError):
        controller.handle("desktop_view", {"display_id": "left", "sequence": sequence}, "owner-a", "session-a")
    assert controller.sessions["session-a"].view == before

@pytest.mark.parametrize("mid_capture", [False, True])
def test_independent_previews_clear_on_locked_secure_desktop_without_disabling_the_main_view(desktop, mid_capture):
    controller, native, _ = desktop
    state = {"locked": not mid_capture, "secure_desktop": False}
    native.desktop_state = lambda: dict(state)
    if mid_capture:
        native.after_capture = lambda: state.update(locked=True)
    with pytest.raises(DesktopError, match="locked"):
        previews(controller, ["left"])
    assert len(native.captures) == int(mid_capture)
    # The explicit secure-desktop/manual view remains an existing independent
    # capability; only the extra overview preview is paused.
    native.after_capture = lambda: None
    controller.frame("", "session-a")
    assert controller.sessions["session-a"].frame_cache is not None
