"""Fix the one broken escape in pathing.py produced by apply_edits.py."""
from pathlib import Path

p = Path("/mnt/c/home/lily/projects/hashi/antigravity-cli-hashi1/orchestrator/pathing.py")
text = p.read_text(encoding="utf-8")
bad = 'if ("/" in candidate or "\\" in candidate or Path(candidate).is_absolute()) and Path('
good = 'if ("/" in candidate or "\\\\" in candidate or Path(candidate).is_absolute()) and Path('
count = text.count(bad)
assert count == 1, f"count={count}"
p.write_text(text.replace(bad, good, 1), encoding="utf-8")
print("FIXED")
