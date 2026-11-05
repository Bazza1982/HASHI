#!/usr/bin/env python3
"""Copy CPython before any venv/pip/sidecar preparation; never write its source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_contract import enforce_runtime_contract  # noqa: E402
from orchestrator.runtime_installation_builder import _copy_runtime_tree  # noqa: E402


def prepare_bootstrap(output):
    output, source = Path(output).absolute(), Path(sys.base_prefix).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Bootstrap requires a fresh directory outside the source interpreter")
    for parent in output.parents:
        if ((parent / "pyvenv.cfg").exists()
                or ((parent / "python.exe").exists() and (parent / "Lib").is_dir())):
            raise ValueError("Bootstrap output must not modify an existing Python environment")
    enforce_runtime_contract(ROOT, require_standard_dependencies=False)
    base = _copy_runtime_tree(source, output / "sidecar-base", base=True)
    executable = base / ("python.exe" if os.name == "nt" else "bin/python3")
    tools = output / "bootstrap"
    subprocess.run([str(executable), "-I", "-B", "-m", "venv", "--copies", str(tools)],
                   check=True, capture_output=True, timeout=180)
    python = tools / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return {"python": str(python), "base": str(base), "source_interpreter_modified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare_bootstrap(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_28a2fa4a0d714462aea0378c389afa06
