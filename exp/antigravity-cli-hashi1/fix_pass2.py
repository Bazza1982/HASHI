"""Second-pass fixes: mock argv capture + adaptor start-activity touch."""
from pathlib import Path

ROOT = Path("/mnt/c/home/lily/projects/hashi/antigravity-cli-hashi1")

# 1. Mock: capture the original argv before the parse loop consumes it.
mock = ROOT / "tests/mocks/bin/agy"
text = mock.read_text(encoding="utf-8")
old_loop = 'while [[ $# -gt 0 ]]; do\n    case "$1" in\n'
assert text.count(old_loop) == 1
text = text.replace(old_loop, 'ORIG_ARGS="$*"\n\n' + old_loop, 1)
old_log = 'printf \'%s\\n\' "$*" >> "$AGY_MOCK_LOG"'
assert text.count(old_log) == 1
text = text.replace(old_log, 'printf \'%s\\n\' "$ORIG_ARGS" >> "$AGY_MOCK_LOG"', 1)
mock.write_text(text, encoding="utf-8")
print("OK mock")

# 2. Adaptor: touch activity right after the subprocess starts so that
#    idle-timeout measures silence from launch, not only after first output.
adaptor = ROOT / "adapters/antigravity_cli.py"
text = adaptor.read_text(encoding="utf-8")
old = '''            self.logger.info(
                f"Antigravity subprocess started for {request_id} "
                f"(pid={self.current_proc.pid})"
            )'''
assert text.count(old) == 1
new = old + '\n            self._touch_activity()'
text = text.replace(old, new, 1)
adaptor.write_text(text, encoding="utf-8")
print("OK adaptor")
