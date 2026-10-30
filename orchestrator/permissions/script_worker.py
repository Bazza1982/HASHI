"""Fixed entrypoint for user Python, launched only after verified LPAC/Job setup.

No pipe, broker endpoint, token, signing key or inherited business file handle is
given to this program. Its caller owns all result inspection and cleanup.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys
import traceback


class _BoundedText:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.written = stream, maximum, 0
        self.encoding = "utf-8"

    def write(self, text):
        value = str(text)
        data = value.encode("utf-8", errors="replace")[:max(0, self.maximum - self.written)]
        if data:
            self.stream.write(data)
            self.stream.flush()
            self.written += len(data)
        return len(value)

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return False


def main():
    if len(sys.argv) != 2:
        raise SystemExit("an owned script scratch directory is required")
    root = Path(sys.argv[1])
    if not root.is_absolute() or Path.cwd() != root:
        raise SystemExit("the verified script working directory changed")
    source = root / "task.py"
    # CWD is deliberately not an import root under the isolated interpreter.
    # Inputs can be read as data; all imports come from the sealed runtime.
    previous = sys.stdout, sys.stderr
    with (root / "stdout.txt").open("wb") as out, (root / "stderr.txt").open("wb") as err:
        sys.stdout, sys.stderr = _BoundedText(out, 128 * 1024), _BoundedText(err, 64 * 1024)
        try:
            runpy.run_path(str(source), run_name="__main__")
        except SystemExit:
            raise
        except BaseException:
            traceback.print_exc()
            raise SystemExit(1) from None
        finally:
            sys.stdout, sys.stderr = previous


if __name__ == "__main__":
    main()
