from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_WORKER = (
    ROOT / "tools" / "chrome_extension" / "hashi_browser_bridge" / "service_worker.js"
)


def test_production_extension_exposes_cdp_hover() -> None:
    source = SERVICE_WORKER.read_text(encoding="utf-8")

    assert "async function actionHover(args)" in source
    assert 'if (action === "hover")' in source
    assert '"Input.dispatchMouseEvent"' in source
    assert '"Runtime.evaluate"' in source
    assert 'type: "mouseMoved"' in source
    assert "await sleep(waitMs)" in source


def test_hover_resolves_viewport_coordinates_after_scrolling() -> None:
    source = SERVICE_WORKER.read_text(encoding="utf-8")

    assert 'element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" })' in source
    assert "const x = rect.left + rect.width * xRatio" in source
    assert "const y = rect.top + rect.height * yRatio" in source
