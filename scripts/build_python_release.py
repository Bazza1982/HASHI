"""Build Python release artifacts without mutating the runtime checkout.

Setuptools may create or refresh ``*.egg-info`` beside ``pyproject.toml``.
That checkout is also a live HASHI runtime input, so release builds run from an
exact Git ``HEAD`` export instead of the working tree.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path


CommandRunner = Callable[..., object]


def build_python_release(
    source_root: Path,
    output_dir: Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, ...]:
    """Build committed ``HEAD`` in an isolated export and return its artifacts."""

    source_root = Path(source_root).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hashi-python-release-") as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "source.tar"
        export_root = temporary_root / "source"
        export_root.mkdir()
        runner(
            (
                "git",
                "archive",
                "--format=tar",
                "--output",
                str(archive_path),
                "HEAD",
            ),
            cwd=source_root,
            check=True,
        )
        with tarfile.open(archive_path, "r") as archive:
            archive.extractall(export_root, filter="data")
        runner(
            (
                sys.executable,
                "-m",
                "build",
                "--outdir",
                str(output_dir),
            ),
            cwd=export_root,
            check=True,
        )

    artifacts = tuple(sorted(path for path in output_dir.iterdir() if path.is_file()))
    if not artifacts:
        raise RuntimeError("Python release build produced no artifacts")
    return artifacts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Python artifacts from an isolated Git HEAD export."
    )
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--source", type=Path, default=root)
    parser.add_argument("--outdir", type=Path, default=root / "dist")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    artifacts = build_python_release(arguments.source, arguments.outdir)
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
