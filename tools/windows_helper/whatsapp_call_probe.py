from __future__ import annotations

import time
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from . import win32


WHATSAPP_TITLE_NEEDLES = ("whatsapp",)
CALL_TEXT_NEEDLES = (
    "incoming",
    "voice call",
    "video call",
    "calling",
    "ringing",
    "answer",
    "accept",
)
ANSWER_TEXT_NEEDLES = ("answer", "accept")
STRONG_CALL_TEXT_NEEDLES = ("incoming", "ringing", "answer", "accept")


@dataclass(frozen=True)
class ProbeResult:
    detected: bool
    detection_method: str
    checked_at: float
    windows: list[dict[str, Any]]
    processes: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    answer_attempted: bool = False
    answer_clicked: bool = False
    error: str | None = None


def _matches_any(value: str, needles: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def _whatsapp_windows() -> list[dict[str, Any]]:
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


def _uia_probe(auto_answer: bool, whatsapp_pids: set[int]) -> tuple[list[dict[str, Any]], bool, str | None]:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on Windows host packages
        return [], False, f"uiautomation unavailable: {exc}"

    signals: list[dict[str, Any]] = []
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

            stack = [(window, 0)]
            while stack:
                control, depth = stack.pop()
                control_name = str(getattr(control, "Name", "") or "")
                control_type = str(getattr(control, "ControlTypeName", "") or "")
                automation_id = str(getattr(control, "AutomationId", "") or "")
                text = " ".join(part for part in (control_name, control_type, automation_id) if part)
                is_call_signal = _matches_any(text, CALL_TEXT_NEEDLES)
                is_answer_signal = _matches_any(text, ANSWER_TEXT_NEEDLES)

                if is_call_signal:
                    signals.append(
                        {
                            "source": "uia",
                            "window_name": name,
                            "window_process_id": process_id,
                            "control_name": control_name,
                            "control_type": control_type,
                            "automation_id": automation_id,
                            "is_answer_candidate": is_answer_signal,
                            "is_strong_call_signal": _is_strong_call_signal(text),
                        }
                    )

                if auto_answer and is_answer_signal and not clicked:
                    try:
                        control.Click(simulateMove=False)
                        clicked = True
                    except TypeError:
                        control.Click()
                        clicked = True

                if depth < 5:
                    try:
                        for child in control.GetChildren():
                            stack.append((child, depth + 1))
                    except Exception:
                        continue
    except Exception as exc:  # pragma: no cover - depends on live Windows UI state
        return signals, clicked, f"uiautomation probe failed: {exc}"

    return signals, clicked, None


def probe_whatsapp_call(*, auto_answer: bool = False, use_uia: bool = True) -> dict[str, Any]:
    """Inspect WhatsApp Desktop for incoming-call signals.

    This is intentionally conservative. By default it detects and reports signals
    only. Clicking the answer control requires explicit ``auto_answer=True``.
    """

    windows = _whatsapp_windows()
    processes = _whatsapp_processes()
    whatsapp_pids = {int(item["pid"]) for item in processes if item.get("pid")}
    signals: list[dict[str, Any]] = []
    errors: list[str] = []

    for window in windows:
        title = str(window.get("title", ""))
        if _is_strong_call_signal(title):
            signals.append({"source": "window_title", "title": title, "window": window})

    answer_clicked = False
    if use_uia:
        uia_signals, clicked, error = _uia_probe(auto_answer=auto_answer, whatsapp_pids=whatsapp_pids)
        signals.extend(uia_signals)
        answer_clicked = clicked
        if error:
            errors.append(error)

    detected = any(
        bool(item.get("is_answer_candidate"))
        or bool(item.get("is_strong_call_signal"))
        or item.get("source") == "window_title"
        for item in signals
    )
    result = ProbeResult(
        detected=detected,
        detection_method="uia" if any(item.get("source") == "uia" for item in signals) else "window_title",
        checked_at=time.time(),
        windows=windows,
        processes=processes,
        signals=signals,
        answer_attempted=auto_answer,
        answer_clicked=answer_clicked,
        error="; ".join(errors) if errors else None,
    )
    return asdict(result)
