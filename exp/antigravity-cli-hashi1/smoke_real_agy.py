"""Stage-4 real-agy headless smoke through the adapter (2 quota calls).

Run with Windows Python (agy.exe is Windows-native), cwd = worktree root:
    C:\\Users\\thene\\projects\\HASHI4\\.venv\\Scripts\\python.exe exp\\antigravity-cli-hashi1\\smoke_real_agy.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from adapters.antigravity_cli import AntigravityCLIAdapter  # noqa: E402

AGY = os.environ.get("AGY_EXE", r"C:\Users\thene\AppData\Local\agy\bin\agy.exe")
WORK = ROOT / "exp" / "antigravity-cli-hashi1" / "smoke-workspace"
WORK.mkdir(parents=True, exist_ok=True)


def make_config(tmp: Path, name: str):
    return SimpleNamespace(
        name=name,
        engine="antigravity-cli",
        model="gemini-3.8-flash-high",
        workspace_dir=tmp,
        extra={},
        resolve_access_root=lambda: tmp,
    )


async def main() -> int:
    results: dict = {}
    t0 = time.time()
    adapter = AntigravityCLIAdapter(
        make_config(WORK, "smoke-agent"), SimpleNamespace(agy_cmd=AGY)
    )
    results["initialize"] = await adapter.initialize()

    r1 = await adapter.generate_response(
        "Reply with exactly the word PONG and nothing else.", "smoke-1"
    )
    results["turn1"] = {
        "success": r1.is_success,
        "text": r1.text,
        "error": r1.error,
        "duration_ms": r1.duration_ms,
        "metadata": r1.stream_metadata,
        "usage_input": r1.usage.input_tokens if r1.usage else None,
        "usage_output": r1.usage.output_tokens if r1.usage else None,
    }
    cid1 = adapter._conversation_id
    results["conversation_id_turn1"] = cid1
    await adapter.shutdown()

    # Fresh adapter instance: the conversation must be restored from disk.
    adapter2 = AntigravityCLIAdapter(
        make_config(WORK, "smoke-agent"), SimpleNamespace(agy_cmd=AGY)
    )
    results["initialize2"] = await adapter2.initialize()
    r2 = await adapter2.generate_response(
        "What exact word did I ask you to reply with in my previous message? "
        "Reply with only that single word.",
        "smoke-2",
    )
    results["turn2"] = {
        "success": r2.is_success,
        "text": r2.text,
        "error": r2.error,
        "duration_ms": r2.duration_ms,
        "metadata": r2.stream_metadata,
        "usage_input": r2.usage.input_tokens if r2.usage else None,
        "usage_output": r2.usage.output_tokens if r2.usage else None,
    }
    results["conversation_id_turn2"] = adapter2._conversation_id
    await adapter2.shutdown()

    results["session_file"] = str(WORK / ".hashi-antigravity-session.json")
    results["elapsed_s"] = round(time.time() - t0, 2)

    out = Path(__file__).parent / "smoke-real-2026-09-16.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))

    verdict = bool(
        results.get("initialize")
        and results["turn1"]["success"]
        and results["turn2"]["success"]
        and results["turn1"]["text"].strip() == "PONG"
        and cid1
        and results["conversation_id_turn2"] == cid1
    )
    print("SMOKE_VERDICT:", "PASS" if verdict else "FAIL")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
