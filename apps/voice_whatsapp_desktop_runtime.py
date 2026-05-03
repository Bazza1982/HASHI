from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from orchestrator.voice.events import VoiceEventLogger
from orchestrator.voice.windows_helper_client import WindowsHelperClient

OCR_CALL_NEEDLES = (
    "incoming voice call",
    "incoming video call",
    "missed voice call",
    "missed video call",
    "voice call",
    "video call",
    "answer",
    "accept",
    "decline",
)
OCR_ACTIVE_CALL_NEEDLES = ("incoming voice call", "incoming video call", "answer", "accept", "decline")
OCR_MISSED_CALL_NEEDLES = ("missed voice call", "missed video call")


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("logs") / "voice_sessions" / f"whatsapp_desktop_phase0_{stamp}.jsonl"


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def _ocr_probe(client: WindowsHelperClient, screenshot_path: Path, timeout: float) -> dict:
    """Capture a screenshot and scan visible text for WhatsApp call evidence."""

    if not shutil.which("tesseract"):
        return {
            "detected": False,
            "active_call_detected": False,
            "missed_call_detected": False,
            "detection_method": "visual_ocr",
            "signals": [],
            "screenshot_path": str(screenshot_path),
            "error": "tesseract executable not found",
        }

    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    capture = client.action("screenshot", {"save_path": str(screenshot_path)})
    try:
        proc = subprocess.run(
            ["tesseract", str(screenshot_path), "stdout", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {
            "detected": False,
            "active_call_detected": False,
            "missed_call_detected": False,
            "detection_method": "visual_ocr",
            "signals": [],
            "screenshot_path": str(screenshot_path),
            "capture": capture,
            "error": f"ocr failed: {exc}",
        }

    text = (proc.stdout or "").strip()
    active = _contains_any(text, OCR_ACTIVE_CALL_NEEDLES)
    missed = _contains_any(text, OCR_MISSED_CALL_NEEDLES)
    generic = _contains_any(text, OCR_CALL_NEEDLES)
    signals = []
    if active or missed or generic:
        signals.append(
            {
                "source": "visual_ocr",
                "kind": "active_call" if active else "missed_call" if missed else "call_text",
                "matched_text": text[:1000],
                "screenshot_path": str(screenshot_path),
            }
        )

    return {
        "detected": active or missed or generic,
        "active_call_detected": active,
        "missed_call_detected": missed,
        "detection_method": "visual_ocr",
        "signals": signals,
        "screenshot_path": str(screenshot_path),
        "capture": capture,
        "ocr_returncode": proc.returncode,
        "ocr_stderr": (proc.stderr or "").strip(),
        "ocr_text_sample": text[:1000],
    }


def run_probe(args: argparse.Namespace) -> int:
    client = WindowsHelperClient(base_url=args.helper_url, timeout=args.timeout)
    logger = VoiceEventLogger(Path(args.log_path) if args.log_path else _default_log_path())

    try:
        health = client.health()
        logger.write("helper.connected", helper_url=args.helper_url, health=health)
        print(f"helper ok: {health}")
    except Exception as exc:
        logger.write("helper.unavailable", helper_url=args.helper_url, error=str(exc))
        print(f"helper unavailable: {exc}", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    detected_once = False
    started = time.monotonic()
    last_ocr_at = 0.0

    while True:
        try:
            result = client.action(
                "whatsapp_call_probe",
                {
                    "auto_answer": args.auto_answer,
                    "use_uia": not args.no_uia,
                    "uia_max_depth": args.uia_depth,
                    "include_uia_tree": args.include_uia_tree,
                },
            )
            if args.ocr_fallback and not result.get("detected"):
                now = time.monotonic()
                if now - last_ocr_at >= args.ocr_interval:
                    last_ocr_at = now
                    screenshot_path = Path(args.ocr_screenshot_dir) / f"whatsapp_probe_{int(time.time())}.png"
                    ocr_result = _ocr_probe(client, screenshot_path, timeout=args.ocr_timeout)
                    logger.write("visual_ocr.checked", **ocr_result)
                    if ocr_result.get("detected"):
                        merged_signals = list(result.get("signals") or [])
                        merged_signals.extend(ocr_result.get("signals") or [])
                        result.update(
                            {
                                "detected": True,
                                "active_call_detected": ocr_result.get("active_call_detected", False),
                                "missed_call_detected": ocr_result.get("missed_call_detected", False),
                                "detection_method": ocr_result.get("detection_method", "visual_ocr"),
                                "signals": merged_signals,
                                "visual_ocr": ocr_result,
                            }
                        )
            event = "call.incoming_detected" if result.get("detected") else "call.waiting"
            if result.get("detected") and not detected_once:
                detected_once = True
                result["pickup_latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            logger.write(event, **result)
            print(
                f"{event}: windows={len(result.get('windows') or [])} "
                f"signals={len(result.get('signals') or [])} "
                f"active={bool(result.get('active_call_detected'))} "
                f"missed={bool(result.get('missed_call_detected'))} "
                f"answered={bool(result.get('answer_clicked'))}"
            )
            if args.once or (args.exit_on_detect and result.get("detected")):
                return 0 if result.get("detected") else 1
        except KeyboardInterrupt:
            logger.write("probe.stopped", reason="keyboard_interrupt")
            return 130
        except Exception as exc:
            logger.write("error.recoverable", component="windows_helper_client", message=str(exc))
            print(f"probe error: {exc}", file=sys.stderr)
            if args.once:
                return 2

        if deadline is not None and time.monotonic() >= deadline:
            logger.write("probe.stopped", reason="duration_elapsed", detected=detected_once)
            return 0 if detected_once else 1
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HASHI WhatsApp Desktop voice-call Phase 0 probe.")
    parser.add_argument("--helper-url", default="http://127.0.0.1:47831")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=float, default=0.0, help="Seconds to run; 0 means until stopped.")
    parser.add_argument("--log-path", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--exit-on-detect", action="store_true")
    parser.add_argument("--auto-answer", action="store_true", help="Click the detected answer control when available.")
    parser.add_argument("--no-uia", action="store_true", help="Disable UI Automation probing and use window titles only.")
    parser.add_argument("--uia-depth", type=int, default=10, help="Maximum UIA tree depth to scan inside WhatsApp windows.")
    parser.add_argument("--include-uia-tree", action="store_true", help="Log a compact UIA tree for diagnostics.")
    parser.add_argument("--ocr-fallback", action="store_true", help="Use screenshot OCR when UIA/window-title probing finds no signal.")
    parser.add_argument("--ocr-interval", type=float, default=5.0, help="Minimum seconds between OCR fallback checks.")
    parser.add_argument("--ocr-timeout", type=float, default=10.0, help="Seconds before OCR fallback times out.")
    parser.add_argument("--ocr-screenshot-dir", default="tmp/voice_ocr", help="Directory for OCR fallback screenshots.")
    return parser


def main() -> int:
    return run_probe(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
