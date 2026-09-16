"""Red/Green evidence helper.

--red:      back up and remove the antigravity registry entry and the
            resolve_agy_executable helper (reverts the implementation).
--restore:  restore both files from the backups.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path("/mnt/c/home/lily/projects/hashi/antigravity-cli-hashi1")
REG = ROOT / "adapters/registry.py"
PATHING = ROOT / "orchestrator/pathing.py"
BAK = ROOT / "exp/antigravity-cli-hashi1/.redgreen-backup"
BAK.mkdir(parents=True, exist_ok=True)


def red() -> int:
    shutil.copy(REG, BAK / "registry.py")
    shutil.copy(PATHING, BAK / "pathing.py")

    text = REG.read_text(encoding="utf-8")
    line = '    "antigravity-cli": ("adapters.antigravity_cli", "AntigravityCLIAdapter"),\n'
    assert text.count(line) == 1
    REG.write_text(text.replace(line, "", 1), encoding="utf-8")

    text = PATHING.read_text(encoding="utf-8")
    start = text.index("def resolve_agy_executable(")
    end = text.index("def to_home_relative(")
    PATHING.write_text(text[:start] + text[end:], encoding="utf-8")
    print("RED applied")
    return 0


def restore() -> int:
    shutil.copy(BAK / "registry.py", REG)
    shutil.copy(BAK / "pathing.py", PATHING)
    print("RESTORE applied")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(red() if mode == "--red" else (restore() if mode == "--restore" else 2))
