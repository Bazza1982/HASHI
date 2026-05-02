from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from orchestrator.voice.events import VoiceEventLogger
from orchestrator.voice.windows_helper_client import WindowsHelperClient


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("logs") / "voice_sessions" / f"whatsapp_desktop_phase0_{stamp}.jsonl"


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

    while True:
        try:
            result = client.action(
                "whatsapp_call_probe",
                {"auto_answer": args.auto_answer, "use_uia": not args.no_uia},
            )
            event = "call.incoming_detected" if result.get("detected") else "call.waiting"
            if result.get("detected") and not detected_once:
                detected_once = True
                result["pickup_latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            logger.write(event, **result)
            print(
                f"{event}: windows={len(result.get('windows') or [])} "
                f"signals={len(result.get('signals') or [])} "
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
    return parser


def main() -> int:
    return run_probe(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
