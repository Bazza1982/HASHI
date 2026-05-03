from __future__ import annotations

import time
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

try:
    from . import win32
except Exception:  # pragma: no cover - allows WSL/Linux unit tests to import probe helpers
    win32 = None  # type: ignore[assignment]


WHATSAPP_TITLE_NEEDLES = ("whatsapp",)
CALL_TEXT_NEEDLES = (
    "incoming",
    "voice call",
    "video call",
    "calling",
    "ringing",
    "answer",
    "accept",
    "missed voice call",
    "missed call",
)
ANSWER_TEXT_NEEDLES = ("answer", "accept")
STRONG_CALL_TEXT_NEEDLES = ("incoming", "ringing", "answer", "accept")
MISSED_CALL_TEXT_NEEDLES = ("missed voice call", "missed call")


@dataclass(frozen=True)
class ProbeResult:
    detected: bool
    active_call_detected: bool
    missed_call_detected: bool
    detection_method: str
    checked_at: float
    windows: list[dict[str, Any]]
    processes: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    answer_attempted: bool = False
    answer_clicked: bool = False
    error: str | None = None


def _matches_any(value: str, needles: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def _whatsapp_windows() -> list[dict[str, Any]]:
    if win32 is None:
        return []
    return [
        item
        for item in win32.list_windows()
        if _matches_any(str(item.get("title", "")), WHATSAPP_TITLE_NEEDLES)
    ]


def _whatsapp_processes() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    "Get-Process | "
                    "Where-Object { $_.ProcessName -match 'WhatsApp' } | "
                    "Select-Object Id,ProcessName,MainWindowTitle,Path | ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        import json

        payload = json.loads(proc.stdout)
    except Exception:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    return [
        {
            "pid": int(item.get("Id", 0) or 0),
            "process_name": str(item.get("ProcessName", "") or ""),
            "main_window_title": str(item.get("MainWindowTitle", "") or ""),
            "path": str(item.get("Path", "") or ""),
        }
        for item in payload
        if int(item.get("Id", 0) or 0)
    ]


def _is_strong_call_signal(text: str) -> bool:
    return _matches_any(text, STRONG_CALL_TEXT_NEEDLES)


def _is_missed_call_signal(text: str) -> bool:
    return _matches_any(text, MISSED_CALL_TEXT_NEEDLES)


def _signal_kind(text: str, is_answer_signal: bool) -> str:
    if is_answer_signal:
        return "answer_candidate"
    if _is_missed_call_signal(text):
        return "missed_call"
    if _is_strong_call_signal(text):
        return "active_call"
    if _matches_any(text, ("voice call", "video call", "calling")):
        return "call_text"
    return "unknown"


def _uia_probe(
    auto_answer: bool,
    whatsapp_pids: set[int],
    *,
    max_depth: int,
    include_tree: bool,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any], str | None]:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on Windows host packages
        return [], False, {"uia_available": False}, f"uiautomation unavailable: {exc}"

    signals: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "uia_available": True,
        "uia_windows_considered": 0,
        "uia_controls_visited": 0,
        "uia_max_depth": max_depth,
    }
    if include_tree:
        diagnostics["uia_tree"] = []
    clicked = False

    try:
        root = auto.GetRootControl()
        windows = root.GetChildren()
        for window in windows:
            name = str(getattr(window, "Name", "") or "")
            class_name = str(getattr(window, "ClassName", "") or "")
            process_id = int(getattr(window, "ProcessId", 0) or 0)
            if (
                process_id not in whatsapp_pids
                and not _matches_any(name, WHATSAPP_TITLE_NEEDLES)
                and not _matches_any(class_name, WHATSAPP_TITLE_NEEDLES)
            ):
                continue

            diagnostics["uia_windows_considered"] += 1
            stack = [(window, 0)]
            while stack:
                control, depth = stack.pop()
                diagnostics["uia_controls_visited"] += 1
                control_name = str(getattr(control, "Name", "") or "")
                control_type = str(getattr(control, "ControlTypeName", "") or "")
                automation_id = str(getattr(control, "AutomationId", "") or "")
                text = " ".join(part for part in (control_name, control_type, automation_id) if part)
                is_call_signal = _matches_any(text, CALL_TEXT_NEEDLES)
                is_answer_signal = _matches_any(text, ANSWER_TEXT_NEEDLES)
                kind = _signal_kind(text, is_answer_signal)

                if include_tree and (control_name or automation_id):
                    diagnostics["uia_tree"].append(
                        {
                            "depth": depth,
                            "window_name": name,
                            "window_process_id": process_id,
                            "control_name": control_name,
                            "control_type": control_type,
                            "automation_id": automation_id,
                        }
                    )

                if is_call_signal:
                    signals.append(
                        {
                            "source": "uia",
                            "kind": kind,
                            "window_name": name,
                            "window_process_id": process_id,
                            "control_name": control_name,
                            "control_type": control_type,
                            "automation_id": automation_id,
                            "is_answer_candidate": is_answer_signal,
                            "is_strong_call_signal": _is_strong_call_signal(text),
                            "is_missed_call_signal": _is_missed_call_signal(text),
                        }
                    )

                if auto_answer and is_answer_signal and not clicked:
                    try:
                        control.Click(simulateMove=False)
                        clicked = True
                    except TypeError:
                        control.Click()
                        clicked = True

                if depth < max_depth:
                    try:
                        for child in control.GetChildren():
                            stack.append((child, depth + 1))
                    except Exception:
                        continue
    except Exception as exc:  # pragma: no cover - depends on live Windows UI state
        return signals, clicked, diagnostics, f"uiautomation probe failed: {exc}"

    return signals, clicked, diagnostics, None


def probe_whatsapp_call(
    *,
    auto_answer: bool = False,
    use_uia: bool = True,
    uia_max_depth: int = 10,
    include_uia_tree: bool = False,
) -> dict[str, Any]:
    """Inspect WhatsApp Desktop for incoming-call signals.

    This is intentionally conservative. By default it detects and reports signals
    only. Clicking the answer control requires explicit ``auto_answer=True``.
    """

    windows = _whatsapp_windows()
    processes = _whatsapp_processes()
    whatsapp_pids = {int(item["pid"]) for item in processes if item.get("pid")}
    signals: list[dict[str, Any]] = []
    errors: list[str] = []
    diagnostics: dict[str, Any] = {
        "uia_enabled": use_uia,
        "whatsapp_pid_count": len(whatsapp_pids),
    }

    for window in windows:
        title = str(window.get("title", ""))
        if _is_strong_call_signal(title):
            signals.append({"source": "window_title", "kind": "active_call", "title": title, "window": window})
        elif _is_missed_call_signal(title):
            signals.append({"source": "window_title", "kind": "missed_call", "title": title, "window": window})

    answer_clicked = False
    if use_uia:
        uia_signals, clicked, uia_diagnostics, error = _uia_probe(
            auto_answer=auto_answer,
            whatsapp_pids=whatsapp_pids,
            max_depth=max(1, int(uia_max_depth)),
            include_tree=include_uia_tree,
        )
        signals.extend(uia_signals)
        diagnostics.update(uia_diagnostics)
        answer_clicked = clicked
        if error:
            errors.append(error)

    active_call_detected = any(
        bool(item.get("is_answer_candidate"))
        or item.get("kind") == "answer_candidate"
        or item.get("kind") == "active_call"
        or item.get("source") == "window_title"
        for item in signals
        if item.get("kind") != "missed_call"
    )
    missed_call_detected = any(
        bool(item.get("is_missed_call_signal")) or item.get("kind") == "missed_call"
        for item in signals
    )
    detected = active_call_detected or missed_call_detected
    if active_call_detected:
        detection_method = "uia" if any(item.get("source") == "uia" and item.get("kind") != "missed_call" for item in signals) else "window_title"
    elif missed_call_detected:
        detection_method = "uia_missed_call" if any(item.get("source") == "uia" for item in signals) else "window_title_missed_call"
    else:
        detection_method = "uia" if use_uia else "window_title"

    result = ProbeResult(
        detected=detected,
        active_call_detected=active_call_detected,
        missed_call_detected=missed_call_detected,
        detection_method=detection_method,
        checked_at=time.time(),
        windows=windows,
        processes=processes,
        signals=signals,
        diagnostics=diagnostics,
        answer_attempted=auto_answer,
        answer_clicked=answer_clicked,
        error="; ".join(errors) if errors else None,
    )
    return asdict(result)
