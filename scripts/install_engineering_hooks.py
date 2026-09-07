"""Install the repository's lightweight checks without replacing custom hooks."""
from pathlib import Path
import subprocess


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    current = subprocess.run(["git", "config", "--get", "core.hooksPath"], capture_output=True, text=True).stdout.strip()
    if current and current != ".githooks":
        raise SystemExit("Custom core.hooksPath exists; keep it and invoke .githooks/pre-commit from that hook.")
    if not current:
        existing = Path(subprocess.check_output(["git", "rev-parse", "--git-path", "hooks/pre-commit"], text=True).strip())
        if not existing.is_absolute():
            existing = root / existing
        if existing.exists():
            raise SystemExit("Existing pre-commit hook retained; invoke .githooks/pre-commit from that hook.")
    subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], check=True)
    print("Engineering hooks enabled: staged core protection and diff checks.")


if __name__ == "__main__":
    main()
