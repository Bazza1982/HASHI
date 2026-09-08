#!/usr/bin/env python3
"""Build the allowlisted HASHI Portable Windows x64 directory image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets as secrets_module
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_IMAGE_BYTES = 957_000_000
TARGET_IMAGE_BYTES = 820 * 1024 * 1024
CAPACITY_CHECK_CLUSTER_BYTES = 32 * 1024
PAIRING_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60

HERE = Path(__file__).resolve().parent
HASHI_ROOT = HERE.parents[1]
TEMPLATES = HERE / "templates"

if str(HASHI_ROOT) not in sys.path:
    sys.path.insert(0, str(HASHI_ROOT))

from orchestrator.runtime_contract import (  # noqa: E402
    CORE_SOURCE_PATHS,
    RuntimePolicy,
    core_source_digest,
    load_runtime_policy,
    locked_standard_dependencies,
)

_RUNTIME_POLICY = load_runtime_policy(HASHI_ROOT)
PYTHON_VERSION = _RUNTIME_POLICY.python_text
PYTHON_BUILD_DATE = _RUNTIME_POLICY.portable_build_date
STANDARD_LOCK_RELATIVE = Path(_RUNTIME_POLICY.standard_lock)


@dataclass(frozen=True)
class Asset:
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class SourceIdentity:
    revision: str
    tree: str


ASSETS = {
    "7zip": Asset(
        "7z2603-x64.msi",
        "https://github.com/ip7z/7zip/releases/download/26.03/7z2603-x64.msi",
        "c0680064d698a62dd4a5a47f403db356a6531a5473e4c4b1d090ea2590513926",
    ),
    "python": Asset(
        f"cpython-{PYTHON_VERSION}+{PYTHON_BUILD_DATE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{PYTHON_BUILD_DATE}/cpython-{PYTHON_VERSION}+{PYTHON_BUILD_DATE}"
            "-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
        ),
        "10b7a95b928e551fc78cac665999e1ae1f08fb738b255adb0a8d3b9c2824a9c0",
    ),
    "ffmpeg": Asset(
        "ffmpeg-9.0.1-essentials_build.zip",
        "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip",
        "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9",
    ),
    "piper": Asset(
        "piper_windows_amd64.zip",
        "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip",
        "f3c58906402b24f3a96d92145f58acba6d86c9b5db896d207f78dc80811efcea",
    ),
    "piper_license": Asset(
        "piper-LICENSE.md",
        "https://raw.githubusercontent.com/rhasspy/piper/2023.11.14-2/LICENSE.md",
        "4cd71dece7037f1d6d93cce7570c57ab75ea9ac566fd4990be2f3ab08d15b47f",
    ),
    "piper_voice": Asset(
        "zh_CN-huayan-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx",
        "9929917bf8cabb26fd528ea44d3a6699c11e87317a14765312420be230be0f3d",
    ),
    "piper_voice_config": Asset(
        "zh_CN-huayan-medium.onnx.json",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json",
        "d521dc45504a8ccc99e325822b35946dd701840bfb07e3dbb31a40929ed6a82b",
    ),
    "piper_voice_card": Asset(
        "zh_CN-huayan-MODEL_CARD",
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/MODEL_CARD",
        "25d7d8f7a03e9382e629c5c4f074176e7ca84e08be12a873022e91fcac98c2c7",
    ),
    "tesseract": Asset(
        "tesseract-ocr-w64-setup-5.5.3.20260724.exe",
        "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe",
        "bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4",
    ),
}

SOURCE_DIRS = (
    "adapters",
    "agent_seeds",
    "apps",
    "browser_gateway",
    "flow",
    "hashi_assets",
    "locales",
    "nagare",
    "native",
    "onboarding",
    "orchestrator",
    "remote",
    "scripts",
    "skills",
    "superloops",
    "tools",
    "transports",
    "tui",
    "veritas",
)
ROOT_SOURCE_FILES = ("__main__.py", "main.py", "tui.py", "pyproject.toml", "LICENSE")
RUNTIME_POLICY_FILES = (_RUNTIME_POLICY.standard_lock,)
ROOT_PACKAGE_FILES = (
    "exp/__init__.py",
    "exp/loader.py",
    "exp/asset-packs.json",
)
PRUNED_SOURCE_PATHS = (
    "adapters/claude_cli.py",
    "adapters/codex_app_server.py",
    "adapters/codex_cli.py",
    "adapters/codex_errors.py",
    "adapters/codex_event_log.py",
    "adapters/gemini_cli.py",
    "adapters/grok_cli.py",
    "adapters/hashi_api.py",
    "adapters/ollama_api.py",
    "adapters/xai_api.py",
    "skills/claude",
    "skills/codex",
    "skills/gemini",
    "skills/agent-audit",
    "skills/hermes-memory-import",
    "skills/library-pick",
    "skills/memory-consolidation",
    "flow/evaluation_kb/improvements",
    "scripts/backfill_codex_tokens.py",
    "scripts/check_stress_test.ps1",
    "scripts/consolidate_memory.py",
    "scripts/dual_brain_common.py",
    "scripts/dual_brain_context.py",
    "scripts/generate_agent_behavior_audit.py",
    "scripts/gitwatch.py",
    "scripts/install_elevated_autostart.ps1",
    "scripts/link_whatsapp.py",
    "scripts/memory_to_obsidian.py",
    "scripts/monitor_hashi1.py",
    "scripts/nuclear_reset.py",
    "scripts/patrol_errors.py",
    "scripts/query_memory.py",
    "scripts/remote_memory_consolidation.py",
    "scripts/hashi_remote_watchdog.py",
    "scripts/reset_dual_brain_notepads.py",
    "scripts/run_dual_brain_turn.py",
    "scripts/send_whatsapp_test.py",
    "scripts/start_stress_test.ps1",
    "scripts/wiki_generate_review.py",
    "scripts/wiki_organise.py",
    "scripts/wiki_organise_cron.sh",
    "tools/browser_bridge_acceptance.py",
    "tools/browser_bridge_live_acceptance.py",
    "tools/browser_bridge_maturity.py",
    "tools/browser_bridge_smoke_runner.py",
    "tools/browser_bridge_stub_server.py",
    "tools/browser_bridge_test_bundle.py",
    "tools/browser_bridge_test_env.py",
    "tools/browser_bridge_test_runner.py",
    "tools/windows_use_evaluation.py",
    "veritas/SETUP.md",
    "superloops/loops",
)
IGNORED_SOURCE_NAMES = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "recordings",
    "runs",
}


def status(message: str) -> None:
    print(f"[portable] {message}", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(argv: list[str], *, cwd: Path | None = None) -> None:
    status(
        "run: "
        + " ".join(
            Path(value).name if index == 0 else value
            for index, value in enumerate(argv)
        )
    )
    subprocess.run(argv, cwd=cwd, check=True)


def download(asset: Asset, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / asset.filename
    if destination.is_file() and sha256_file(destination) == asset.sha256:
        status(f"cache hit: {asset.filename}")
        return destination
    if destination.exists():
        destination.unlink()
    partial = destination.with_suffix(destination.suffix + ".part")
    if partial.exists():
        partial.unlink()
    status(f"download: {asset.filename}")
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": "HASHI-Portable-Builder/1"}
    )
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        partial.open("wb") as output,
    ):
        shutil.copyfileobj(response, output, length=1024 * 1024)
    actual = sha256_file(partial)
    if actual != asset.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"integrity check failed for {asset.filename}: expected {asset.sha256}, got {actual}"
        )
    partial.replace(destination)
    return destination


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def ignored_tracked_source(relative: Path) -> bool:
    if relative == STANDARD_LOCK_RELATIVE:
        return False
    for part in relative.parts:
        if (
            part in IGNORED_SOURCE_NAMES
            or part.endswith((".pyc", ".pyo", ".log", ".lock", ".pid"))
            or (part.startswith(".") and part != ".well-known")
        ):
            return True
    return False


def copy_hashi_source(destination: Path) -> None:
    status("copy allowlisted, Git-tracked HASHI source")
    destination.mkdir(parents=True, exist_ok=True)
    requested = (
        *SOURCE_DIRS,
        *ROOT_SOURCE_FILES,
        *ROOT_PACKAGE_FILES,
        *RUNTIME_POLICY_FILES,
    )
    result = subprocess.run(
        ["git", "-C", str(HASHI_ROOT), "ls-files", "-z", "--", *requested],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not enumerate Git-tracked source files: {detail}")
    tracked = [
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in result.stdout.split(b"\0")
        if value
    ]
    if not tracked:
        raise RuntimeError("Git-tracked portable source set is empty")

    pruned = tuple(Path(value) for value in PRUNED_SOURCE_PATHS)
    for relative in tracked:
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe Git-tracked source path: {relative}")
        if ignored_tracked_source(relative):
            continue
        if any(relative == item or item in relative.parents for item in pruned):
            continue
        source = HASHI_ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(
                f"Git-tracked source is missing or not a file: {relative}"
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _runtime_input_paths(policy: RuntimePolicy) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(("pyproject.toml", policy.standard_lock, *CORE_SOURCE_PATHS))
    )


def validate_portable_runtime_inputs(
    app_hashi: Path,
    *,
    source_root: Path | None = None,
) -> RuntimePolicy:
    """Require the copied image to carry the authoritative runtime inputs."""

    source_root = HASHI_ROOT if source_root is None else Path(source_root).resolve()
    app_hashi = Path(app_hashi).resolve()
    source_policy = load_runtime_policy(source_root)
    copied_policy = load_runtime_policy(app_hashi)
    if copied_policy != source_policy:
        raise RuntimeError(
            "portable runtime policy differs from the selected HASHI source"
        )

    mismatched: list[str] = []
    for relative in _runtime_input_paths(source_policy):
        source = source_root / relative
        copied = app_hashi / relative
        if not source.is_file() or not copied.is_file():
            mismatched.append(relative)
            continue
        if sha256_file(source) != sha256_file(copied):
            mismatched.append(relative)
    if mismatched:
        raise RuntimeError(
            "portable runtime contract inputs are missing or changed: "
            + ", ".join(mismatched)
        )

    locked_standard_dependencies(app_hashi, copied_policy)
    if core_source_digest(app_hashi) != core_source_digest(source_root):
        raise RuntimeError("portable protected Core source digest differs from source")
    return copied_policy


_LOCKED_PORTABLE_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)")


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _hashed_portable_dependencies(lock_path: Path) -> dict[str, str]:
    try:
        raw_lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(
            f"portable dependency lock is unavailable: {lock_path}: {exc}"
        ) from exc

    dependencies: dict[str, str] = {}
    missing_hashes: list[str] = []
    logical = ""
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical = f"{logical} {stripped}".strip()
        if logical.endswith("\\"):
            logical = logical[:-1].rstrip()
            continue
        match = _LOCKED_PORTABLE_REQUIREMENT.match(logical)
        if match is not None:
            name = _normalized_distribution_name(match.group(1))
            version = match.group(2)
            previous = dependencies.get(name)
            if previous is not None and previous != version:
                raise RuntimeError(
                    f"portable dependency lock has conflicting pins for {name}: "
                    f"{previous}, {version}"
                )
            dependencies[name] = version
            if "--hash=sha256:" not in logical:
                missing_hashes.append(name)
        logical = ""
    if logical:
        raise RuntimeError("portable dependency lock ends with an incomplete entry")
    if not dependencies:
        raise RuntimeError("portable dependency lock is empty")
    if missing_hashes:
        raise RuntimeError(
            "portable dependency lock has unhashed entries: "
            + ", ".join(sorted(missing_hashes))
        )
    return dependencies


def _requirement_directive_paths(input_path: Path, option: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw in input_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pieces = line.split(maxsplit=1)
        if len(pieces) == 2 and pieces[0] == option:
            paths.append((input_path.parent / pieces[1]).resolve())
    return tuple(paths)


def _portable_input_dependencies(input_path: Path) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for line_number, raw in enumerate(
        input_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith(("#", "-c ", "-r ")):
            continue
        match = _LOCKED_PORTABLE_REQUIREMENT.fullmatch(line)
        if match is None:
            raise RuntimeError(
                "portable-only dependencies must use exact pins in "
                f"requirements.in:{line_number}: {line}"
            )
        name = _normalized_distribution_name(match.group(1))
        version = match.group(2)
        previous = dependencies.get(name)
        if previous is not None and previous != version:
            raise RuntimeError(
                f"portable requirements.in has conflicting pins for {name}: "
                f"{previous}, {version}"
            )
        dependencies[name] = version
    if not dependencies:
        raise RuntimeError("portable requirements.in has no portable-only dependencies")
    return dependencies


def validate_portable_dependency_generation(
    *,
    source_root: Path = HASHI_ROOT,
    portable_root: Path = HERE,
) -> dict[str, str]:
    """Fail closed unless the portable lock contains the standard generation."""

    source_root = Path(source_root).resolve()
    portable_root = Path(portable_root).resolve()
    policy = load_runtime_policy(source_root)
    standard = locked_standard_dependencies(source_root, policy)
    input_path = portable_root / "requirements.in"
    standard_lock = (source_root / policy.standard_lock).resolve()
    root_requirements = (source_root / "requirements.txt").resolve()
    try:
        constraints = _requirement_directive_paths(input_path, "-c")
        requirements = _requirement_directive_paths(input_path, "-r")
    except OSError as exc:
        raise RuntimeError(
            f"portable dependency input is unavailable: {input_path}: {exc}"
        ) from exc
    if constraints != (standard_lock,):
        raise RuntimeError(
            "portable requirements.in must constrain exactly the runtime policy lock: "
            f"{policy.standard_lock}"
        )
    if requirements != (root_requirements,):
        raise RuntimeError(
            "portable requirements.in must include exactly the standard requirements.txt"
        )
    if not root_requirements.is_file():
        raise RuntimeError(
            f"standard requirements input is unavailable: {root_requirements}"
        )

    portable_inputs = _portable_input_dependencies(input_path)
    portable = _hashed_portable_dependencies(portable_root / "requirements.lock")
    mismatches = [
        f"{name}: portable={portable.get(name, 'missing')}, standard={version}"
        for name, version in sorted(standard.items())
        if portable.get(name) != version
    ]
    if mismatches:
        raise RuntimeError(
            "portable dependency generation does not match the standard runtime lock: "
            + "; ".join(mismatches[:20])
        )
    input_mismatches = [
        f"{name}: lock={portable.get(name, 'missing')}, input={version}"
        for name, version in sorted(portable_inputs.items())
        if portable.get(name) != version
    ]
    if input_mismatches:
        raise RuntimeError(
            "portable dependency generation does not match its direct inputs: "
            + "; ".join(input_mismatches)
        )
    return portable


def extract_python(runtime_python: Path, cache: Path) -> None:
    archive = download(ASSETS["python"], cache)
    status(f"extract Python {PYTHON_VERSION}")
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    if runtime_python.exists():
        raise RuntimeError(
            f"Python runtime destination already exists: {runtime_python}"
        )
    extraction_root = Path(
        tempfile.mkdtemp(prefix="hashi-python-extract-", dir=runtime_python.parent)
    )
    try:
        with tarfile.open(archive, mode="r:gz") as package:
            package.extractall(extraction_root, filter="data")
        extracted_python = extraction_root / "python"
        if not (extracted_python / "python.exe").is_file():
            raise RuntimeError("standalone Python archive is missing python/python.exe")
        shutil.copytree(extracted_python, runtime_python)
    finally:
        shutil.rmtree(extraction_root, ignore_errors=True)


def install_python_dependencies(runtime_python: Path) -> None:
    site_packages = runtime_python / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(site_packages),
            "--python-platform",
            "x86_64-pc-windows-msvc",
            "--python-version",
            "3.12",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "-r",
            str(HERE / "requirements.lock"),
        ]
    )
    for cache_dir in site_packages.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
    for compiled in site_packages.rglob("*.py[co]"):
        compiled.unlink()
    remove_path(site_packages / "bin")


def validate_bundled_runtime_contract(runtime_python: Path, app_hashi: Path) -> None:
    status("validate bundled Python and HASHI runtime contract")
    run(
        [
            str(runtime_python / "python.exe"),
            str(app_hashi / "scripts" / "check_runtime_contract.py"),
            "--code-root",
            str(app_hashi),
            "--json",
        ]
    )


def install_piper(
    runtime_python: Path, app_hashi: Path, licenses: Path, cache: Path
) -> None:
    archive = download(ASSETS["piper"], cache)
    status("install standalone Piper and Chinese voice")
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if member.is_dir() or not member.filename.startswith("piper/"):
                continue
            relative = Path(member.filename).relative_to("piper")
            target = runtime_python / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    model_dir = app_hashi / "voice_models" / "piper"
    model_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        download(ASSETS["piper_voice"], cache), model_dir / "zh_CN-huayan-medium.onnx"
    )
    shutil.copy2(
        download(ASSETS["piper_voice_config"], cache),
        model_dir / "zh_CN-huayan-medium.onnx.json",
    )
    shutil.copy2(download(ASSETS["piper_voice_card"], cache), model_dir / "MODEL_CARD")
    shutil.copy2(
        download(ASSETS["piper_license"], cache), licenses / "Piper-LICENSE.md"
    )


def extract_selected_zip_file(archive: Path, predicate, destination: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        members = [
            member
            for member in package.infolist()
            if not member.is_dir() and predicate(member.filename)
        ]
        if not members:
            raise RuntimeError(f"required payload is missing from {archive.name}")
        for member in members:
            target = destination / Path(member.filename).name
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def install_ffmpeg(runtime_bin: Path, licenses: Path, cache: Path) -> None:
    archive = download(ASSETS["ffmpeg"], cache)
    status("install FFmpeg executable")
    runtime_bin.mkdir(parents=True, exist_ok=True)
    extract_selected_zip_file(
        archive, lambda name: name.endswith("/bin/ffmpeg.exe"), runtime_bin
    )
    extract_selected_zip_file(
        archive,
        lambda name: name.endswith(("/LICENSE", "/README.txt")),
        licenses / "ffmpeg",
    )


def resolve_7zip(cache: Path, build_temp: Path) -> Path:
    """Return a full 7-Zip executable without requiring a Windows install."""

    for command in ("7z", "7zz"):
        executable = shutil.which(command)
        if executable:
            return Path(executable)

    if sys.platform != "win32":
        raise RuntimeError(
            "7-Zip is required to extract the pinned Tesseract installer; "
            "install 7z or 7zz on the build host"
        )

    installer = download(ASSETS["7zip"], cache)
    extract_root = build_temp / "7zip"
    extract_root.mkdir(parents=True, exist_ok=True)
    status("extract pinned build-only 7-Zip runtime")
    run(
        [
            "msiexec.exe",
            "/a",
            str(installer),
            "/qn",
            f"TARGETDIR={extract_root}",
        ]
    )
    executable = next(extract_root.rglob("7z.exe"), None)
    if executable is None:
        raise RuntimeError("pinned 7-Zip MSI did not contain 7z.exe")
    return executable


def install_tesseract(
    app_hashi: Path, licenses: Path, cache: Path, build_temp: Path
) -> None:
    installer = download(ASSETS["tesseract"], cache)
    extract_root = build_temp / "tesseract-extracted"
    extract_root.mkdir(parents=True, exist_ok=True)
    status("extract Tesseract Windows runtime")
    seven_zip = resolve_7zip(cache, build_temp)
    run([str(seven_zip), "x", "-y", str(installer), f"-o{extract_root}"])
    binary_root = app_hashi / "hashi_assets" / "ocr" / "bin" / "windows-x86_64"
    binary_root.mkdir(parents=True, exist_ok=True)
    tesseract_exe = next(extract_root.rglob("tesseract.exe"), None)
    if tesseract_exe is None:
        raise RuntimeError("tesseract.exe was not extracted")
    source_root = tesseract_exe.parent
    shutil.copy2(tesseract_exe, binary_root / "tesseract.exe")
    for dll in source_root.glob("*.dll"):
        shutil.copy2(dll, binary_root / dll.name)
    extracted_license = next(extract_root.rglob("LICENSE"), None)
    if extracted_license:
        shutil.copy2(extracted_license, licenses / "Tesseract-Windows-LICENSE")

    manifest_path = HASHI_ROOT / "hashi_assets" / "ocr" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    revision = str(manifest["revision"])
    model_root = app_hashi / "hashi_assets" / "ocr" / f"tessdata_fast-{revision}"
    model_root.mkdir(parents=True, exist_ok=True)
    source_url = (
        f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{revision}"
    )
    entries = [manifest["license_file"], *manifest["files"]]
    for entry in entries:
        name = str(entry.get("name") or Path(str(entry.get("path"))).name)
        asset = Asset(
            f"tessdata-{revision}-{name}",
            f"{source_url}/{name}",
            str(entry["sha256"]),
        )
        cached = download(asset, cache)
        shutil.copy2(cached, model_root / name)


def git_source_identity(root: Path) -> SourceIdentity:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%T", "HEAD"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not resolve HASHI source identity: {result.stderr.strip()}"
        )
    object_ids = result.stdout.splitlines()
    if len(object_ids) != 2:
        raise RuntimeError("Git returned an incomplete HASHI source identity")
    return SourceIdentity(revision=object_ids[0].strip(), tree=object_ids[1].strip())


def _expected_object_id(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40,64}", normalized) is None:
        raise RuntimeError(f"expected {label} must be a full Git object ID")
    return normalized


def require_expected_source_identity(
    root: Path,
    *,
    expected_revision: str | None = None,
    expected_tree: str | None = None,
) -> SourceIdentity:
    identity = git_source_identity(root)
    revision = _expected_object_id(expected_revision, label="revision")
    tree = _expected_object_id(expected_tree, label="tree")
    if revision is not None and identity.revision.lower() != revision:
        raise RuntimeError(
            f"HASHI source does not match expected revision: "
            f"actual={identity.revision}, expected={revision}"
        )
    if tree is not None and identity.tree.lower() != tree:
        raise RuntimeError(
            f"HASHI source does not match expected tree: "
            f"actual={identity.tree}, expected={tree}"
        )
    return identity


def require_unchanged_source_identity(
    root: Path, expected: SourceIdentity
) -> SourceIdentity:
    actual = git_source_identity(root)
    if actual != expected:
        raise RuntimeError(
            "HASHI source changed while the image was building: "
            f"revision={expected.revision}->{actual.revision}, "
            f"tree={expected.tree}->{actual.tree}"
        )
    return actual


def require_clean_tracked_worktree(root: Path, *, label: str) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise RuntimeError(f"could not inspect {label} worktree: {detail}")
    if result.stdout.strip():
        raise RuntimeError(
            f"{label} has uncommitted tracked changes; build from a clean worktree"
        )


def read_private_deepseek_key(path: Path | None) -> str | None:
    """Read the one credential allowed in a privately finalized image.

    The key is deliberately accepted only through a file.  Putting it directly
    on the command line would expose it in shell history and process listings.
    """

    if path is None:
        return None
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError(f"private DeepSeek key file is missing: {path}")
    if path.stat().st_size > 16 * 1024:
        raise RuntimeError("private DeepSeek key file is unexpectedly large")
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value:
        raise RuntimeError("private DeepSeek key file is empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise RuntimeError("private DeepSeek key file must contain exactly one key")
    return value


def configure_data(
    image_root: Path, *, private_deepseek_key: str | None
) -> None:
    data = image_root / "data"
    for relative in (
        "logs",
        "media",
        "state",
        "tmp",
        "workspaces/portable",
        "remote",
        "browser-profile",
    ):
        (data / relative).mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATES / "agents.json", data / "agents.json")
    # A public transfer image is not an installed HASHI instance.  Concrete
    # identity and lineage are generated on the destination PC, never at
    # public build time.  This makes a new-image first install and an update of
    # an existing lineage unambiguous.
    portable_identity = {
        "schema_version": 2,
        "product": "HASHI Portable Windows x64",
        "provisioning_state": "unprovisioned",
        "portable_instance_id": None,
        "identity_lineage_id": None,
        "created_at_utc": None,
    }
    (data / "portable-instance.json").write_text(
        json.dumps(portable_identity, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(TEMPLATES / "tasks.json", data / "tasks.json")
    shutil.copy2(
        TEMPLATES / "agent_capabilities.json", data / "agent_capabilities.json"
    )
    shutil.copy2(TEMPLATES / "api_gateway_state.json", data / "api_gateway_state.json")
    shutil.copy2(TEMPLATES / "agent.md", data / "workspaces" / "portable" / "agent.md")
    shutil.copy2(TEMPLATES / "remote-config.yaml", data / "remote" / "config.yaml")

    deepseek_key = str(private_deepseek_key or "").strip()
    private_finalized = bool(deepseek_key)
    portable_secrets = {
        "authorized_telegram_id": 0,
        "agent": "WORKBENCH_ONLY_NO_TOKEN",
        "deepseek_api_key": deepseek_key,
        # Public builds carry no credential.  First install provisions fresh
        # local tokens.  A private finalization also gets its own tokens and
        # never inherits a source instance's Remote credential.
        "workbench_admin_token": (
            secrets_module.token_urlsafe(32) if private_finalized else ""
        ),
        "hashi_remote_shared_token": (
            secrets_module.token_urlsafe(48) if private_finalized else ""
        ),
    }
    (data / "secrets.json").write_text(
        json.dumps(portable_secrets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def copy_launchers(image_root: Path) -> None:
    for name in (
        "Start_HASHI_TUI.bat",
        "Install_HASHI_On_This_PC.bat",
        "Update_HASHI_On_This_PC.bat",
        "Rollback_HASHI_On_This_PC.bat",
        "Uninstall_HASHI_From_This_PC.bat",
        "Stop_HASHI.bat",
        "Diagnose_HASHI.bat",
        "PORTABLE_README.txt",
    ):
        source = TEMPLATES / name
        destination = image_root / name
        if name == "PORTABLE_README.txt":
            destination.write_text(
                source.read_text(encoding="utf-8-sig"), encoding="utf-8-sig"
            )
        else:
            shutil.copy2(source, destination)
    launcher_root = image_root / "launcher"
    shutil.copytree(TEMPLATES / "launcher", launcher_root)
    # Windows PowerShell 5.1 interprets non-ASCII scripts using the active ANSI
    # code page unless a UTF-8 BOM is present.  The templates stay ordinary
    # UTF-8 in git, while every packaged PowerShell launcher is made reliably
    # bilingual here.
    for script in launcher_root.rglob("*.ps1"):
        source = script.read_text(encoding="utf-8-sig")
        script.write_text(source, encoding="utf-8-sig")


def tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def estimated_allocated_size(path: Path, cluster_bytes: int) -> int:
    """Conservative expanded size including one allocation unit per directory."""

    files = sum(
        ((item.stat().st_size + cluster_bytes - 1) // cluster_bytes) * cluster_bytes
        for item in path.rglob("*")
        if item.is_file()
    )
    directories = sum(1 for item in path.rglob("*") if item.is_dir()) + 1
    return files + directories * cluster_bytes


def write_manifest(image_root: Path, build_info: dict) -> int:
    info_path = image_root / "BUILD_INFO.json"
    info_path.write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    marker = image_root / ".hashi-portable-bundle"
    marker.write_text("HASHI Portable Windows x64\n", encoding="utf-8")
    manifest_path = image_root / "SHA256SUMS.txt"
    lines = []
    for path in sorted(
        item
        for item in image_root.rglob("*")
        if item.is_file() and item != manifest_path
    ):
        lines.append(f"{sha256_file(path)}  {path.relative_to(image_root).as_posix()}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tree_size(image_root)


def validate_image(image_root: Path) -> None:
    app_hashi = image_root / "app" / "hashi"
    validate_portable_runtime_inputs(app_hashi)
    portable_identity = json.loads(
        (image_root / "data" / "portable-instance.json").read_text(encoding="utf-8")
    )
    if (
        portable_identity.get("schema_version") != 2
        or portable_identity.get("product") != "HASHI Portable Windows x64"
        or portable_identity.get("provisioning_state") != "unprovisioned"
        or portable_identity.get("portable_instance_id") is not None
        or portable_identity.get("identity_lineage_id") is not None
        or portable_identity.get("created_at_utc") is not None
    ):
        raise RuntimeError("portable public provisioning template is invalid")
    portable_secrets = json.loads(
        (image_root / "data" / "secrets.json").read_text(encoding="utf-8")
    )
    permitted_secret_keys = {
        "authorized_telegram_id",
        "agent",
        "deepseek_api_key",
        "workbench_admin_token",
        "hashi_remote_shared_token",
    }
    unexpected_secret_keys = set(portable_secrets) - permitted_secret_keys
    if unexpected_secret_keys:
        raise RuntimeError(
            "portable image contains credentials outside private finalization: "
            + ", ".join(sorted(unexpected_secret_keys))
        )
    deepseek_key = str(portable_secrets.get("deepseek_api_key") or "")
    local_token = str(portable_secrets.get("workbench_admin_token") or "")
    remote_token = str(portable_secrets.get("hashi_remote_shared_token") or "")
    if deepseek_key:
        if len(local_token) < 32 or len(remote_token) < 48:
            raise RuntimeError("private finalization did not create independent tokens")
    elif local_token or remote_token:
        raise RuntimeError("public portable image contains generated credentials")
    config = json.loads(
        (image_root / "data" / "agents.json").read_text(encoding="utf-8")
    )
    [agent] = config["agents"]
    engines = [item["engine"] for item in agent["allowed_backends"]]
    if engines != ["her-v2"] or agent["active_backend"] != "her-v2":
        raise RuntimeError("portable Agent exposes a non-HER Engine")
    remote_config = (image_root / "data" / "remote" / "config.yaml").read_text(
        encoding="utf-8"
    )
    if (
        "lan_mode: false" not in remote_config
        or "pairing_auto_approve: true" not in remote_config
    ):
        raise RuntimeError("portable Remote must use token-required one-click pairing")
    if f"pairing_token_ttl_seconds: {PAIRING_TOKEN_TTL_SECONDS}" not in remote_config:
        raise RuntimeError("portable pairing TTL is not seven days")
    forbidden = (
        "runtime/node",
        "app/workbench",
        "runtime/python/Scripts/pip.exe",
        "app/hashi/adapters/codex_cli.py",
        "app/hashi/adapters/claude_cli.py",
        "app/hashi/adapters/gemini_cli.py",
        "app/hashi/adapters/grok_cli.py",
        "app/hashi/scripts/hashi_remote_watchdog.py",
    )
    present = [relative for relative in forbidden if (image_root / relative).exists()]
    if present:
        raise RuntimeError(f"forbidden portable components are present: {present}")
    required = (
        "runtime/python/python.exe",
        "runtime/bin/ffmpeg.exe",
        "data/portable-instance.json",
        "app/hashi/__main__.py",
        "app/hashi/main.py",
        "app/hashi/pyproject.toml",
        f"app/hashi/{_RUNTIME_POLICY.standard_lock}",
        "app/hashi/tui.py",
        "app/hashi/tui/assets/sounds/soft_chat_send.wav",
        "app/hashi/tui/assets/sounds/soft_chat_receive.wav",
        "app/hashi/exp/loader.py",
        "app/hashi/veritas/__init__.py",
        "app/hashi/voice_models/piper/zh_CN-huayan-medium.onnx",
        "app/hashi/hashi_assets/ocr/bin/windows-x86_64/tesseract.exe",
        "Install_HASHI_On_This_PC.bat",
        "Update_HASHI_On_This_PC.bat",
        "Rollback_HASHI_On_This_PC.bat",
        "Uninstall_HASHI_From_This_PC.bat",
        "launcher/Install-To-PC.ps1",
        "launcher/Bootstrap-Elevated.ps1",
        "launcher/Elevated-Entry.ps1",
        "launcher/Uninstall-From-PC.ps1",
        "launcher/Rollback-Previous.ps1",
        "launcher/Common.ps1",
        "launcher/Start-TUI.ps1",
        "launcher/Stop-HASHI.ps1",
    )
    missing = [
        relative for relative in required if not (image_root / relative).is_file()
    ]
    if missing:
        raise RuntimeError(f"portable image is incomplete: {missing}")
    obsolete = (
        "install/local-cache-manifest.json",
        "install/local-cache-small-files.zip",
        "launcher/Install-LocalCache.ps1",
        "launcher/Uninstall-LocalCache.ps1",
        "launcher/Compile-LocalCache.py",
    )
    present_obsolete = [
        relative for relative in obsolete if (image_root / relative).exists()
    ]
    if present_obsolete:
        raise RuntimeError(
            f"obsolete split-runtime installer files are present: {present_obsolete}"
        )

    common = (image_root / "launcher" / "Common.ps1").read_text(encoding="utf-8-sig")
    installer = (image_root / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8-sig"
    )
    if "C:\\HASHI-Portable" not in common or "C:\\HASHI-Portable" not in installer:
        raise RuntimeError("portable launchers do not target the required local root")
    if "HASHI Portable Local Endpoint" not in common:
        raise RuntimeError("portable launcher has no trusted local endpoint contract")
    if "HASHI_PORTABLE_STORAGE_PROFILE = 'removable'" in common:
        raise RuntimeError("local HASHI execution still enables removable storage mode")


def build(args: argparse.Namespace) -> Path:
    require_clean_tracked_worktree(HASHI_ROOT, label="HASHI source")
    source_identity = require_expected_source_identity(
        HASHI_ROOT,
        expected_revision=getattr(args, "expected_revision", None),
        expected_tree=getattr(args, "expected_tree", None),
    )
    validate_portable_dependency_generation()
    private_deepseek_key = read_private_deepseek_key(
        getattr(args, "private_deepseek_key_file", None)
    )
    output = args.output.resolve()
    if output.exists():
        marker = output / ".hashi-portable-bundle"
        if not args.overwrite:
            raise RuntimeError(
                f"output already exists: {output}; pass --overwrite to replace it"
            )
        if not marker.is_file():
            raise RuntimeError(f"refusing to replace unmarked directory: {output}")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent)
    )
    build_temp = Path(tempfile.mkdtemp(prefix="hashi-portable-build-"))
    try:
        app_hashi = staging / "app" / "hashi"
        runtime_python = staging / "runtime" / "python"
        runtime_bin = staging / "runtime" / "bin"
        licenses = staging / "THIRD_PARTY_LICENSES"
        licenses.mkdir(parents=True, exist_ok=True)

        copy_hashi_source(app_hashi)
        validate_portable_runtime_inputs(app_hashi)
        extract_python(runtime_python, args.cache)
        install_python_dependencies(runtime_python)
        validate_bundled_runtime_contract(runtime_python, app_hashi)
        install_piper(runtime_python, app_hashi, licenses, args.cache)
        install_ffmpeg(runtime_bin, licenses, args.cache)
        install_tesseract(app_hashi, licenses, args.cache, build_temp)
        configure_data(staging, private_deepseek_key=private_deepseek_key)
        copy_launchers(staging)
        validate_image(staging)

        categories = {
            name: tree_size(staging / name)
            for name in (
                "app",
                "runtime",
                "data",
                "THIRD_PARTY_LICENSES",
            )
        }
        build_info = {
            "schema_version": 1,
            "product": "HASHI Portable Windows x64",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "hashi_revision": source_identity.revision,
            "hashi_tree": source_identity.tree,
            "python_version": PYTHON_VERSION,
            "pairing_token_ttl_seconds": PAIRING_TOKEN_TTL_SECONDS,
            "maximum_image_bytes": MAX_IMAGE_BYTES,
            "target_image_bytes": TARGET_IMAGE_BYTES,
            "provisioning": (
                "private-deepseek" if private_deepseek_key else "public"
            ),
            "category_bytes_before_manifest": categories,
            "features": {
                "engine": "her-v2",
                "default_provider": "official-deepseek",
                "configurable_qwen": True,
                "tui_backend_api_session": True,
                "full_host_filesystem_access": True,
                "remote_lan_discovery": True,
                "remote_one_click_pairing": True,
                "api_gateway_service": False,
                "local_llm": False,
                "electron_or_bundled_browser": False,
                "semantic_vector_memory": False,
                "tesseract_ocr": True,
                "piper_chinese_tts": True,
                "ffmpeg": True,
                "playwright_without_browser": True,
                "administrator_local_execution": True,
                "complete_local_copy": True,
                "usb_execution": False,
                "fixed_loopback_dynamic_port": True,
                "identity_bound_local_endpoint": True,
                "safe_owned_local_uninstall": True,
                "desktop_shortcuts": True,
                "verified_shutdown_quiescence": True,
                "git_tracked_source_only": True,
                "clean_tracked_inputs_required": True,
                "local_backend_api_observability": True,
                "local_remote_route_cache": True,
                "soft_chat_message_sounds": True,
                "windows_native_only": True,
            },
        }
        final_size = 0
        for _attempt in range(5):
            measured = write_manifest(staging, build_info)
            if build_info.get("final_image_bytes") == measured:
                final_size = measured
                break
            build_info["final_image_bytes"] = measured
        else:
            raise RuntimeError("portable logical size did not stabilize")
        allocated_size = estimated_allocated_size(staging, CAPACITY_CHECK_CLUSTER_BYTES)
        build_info["estimated_allocated_bytes_32k"] = allocated_size
        build_info["within_target"] = allocated_size <= TARGET_IMAGE_BYTES
        for _attempt in range(5):
            measured = write_manifest(staging, build_info)
            allocated_size = estimated_allocated_size(
                staging, CAPACITY_CHECK_CLUSTER_BYTES
            )
            if (
                build_info.get("final_image_bytes") == measured
                and build_info.get("estimated_allocated_bytes_32k") == allocated_size
            ):
                final_size = measured
                break
            build_info["final_image_bytes"] = measured
            build_info["estimated_allocated_bytes_32k"] = allocated_size
            build_info["within_target"] = allocated_size <= TARGET_IMAGE_BYTES
        else:
            raise RuntimeError("portable allocated size did not stabilize")
        if allocated_size > MAX_IMAGE_BYTES:
            raise RuntimeError(
                "portable image requires an estimated "
                f"{allocated_size:,} bytes with 32 KiB clusters, exceeding hard limit "
                f"{MAX_IMAGE_BYTES:,}"
            )
        require_clean_tracked_worktree(HASHI_ROOT, label="HASHI source")
        require_unchanged_source_identity(HASHI_ROOT, source_identity)
        staging.replace(output)
        status(
            f"complete: {output} ({final_size:,} logical bytes; "
            f"{allocated_size:,} estimated bytes with 32 KiB clusters)"
        )
        return output
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(build_temp, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=HASHI_ROOT / "dist" / "HASHI-Portable-Windows-x64",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=HASHI_ROOT / "build" / "portable-cache",
    )
    parser.add_argument(
        "--private-deepseek-key-file",
        type=Path,
        help=(
            "privately finalize the image with the DeepSeek key read from this "
            "file; public builds omit all credentials"
        ),
    )
    parser.add_argument(
        "--expected-revision",
        help="require this full HASHI source commit ID before building",
    )
    parser.add_argument(
        "--expected-tree",
        help="require this full HASHI source tree ID before building",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        build(parse_args(argv))
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"[portable] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
