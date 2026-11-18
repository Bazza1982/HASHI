from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import browser
from tools.schemas import TOOL_SCHEMA_MAP


ROOT = Path(__file__).resolve().parents[1]


def test_normalize_extension_bridge_screenshot_data_url() -> None:
    result = browser._normalize_extension_bridge_output(
        "screenshot",
        "data:image/png;base64,aGVsbG8=",
    )
    assert result == "screenshot:aGVsbG8="


def test_normalize_extension_bridge_leaves_non_screenshot_output() -> None:
    result = browser._normalize_extension_bridge_output(
        "get_text",
        "data:image/png;base64,aGVsbG8=",
    )
    assert result == "data:image/png;base64,aGVsbG8="


@pytest.mark.asyncio
async def test_execute_browser_screenshot_normalizes_extension_data_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "screenshot"
        return "screenshot:aGVsbG8="

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_screenshot(
        {
            "url": "https://example.com",
            "bridge_backend": "extension",
        }
    )
    assert result == "screenshot:aGVsbG8="


@pytest.mark.asyncio
async def test_execute_browser_click_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "click"
        assert args["selector"] == "button.search"
        return "OK: clicked 'button.search'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_click(
        {
            "url": "https://example.com",
            "selector": "button.search",
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: clicked 'button.search'"


@pytest.mark.asyncio
async def test_execute_browser_react_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "react"
        assert args["post_text"] == "City2Surf 2026"
        assert args["author"] == "Jordan Xu"
        return json.dumps({"ok": True, "action": "react", "verified": True})

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_react(
        {
            "url": "https://www.linkedin.com/feed/",
            "post_text": "City2Surf 2026",
            "author": "Jordan Xu",
            "reaction": "like",
        }
    )

    assert json.loads(result)["verified"] is True


@pytest.mark.asyncio
async def test_execute_browser_fill_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "fill"
        assert args["selector"] == "input[name='q']"
        assert args["text"] == "hashi browser bridge"
        return "OK: filled 'input[name=\\'q\\']'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_fill(
        {
            "url": "https://scholar.google.com",
            "selector": "input[name='q']",
            "text": "hashi browser bridge",
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: filled 'input[name=\\'q\\']'"


@pytest.mark.asyncio
async def test_execute_browser_hover_passes_timing_and_position_to_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "hover"
        assert args["selector"] == "button.reaction"
        assert args["timeout_ms"] == 8000
        assert args["wait_ms"] == 650
        assert args["x_ratio"] == 0.4
        assert args["y_ratio"] == 0.6
        return "OK: hovered 'button.reaction'"

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_hover(
        {
            "url": "https://www.linkedin.com/feed/",
            "selector": "button.reaction",
            "timeout_ms": 8000,
            "wait_ms": 650,
            "x_ratio": 0.4,
            "y_ratio": 0.6,
            "bridge_backend": "extension",
        }
    )
    assert result == "OK: hovered 'button.reaction'"


@pytest.mark.asyncio
async def test_execute_browser_evaluate_prefers_extension_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_bridge(action: str, args: dict) -> str | None:
        assert action == "evaluate"
        assert args["script"] == "() => document.title"
        return "\"Example Domain\""

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)
    result = await browser.execute_browser_evaluate(
        {
            "url": "https://example.com",
            "script": "() => document.title",
            "bridge_backend": "extension",
        }
    )
    assert result == "\"Example Domain\""


@pytest.mark.asyncio
async def test_structured_media_tools_use_extension_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    async def fake_bridge(action: str, args: dict) -> str | None:
        observed.append(action)
        assert args["bridge_backend"] == "extension"
        if action == "active_tab":
            return json.dumps(
                {
                    "tabId": 7,
                    "windowId": 9,
                    "url": "https://www.youtube.com/watch?v=abc",
                    "title": "Example video - YouTube",
                    "maximized": True,
                }
            )
        if action == "media_state":
            return json.dumps({"ok": True, "media_found": True, "state": {"paused": True}})
        return json.dumps({"ok": True, "verified": True, "time_delta": 1.25})

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", fake_bridge)

    active = json.loads(await browser.execute_browser_active_tab({}))
    state = json.loads(await browser.execute_browser_get_media_state({}))
    played = json.loads(await browser.execute_browser_play({}))

    assert active["maximized"] is True
    assert state["state"]["paused"] is True
    assert played["verified"] is True
    assert observed == ["active_tab", "media_state", "media_play"]


@pytest.mark.asyncio
async def test_open_play_verify_requires_every_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.youtube.com/watch?v=abc123"
    active_calls = 0

    async def fake_wait(_timeout_s: float) -> dict:
        return {
            "connected": True,
            "response": {"extension_meta": {"extension_version": "0.2.0"}},
        }

    async def fake_active(args: dict) -> str:
        nonlocal active_calls
        active_calls += 1
        return json.dumps(
            {
                "tabId": 7,
                "windowId": 9,
                "url": url,
                "title": "系统送我一只鸡，我卷翻了修仙界 - YouTube",
                "windowState": "maximized",
                "maximized": True,
            }
        )

    async def fake_play(args: dict) -> str:
        assert args["url"] == url
        return json.dumps(
            {
                "ok": True,
                "verified": True,
                "time_advanced": True,
                "time_delta": 1.5,
                "after": {"paused": False, "current_time": 8.5},
            }
        )

    async def fake_focus(args: dict) -> str:
        assert args == {
            "title_contains": "YouTube",
            "maximize": True,
            "_skip_helper": True,
        }
        return "Focused and maximized window id=11 title=YouTube - Google Chrome"

    monkeypatch.setattr(browser, "_wait_for_extension_connection", fake_wait)
    monkeypatch.setattr(browser, "execute_browser_active_tab", fake_active)
    monkeypatch.setattr(browser, "execute_browser_play", fake_play)
    monkeypatch.setattr(browser.platform, "system", lambda: "Windows")
    monkeypatch.setattr("tools.windows_use.execute_windows_window_focus", fake_focus)

    result = json.loads(
        await browser.execute_browser_open_play_verify(
            {"url": url, "expected_title": "系统送我一只鸡，我卷翻了修仙界"}
        )
    )

    assert result["ok"] is True
    assert result["maximized"] is True
    assert result["playback_verified"] is True
    assert active_calls == 2
    assert [stage["stage"] for stage in result["stages"]] == [
        "bridge_connect",
        "open_and_maximize",
        "play_and_verify",
        "final_identity_check",
    ]


@pytest.mark.asyncio
async def test_open_play_verify_never_promotes_a_click_to_playback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.youtube.com/watch?v=abc123"

    async def fake_wait(_timeout_s: float) -> dict:
        return {"connected": True, "response": {"extension_meta": {"extension_version": "0.2.0"}}}

    async def fake_active(_args: dict) -> str:
        return json.dumps(
            {
                "url": url,
                "title": "Example video - YouTube",
                "maximized": True,
            }
        )

    async def fake_play(_args: dict) -> str:
        return json.dumps(
            {
                "ok": False,
                "verified": False,
                "time_advanced": False,
                "after": {"paused": True, "current_time": 2.0},
                "error": "media playback did not satisfy the advancing-time postcondition",
            }
        )

    async def fake_focus(_args: dict) -> str:
        return "Focused and maximized window id=11 title=YouTube - Google Chrome"

    monkeypatch.setattr(browser, "_wait_for_extension_connection", fake_wait)
    monkeypatch.setattr(browser, "execute_browser_active_tab", fake_active)
    monkeypatch.setattr(browser, "execute_browser_play", fake_play)
    monkeypatch.setattr(browser.platform, "system", lambda: "Windows")
    monkeypatch.setattr("tools.windows_use.execute_windows_window_focus", fake_focus)

    result = json.loads(
        await browser.execute_browser_open_play_verify(
            {"url": url, "expected_title": "Example video"}
        )
    )

    assert result["ok"] is False
    assert result["failed_stage"] == "play_and_verify"
    assert result["playback_verified"] is False
    assert "advancing-time postcondition" in result["error"]


def test_media_tools_are_model_visible_with_strict_high_level_contract() -> None:
    for name in (
        "browser_active_tab",
        "browser_get_media_state",
        "browser_play",
        "browser_open_play_verify",
    ):
        assert name in TOOL_SCHEMA_MAP
    high_level = TOOL_SCHEMA_MAP["browser_open_play_verify"]["function"]["parameters"]
    assert high_level["required"] == ["url", "expected_title"]
    assert high_level["additionalProperties"] is False


@pytest.mark.asyncio
async def test_registry_projects_runtime_audit_before_browser_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from orchestrator.config import GlobalConfig
    from tools.registry import ToolRegistry

    captured: dict = {}

    async def fake_get_text(args: dict) -> str:
        captured.update(args)
        return "Example Domain"

    monkeypatch.setattr(browser, "execute_browser_get_text", fake_get_text)
    registry = ToolRegistry(
        allowed_tools=["browser_get_text"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "agent_name": "momo",
            "workspace_dir": tmp_path,
            "safety_mode": "read_only",
            "global_config": GlobalConfig(authorized_id=123),
            "_runtime": object(),
        },
    )

    result = await registry.execute(
        "browser_get_text", {"url": "https://example.com"}, "call-7"
    )

    assert result.is_error is False
    assert result.output == "Example Domain"
    assert captured["_audit"] == {
        "agent_name": "momo",
        "workspace_dir": str(tmp_path),
        "safety_mode": "read_only",
    }


@pytest.mark.asyncio
async def test_registry_injects_configured_browser_transport_for_isolated_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tools.registry import ToolRegistry

    captured: dict = {}

    async def fake_active_tab(args: dict) -> str:
        captured.update(args)
        return "active"

    monkeypatch.setattr(browser, "execute_browser_active_tab", fake_active_tab)
    registry = ToolRegistry(
        allowed_tools=["browser_active_tab"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        tool_options={
            "browser": {
                "bridge_endpoint": r"\\.\pipe\shared-browser-bridge",
                "bridge_auth_file": str(tmp_path / "bridge-auth.key"),
            }
        },
    )

    result = await registry.execute("browser_active_tab", {}, "call-transport")

    assert result.is_error is False
    assert result.output == "active"
    assert captured["_bridge_endpoint"] == r"\\.\pipe\shared-browser-bridge"
    assert captured["_bridge_auth_file"] == str(tmp_path / "bridge-auth.key")


@pytest.mark.asyncio
async def test_extension_bridge_uses_private_configured_transport_without_forwarding_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tools import browser_extension_bridge

    endpoint = r"\\.\pipe\shared-browser-bridge"
    auth_file = tmp_path / "bridge-auth.key"
    observed: dict = {}

    def fake_available(**kwargs) -> bool:
        observed["available"] = kwargs
        return True

    def fake_session(**kwargs):
        observed["session"] = kwargs
        return {
            "session": {"session_id": "default::momo"},
            "extension_meta": {"extension_version": "0.2.2", "actions": ["active_tab"]},
        }

    def fake_command(action, args, **kwargs):
        observed["command"] = (action, args, kwargs)
        return {"ok": True, "output": "connected"}

    monkeypatch.setattr(browser_extension_bridge, "bridge_available", fake_available)
    monkeypatch.setattr(browser_extension_bridge, "ensure_bridge_session", fake_session)
    monkeypatch.setattr(browser_extension_bridge, "send_bridge_command", fake_command)

    result = await browser._maybe_execute_extension_bridge(
        "active_tab",
        {
            "agent_name": "momo",
            "_bridge_endpoint": endpoint,
            "_bridge_auth_file": str(auth_file),
        },
    )

    expected_transport = {"socket_path": endpoint, "auth_file": auth_file}
    assert result == "connected"
    assert observed["available"] == expected_transport
    assert observed["session"]["socket_path"] == endpoint
    assert observed["session"]["auth_file"] == auth_file
    action, command_args, command_transport = observed["command"]
    assert action == "active_tab"
    assert command_transport == expected_transport
    assert "_bridge_endpoint" not in command_args
    assert "_bridge_auth_file" not in command_args


@pytest.mark.asyncio
async def test_extension_contract_rejects_advanced_action_not_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import browser_extension_bridge

    monkeypatch.setattr(browser_extension_bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        browser_extension_bridge,
        "ensure_bridge_session",
        lambda **_kwargs: {
            "session": {"session_id": "default::momo"},
            "extension_meta": {"extension_version": "0.1.2"},
        },
    )

    result = await browser._maybe_execute_extension_bridge(
        "session",
        {"url": "https://example.com", "steps": [{"action": "get_text"}]},
    )

    assert result is not None
    assert "does not advertise the 'session' contract" in result


@pytest.mark.asyncio
async def test_extension_contract_allows_advertised_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import browser_extension_bridge

    monkeypatch.setattr(browser_extension_bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        browser_extension_bridge,
        "ensure_bridge_session",
        lambda **_kwargs: {
            "session": {"session_id": "default::momo"},
            "extension_meta": {
                "extension_version": "0.1.4",
                "actions": ["session_create", "session"],
            },
        },
    )
    monkeypatch.setattr(
        browser_extension_bridge,
        "send_bridge_command",
        lambda action, args: {"ok": True, "output": f"executed:{action}:{args['session_id']}"},
    )

    result = await browser._maybe_execute_extension_bridge(
        "session",
        {"url": "https://example.com", "steps": [{"action": "get_text"}]},
    )

    assert result == "executed:session:default::momo"
def test_extension_source_routes_session_and_scroll_to_real_implementations() -> None:
    source = (
        ROOT / "tools" / "chrome_extension" / "hashi_browser_bridge" / "service_worker.js"
    ).read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "tools" / "chrome_extension" / "hashi_browser_bridge" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert 'const BRIDGE_VERSION = "0.2.0";' in source
    assert manifest["version"] == "0.2.0"
    assert 'if (action === "session") {\n    return actionSession(args);' in source
    assert 'if (action === "scroll") {\n    return actionScroll(args);' in source
    assert 'if (action === "react") {\n    return actionReact(args);' in source
    assert '"click", "react", "hover"' in source
    assert "scrollable[0]" in source
    assert "reaction click did not produce a verified state change" in source
    assert 'action === "active_tab" || action === "session_create" || action === "session"' not in source
    assert "actions: SUPPORTED_ACTIONS" in source
    assert '"media_state", "media_play"' in source
    assert 'if (action === "media_state") {\n    return actionMediaState(args);' in source
    assert 'if (action === "media_play") {\n    return actionMediaPlay(args);' in source
    assert "media playback did not satisfy the advancing-time postcondition" in source
    assert 'state: "maximized"' in source


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "html", "evaluate"])
async def test_standalone_browser_uses_selected_timeout_and_returns_full_result(monkeypatch, kind):
    payload = "visible-" * 5000 + "-complete-tail"
    calls = []

    class Page:
        async def goto(self, url, *, wait_until, timeout):
            calls.append((url, wait_until, timeout))
            if wait_until == "networkidle":
                raise TimeoutError("continuous traffic never became idle")

        async def evaluate(self, _script):
            return payload

        async def content(self):
            return payload

    class Closed:
        async def close(self):
            pass

        async def stop(self):
            pass

    async def no_bridge(_action, _args):
        return None

    async def get_page(**_kwargs):
        return Closed(), Closed(), object(), Page()

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", no_bridge)
    monkeypatch.setattr(browser, "_get_page", get_page)
    args = {"url": "https://example.invalid", "wait_ms": 0, "timeout_ms": 90_000}
    if kind == "evaluate":
        args["script"] = "() => document.body.innerText"
    result = await {
        "text": browser.execute_browser_get_text,
        "html": browser.execute_browser_get_html,
        "evaluate": browser.execute_browser_evaluate,
    }[kind](args)
    assert calls == [("https://example.invalid", "domcontentloaded", 90_000)]
    assert result == payload


@pytest.mark.asyncio
async def test_standalone_browser_selected_readiness_timeout_and_cancel(monkeypatch):
    import asyncio
    calls = []
    closed = []
    blocked = asyncio.Event()

    class Page:
        async def goto(self, url, *, wait_until, timeout):
            calls.append((wait_until, timeout))
            if timeout == 1:
                raise TimeoutError("selected deadline")
            if timeout == 99_000:
                blocked.set()
                await asyncio.Event().wait()

        async def evaluate(self, _script):
            return "ready"

    class Pw:
        async def stop(self):
            closed.append("playwright")

    class Browser:
        async def close(self):
            closed.append("browser")

    async def no_bridge(_action, _args):
        return None

    async def get_page(**_kwargs):
        return Pw(), Browser(), object(), Page()

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", no_bridge)
    monkeypatch.setattr(browser, "_get_page", get_page)
    ready = await browser.execute_browser_get_text({
        "url": "https://example.invalid", "wait_ms": 0,
        "wait_until": "networkidle", "timeout_ms": 61_000,
    })
    assert ready == "ready" and calls[-1] == ("networkidle", 61_000)
    timed_out = await browser.execute_browser_get_text({
        "url": "https://example.invalid", "wait_ms": 0, "timeout_ms": 1,
    })
    assert timed_out.startswith("Error:") and "selected deadline" in timed_out
    task = asyncio.create_task(browser.execute_browser_get_text({
        "url": "https://example.invalid", "wait_ms": 0, "timeout_ms": 99_000,
    }))
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed[-2:] == ["browser", "playwright"]


@pytest.mark.asyncio
async def test_standalone_browser_session_does_not_silently_cut_text(monkeypatch):
    payload = "session-" * 3000 + "-complete-tail"

    class Page:
        async def evaluate(self, _script):
            return payload

    class Closed:
        async def close(self):
            pass

        async def stop(self):
            pass

    async def no_bridge(_action, _args):
        return None

    async def get_page(**_kwargs):
        return Closed(), Closed(), object(), Page()

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", no_bridge)
    monkeypatch.setattr(browser, "_get_page", get_page)
    result = await browser.execute_browser_session({"steps": [{"action": "get_text"}]})
    assert result == "[get_text] " + payload


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["browser_get_text", "browser_get_html", "browser_session"])
async def test_standalone_browser_long_output_is_recoverable_in_owning_run(tmp_path, monkeypatch, tool_name):
    from tools.registry import ToolRegistry

    payload = "long-page-" * 5000 + "unique-page-tail"
    registry = ToolRegistry([tool_name], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
                            audit_context={"hashi_run_id": "run-one", "request_id": "request-one"})

    class Page:
        async def goto(self, _url, *, wait_until, timeout):
            pass

        async def evaluate(self, _script):
            return payload

        async def content(self):
            return payload

    class Closed:
        async def close(self):
            pass

        async def stop(self):
            pass

    async def no_bridge(_action, _args):
        return None

    async def get_page(**_kwargs):
        return Closed(), Closed(), object(), Page()

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", no_bridge)
    monkeypatch.setattr(browser, "_get_page", get_page)
    args = ({"steps": [{"action": "get_text"}]} if tool_name == "browser_session"
            else {"url": "https://example.invalid", "wait_ms": 0})
    first = await registry.execute(tool_name, args)
    assert not first.is_error, first.output
    reference = first.details["output_continuation"]
    pages, offset = [], 0
    while offset is not None:
        result = await registry.execute("tool_output_read", {"reference": reference, "offset": offset})
        assert not result.is_error, result.output
        body = json.loads(result.output)
        pages.append(body["text"])
        offset = body["next_offset"]
    expected = "[get_text] " + payload if tool_name == "browser_session" else payload
    assert "".join(pages) == expected


@pytest.mark.asyncio
async def test_extension_navigation_timeout_is_forwarded_and_unsupported_readiness_is_explicit(monkeypatch):
    from tools import browser_extension_bridge as bridge
    captured = []

    monkeypatch.setattr(bridge, "bridge_available", lambda **_kwargs: True)
    sessions = []

    def ensure_session(**kwargs):
        sessions.append(kwargs)
        return {"session": {"session_id": "fixture"}, "extension_meta": {"actions": ["get_text"]}}

    monkeypatch.setattr(bridge, "ensure_bridge_session", ensure_session)

    def send(action, args, **kwargs):
        captured.append((action, args, kwargs))
        return {"ok": True, "output": "complete"}

    monkeypatch.setattr(bridge, "send_bridge_command", send)
    result = await browser._maybe_execute_extension_bridge(
        "get_text", {"url": "https://example.invalid", "timeout_ms": 90_000, "wait_ms": 500},
    )
    assert result == "complete"
    assert captured[0][2]["timeout_s"] == 95.5
    assert sessions[0]["timeout_s"] == 95.5
    result = await browser._maybe_execute_extension_bridge(
        "get_text", {"url": "https://example.invalid", "wait_until": "networkidle"},
    )
    assert result.startswith("Error:") and "supports only load readiness" in result
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_real_standalone_browser_delayed_navigation_and_full_text(tmp_path, monkeypatch):
    import asyncio
    import os
    import time
    from urllib.parse import urlsplit
    from aiohttp import web
    from tools.registry import ToolRegistry

    if os.environ.get("HASHI_HX036_REAL_BROWSER") != "1":
        pytest.skip("opt-in disposable local browser qualification")
    playwright = pytest.importorskip("playwright.async_api")
    stopping = asyncio.Event()
    hang_started = asyncio.Event()
    payload = "local-page-" * 3000 + "LOCAL-UNIQUE-TAIL"

    async def delayed(request):
        seconds = float(request.query.get("seconds", "0"))
        if seconds:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                pass
        html = f"<html><body>{payload}<script>fetch('/hang')</script></body></html>"
        return web.Response(text=html, content_type="text/html")

    async def hang(_request):
        hang_started.set()
        await stopping.wait()
        return web.Response(text="done")

    app = web.Application()
    app.router.add_get("/delay", delayed)
    app.router.add_get("/hang", hang)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    async def no_bridge(_action, _args):
        return None

    async def disposable_page(**_kwargs):
        pw = await playwright.async_playwright().start()
        try:
            browser_instance = await pw.chromium.launch(headless=True, args=[
                "--disable-background-networking", "--disable-component-update", "--no-first-run",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
            ])
            context = await browser_instance.new_context()

            async def only_fixture(route):
                url = urlsplit(route.request.url)
                if url.hostname == "127.0.0.1" and url.port == port:
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", only_fixture)
            page = await context.new_page()
            return pw, browser_instance, context, page
        except BaseException:
            await pw.stop()
            raise

    monkeypatch.setattr(browser, "_maybe_execute_extension_bridge", no_bridge)
    monkeypatch.setattr(browser, "_get_page", disposable_page)
    registry = ToolRegistry(["browser_get_text"], access_root=tmp_path, workspace_dir=tmp_path,
                            secrets={}, audit_context={"hashi_run_id": "real-run", "request_id": "real-request"})
    try:
        started = time.monotonic()
        first = await registry.execute("browser_get_text", {
            "url": f"http://127.0.0.1:{port}/delay?seconds=30.2",
            "timeout_ms": 40_000, "wait_ms": 0,
        })
        assert not first.is_error, first.output
        assert time.monotonic() - started >= 30
        assert hang_started.is_set()
        reference = first.details["output_continuation"]
        offset, parts = 0, []
        while offset is not None:
            page = await registry.execute("tool_output_read", {"reference": reference, "offset": offset})
            assert not page.is_error, page.output
            data = json.loads(page.output)
            parts.append(data["text"])
            offset = data["next_offset"]
        assert "".join(parts) == payload

        short = await browser.execute_browser_get_text({
            "url": f"http://127.0.0.1:{port}/delay?seconds=5",
            "timeout_ms": 150, "wait_ms": 0,
        })
        assert short.startswith("Error:") and "Timeout" in short
    finally:
        stopping.set()
        await runner.cleanup()
