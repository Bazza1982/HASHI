#!/usr/bin/env python3
"""Build the allowlisted HASHI Portable Windows x64 directory image."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets as secrets_module
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_IMAGE_BYTES = 957_000_000
TARGET_IMAGE_BYTES = 820 * 1024 * 1024
CAPACITY_CHECK_CLUSTER_BYTES = 32 * 1024
PYTHON_VERSION = "3.12.10"
NODE_VERSION = "22.23.2"
PAIRING_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
LOCAL_CACHE_ARCHIVE_MAX_FILE_BYTES = 512 * 1024
LOCAL_CACHE_INSTALL_RESERVE_BYTES = 512 * 1024 * 1024
LOCAL_CACHE_MANIFEST = "local-cache-manifest.json"
LOCAL_CACHE_PAYLOAD = "local-cache-small-files.zip"
LOCAL_CACHE_REQUIRED_FILES = (
    "runtime/python/python.exe",
    "runtime/node/node.exe",
    "runtime/bin/ffmpeg.exe",
    "app/hashi/main.py",
    "app/hashi/tui.py",
    "app/workbench/server.mjs",
    "app/workbench/ui/index.html",
)

HERE = Path(__file__).resolve().parent
HASHI_ROOT = HERE.parents[1]
TEMPLATES = HERE / "templates"


@dataclass(frozen=True)
class Asset:
    filename: str
    url: str
    sha256: str


ASSETS = {
    "python": Asset(
        "python-3.12.10-embed-amd64.zip",
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
        "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
    ),
    "node": Asset(
        "node-v22.23.2-win-x64.zip",
        "https://nodejs.org/dist/v22.23.2/node-v22.23.2-win-x64.zip",
        "1177b4137ba5adaa56354ae40f1080c7450e8ae09cecb47da459d1c52ac99f97",
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
ROOT_SOURCE_FILES = ("main.py", "tui.py", "LICENSE")
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
    "skills/memory-consolidation",
    "scripts/backfill_codex_tokens.py",
    "scripts/consolidate_memory.py",
    "scripts/link_whatsapp.py",
    "scripts/memory_to_obsidian.py",
    "scripts/query_memory.py",
    "scripts/remote_memory_consolidation.py",
    "scripts/send_whatsapp_test.py",
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
    "veritas/test_adapters.py",
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
    requested = (*SOURCE_DIRS, *ROOT_SOURCE_FILES, *ROOT_PACKAGE_FILES)
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
            raise RuntimeError(f"Git-tracked source is missing or not a file: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def extract_python(runtime_python: Path, cache: Path) -> None:
    archive = download(ASSETS["python"], cache)
    status(f"extract Python {PYTHON_VERSION}")
    runtime_python.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(runtime_python)
    pth = next(runtime_python.glob("python3*._pth"), None)
    if pth is None:
        raise RuntimeError("embedded Python _pth file is missing")
    pth.write_text(
        "python312.zip\n.\nLib\\site-packages\n..\\..\\app\\hashi\nimport site\n",
        encoding="utf-8",
    )


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


def install_node(runtime_node: Path, licenses: Path, cache: Path) -> None:
    archive = download(ASSETS["node"], cache)
    status(f"install Node {NODE_VERSION} runtime without npm/corepack")
    runtime_node.mkdir(parents=True, exist_ok=True)
    extract_selected_zip_file(
        archive, lambda name: name.endswith("/node.exe"), runtime_node
    )
    extract_selected_zip_file(
        archive, lambda name: name.endswith("/LICENSE"), licenses / "node"
    )


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


def install_tesseract(
    app_hashi: Path, licenses: Path, cache: Path, build_temp: Path
) -> None:
    installer = download(ASSETS["tesseract"], cache)
    extract_root = build_temp / "tesseract-extracted"
    extract_root.mkdir(parents=True, exist_ok=True)
    status("extract Tesseract Windows runtime")
    run(["7z", "x", "-y", str(installer), f"-o{extract_root}"])
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


def copy_workbench_licenses(workbench_root: Path, licenses: Path) -> None:
    destination = licenses / "workbench"
    destination.mkdir(parents=True, exist_ok=True)
    for name in (
        "LICENSE",
        "LICENSE_SCOPE.md",
        "NOTICE_WORKBENCH.md",
        "THIRD_PARTY_NOTICES.md",
    ):
        source = workbench_root / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    for name in ("LICENSE", "NOTICE.md"):
        source = workbench_root / "kasumi" / name
        if source.is_file():
            shutil.copy2(source, destination / f"kasumi-{name}")


def build_workbench(
    workbench_root: Path, destination: Path, build_temp: Path, *, skip_build: bool
) -> str:
    if not skip_build:
        run(["npm", "run", "build"], cwd=workbench_root)
    if not (workbench_root / "dist" / "index.html").is_file():
        raise RuntimeError("Workbench dist/index.html is missing")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        workbench_root / "dist",
        destination / "ui",
        ignore=shutil.ignore_patterns("kasumi-app"),
    )
    kasumi_source = workbench_root / "public" / "kasumi-app"
    if kasumi_source.is_dir():
        shutil.copytree(kasumi_source, destination / "kasumi-app")
    else:
        shutil.copytree(
            workbench_root / "dist" / "kasumi-app", destination / "kasumi-app"
        )

    esbuild = workbench_root / "node_modules" / "esbuild" / "bin" / "esbuild"
    if not esbuild.is_file():
        raise RuntimeError(
            "Workbench esbuild dependency is missing; run npm install in the Workbench repository"
        )
    run(
        [
            str(esbuild),
            str(workbench_root / "server" / "index.js"),
            "--bundle",
            "--platform=node",
            "--format=esm",
            "--target=node22",
            "--external:sharp",
            "--banner:js=import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);",
            f"--outfile={destination / 'server.mjs'}",
        ],
        cwd=workbench_root,
    )

    sharp_stage = build_temp / "sharp-runtime"
    sharp_stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "sharp_runtime" / "package.json", sharp_stage / "package.json")
    shutil.copy2(
        HERE / "sharp_runtime" / "package-lock.json", sharp_stage / "package-lock.json"
    )
    run(
        [
            "npm",
            "ci",
            "--ignore-scripts",
            "--include=optional",
            "--no-audit",
            "--no-fund",
            "--os=win32",
            "--cpu=x64",
        ],
        cwd=sharp_stage,
    )
    node_modules = sharp_stage / "node_modules"
    for relative in ("@emnapi", "@img/sharp-wasm32", "tslib", ".package-lock.json"):
        remove_path(node_modules / relative)
    shutil.copytree(node_modules, destination / "node_modules")
    return git_revision(workbench_root)


def git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


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


def configure_data(
    image_root: Path, source_secrets: Path, *, allow_missing_key: bool
) -> None:
    data = image_root / "data"
    for relative in (
        "logs",
        "media",
        "state",
        "tmp",
        "workbench",
        "workspaces/portable",
        "remote",
        "browser-profile",
    ):
        (data / relative).mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATES / "agents.json", data / "agents.json")
    portable_identity = {
        "schema_version": 1,
        "product": "HASHI Portable Windows x64",
        "portable_instance_id": secrets_module.token_hex(16),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
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

    source = (
        json.loads(source_secrets.read_text(encoding="utf-8-sig"))
        if source_secrets.is_file()
        else {}
    )
    deepseek_key = str(source.get("deepseek_api_key") or "").strip()
    if not deepseek_key and not allow_missing_key:
        raise RuntimeError("deepseek_api_key is missing from the source secrets file")
    remote_token = str(source.get("hashi_remote_shared_token") or "").strip()
    portable_secrets = {
        "authorized_telegram_id": 0,
        "portable": "WORKBENCH_ONLY_NO_TOKEN",
        "deepseek_api_key": deepseek_key,
        "workbench_admin_token": secrets_module.token_urlsafe(32),
        "hashi_remote_shared_token": remote_token or secrets_module.token_urlsafe(48),
    }
    for optional in ("dashscope_api_key", "openrouter_key"):
        value = str(source.get(optional) or "").strip()
        if value:
            portable_secrets[optional] = value
    (data / "secrets.json").write_text(
        json.dumps(portable_secrets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def copy_launchers(image_root: Path) -> None:
    for name in (
        "Start_HASHI_TUI.bat",
        "Start_HASHI_Workbench.bat",
        "Install_HASHI_On_This_PC.bat",
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


def create_local_cache_payload(image_root: Path) -> dict:
    """Create a compact installer payload while retaining expanded USB fallback.

    Thousands of small Python and Workbench files are expensive to read from a
    low-end flash drive.  They are duplicated into one ZIP for sequential
    installation reads.  Large files remain direct-copy inputs so the payload
    costs little additional USB capacity.
    """

    install_dir = image_root / "install"
    remove_path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    archive_path = install_dir / LOCAL_CACHE_PAYLOAD
    records: list[dict] = []
    bundle_digest = hashlib.sha256()
    archive_bytes = 0
    archive_files = 0
    direct_bytes = 0
    direct_files = 0

    source_files = sorted(
        path
        for root_name in ("app", "runtime")
        for path in (image_root / root_name).rglob("*")
        if path.is_file()
    )
    status(
        "create local acceleration payload from "
        f"{len(source_files):,} app/runtime files"
    )
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
        strict_timestamps=False,
    ) as archive:
        for path in source_files:
            relative = path.relative_to(image_root).as_posix()
            size = path.stat().st_size
            digest = sha256_file(path)
            delivery = (
                "archive" if size < LOCAL_CACHE_ARCHIVE_MAX_FILE_BYTES else "direct"
            )
            record = {
                "path": relative,
                "size": size,
                "sha256": digest,
                "delivery": delivery,
            }
            records.append(record)
            bundle_digest.update(f"{relative}\0{size}\0{digest}\n".encode("utf-8"))
            if delivery == "archive":
                archive.write(path, arcname=relative)
                archive_bytes += size
                archive_files += 1
            else:
                direct_bytes += size
                direct_files += 1

    bundle_id = bundle_digest.hexdigest()
    manifest = {
        "schema_version": 1,
        "product": "HASHI Portable Local Acceleration Cache",
        "bundle_id": bundle_id,
        "cache_key": bundle_id[:20],
        "install_scope": "machine",
        "administrator_required": True,
        "authoritative_data": "usb:data",
        "expanded_usb_fallback": True,
        "install_bytes": archive_bytes + direct_bytes,
        "minimum_free_bytes": (
            archive_bytes + direct_bytes + LOCAL_CACHE_INSTALL_RESERVE_BYTES
        ),
        "archive": {
            "path": f"install/{LOCAL_CACHE_PAYLOAD}",
            "sha256": sha256_file(archive_path),
            "compressed_bytes": archive_path.stat().st_size,
            "uncompressed_bytes": archive_bytes,
            "file_count": archive_files,
            "maximum_source_file_bytes": LOCAL_CACHE_ARCHIVE_MAX_FILE_BYTES,
        },
        "direct": {
            "bytes": direct_bytes,
            "file_count": direct_files,
        },
        "bytecode_roots": [
            "app/hashi",
            "runtime/python/Lib/site-packages",
        ],
        "required_files": list(LOCAL_CACHE_REQUIRED_FILES),
        "files": records,
    }
    (install_dir / LOCAL_CACHE_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    status(
        "local acceleration payload: "
        f"{archive_files:,} small files -> {archive_path.stat().st_size:,} bytes; "
        f"{direct_files:,} large files stay direct-copy"
    )
    return manifest


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
    portable_identity = json.loads(
        (image_root / "data" / "portable-instance.json").read_text(encoding="utf-8")
    )
    if (
        portable_identity.get("schema_version") != 1
        or portable_identity.get("product") != "HASHI Portable Windows x64"
        or not isinstance(portable_identity.get("portable_instance_id"), str)
        or len(portable_identity["portable_instance_id"]) != 32
        or any(
            character not in "0123456789abcdef"
            for character in portable_identity["portable_instance_id"]
        )
    ):
        raise RuntimeError("portable instance identity is invalid")
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
        "runtime/node/npm.cmd",
        "runtime/node/npx.cmd",
        "runtime/python/Scripts/pip.exe",
        "app/hashi/adapters/codex_cli.py",
        "app/hashi/adapters/claude_cli.py",
        "app/hashi/adapters/gemini_cli.py",
        "app/hashi/adapters/grok_cli.py",
    )
    present = [relative for relative in forbidden if (image_root / relative).exists()]
    if present:
        raise RuntimeError(f"forbidden portable components are present: {present}")
    required = (
        "runtime/python/python.exe",
        "runtime/node/node.exe",
        "runtime/bin/ffmpeg.exe",
        "data/portable-instance.json",
        "app/hashi/main.py",
        "app/hashi/tui.py",
        "app/hashi/exp/loader.py",
        "app/hashi/veritas/__init__.py",
        "app/workbench/server.mjs",
        "app/workbench/ui/index.html",
        "app/hashi/voice_models/piper/zh_CN-huayan-medium.onnx",
        "app/hashi/hashi_assets/ocr/bin/windows-x86_64/tesseract.exe",
        f"install/{LOCAL_CACHE_MANIFEST}",
        f"install/{LOCAL_CACHE_PAYLOAD}",
        "Install_HASHI_On_This_PC.bat",
        "Uninstall_HASHI_From_This_PC.bat",
        "launcher/Install-LocalCache.ps1",
        "launcher/Uninstall-LocalCache.ps1",
        "launcher/Compile-LocalCache.py",
    )
    missing = [
        relative for relative in required if not (image_root / relative).is_file()
    ]
    if missing:
        raise RuntimeError(f"portable image is incomplete: {missing}")

    cache_manifest = json.loads(
        (image_root / "install" / LOCAL_CACHE_MANIFEST).read_text(encoding="utf-8")
    )
    if cache_manifest.get("install_scope") != "machine":
        raise RuntimeError("portable local acceleration cache must be machine scoped")
    if not cache_manifest.get("administrator_required"):
        raise RuntimeError(
            "portable local acceleration install must require administrator"
        )
    if not cache_manifest.get("expanded_usb_fallback"):
        raise RuntimeError(
            "portable local acceleration must retain expanded USB fallback"
        )
    if cache_manifest.get("authoritative_data") != "usb:data":
        raise RuntimeError("portable local cache must not own authoritative user data")
    records = cache_manifest.get("files", [])
    delivery = {record.get("delivery") for record in records}
    if delivery != {"archive", "direct"}:
        raise RuntimeError(
            "portable local cache must contain archive and direct-copy inputs"
        )
    record_paths = [str(record.get("path") or "") for record in records]
    if len(record_paths) != len(set(record_paths)):
        raise RuntimeError("portable local cache manifest contains duplicate paths")
    actual_paths = {
        path.relative_to(image_root).as_posix()
        for root_name in ("app", "runtime")
        for path in (image_root / root_name).rglob("*")
        if path.is_file()
    }
    if set(record_paths) != actual_paths:
        raise RuntimeError(
            "portable local cache manifest does not cover app/runtime exactly"
        )
    digest = hashlib.sha256()
    archive_records = []
    direct_records = []
    for record in records:
        relative = str(record["path"])
        source = image_root / relative
        size = source.stat().st_size
        actual_hash = sha256_file(source)
        if size != record.get("size") or actual_hash != record.get("sha256"):
            raise RuntimeError(
                f"portable local cache record is inconsistent: {relative}"
            )
        digest.update(f"{relative}\0{size}\0{actual_hash}\n".encode("utf-8"))
        (archive_records if record["delivery"] == "archive" else direct_records).append(
            record
        )
    if digest.hexdigest() != cache_manifest.get("bundle_id"):
        raise RuntimeError("portable local cache bundle identity is inconsistent")
    if cache_manifest.get("cache_key") != digest.hexdigest()[:20]:
        raise RuntimeError("portable local cache key is inconsistent")
    archive = cache_manifest.get("archive") or {}
    archive_path = image_root / str(archive.get("path") or "")
    if sha256_file(archive_path) != archive.get("sha256"):
        raise RuntimeError("portable local cache archive hash is inconsistent")
    with zipfile.ZipFile(archive_path) as package:
        if package.testzip() is not None:
            raise RuntimeError("portable local cache archive failed its CRC check")
        archive_names = {
            item.filename for item in package.infolist() if not item.is_dir()
        }
    if archive_names != {str(record["path"]) for record in archive_records}:
        raise RuntimeError("portable local cache archive contents are inconsistent")
    if archive.get("file_count") != len(archive_records):
        raise RuntimeError("portable local cache archive file count is inconsistent")
    if archive.get("uncompressed_bytes") != sum(
        int(record["size"]) for record in archive_records
    ):
        raise RuntimeError("portable local cache archive byte count is inconsistent")
    direct = cache_manifest.get("direct") or {}
    if direct.get("file_count") != len(direct_records):
        raise RuntimeError("portable local cache direct-copy count is inconsistent")
    if direct.get("bytes") != sum(int(record["size"]) for record in direct_records):
        raise RuntimeError("portable local cache direct-copy bytes are inconsistent")
    if cache_manifest.get("install_bytes") != sum(
        int(record["size"]) for record in records
    ):
        raise RuntimeError("portable local cache install size is inconsistent")
    if not set(LOCAL_CACHE_REQUIRED_FILES).issubset(actual_paths):
        raise RuntimeError("portable local cache required-file list is inconsistent")


def build(args: argparse.Namespace) -> Path:
    workbench_root = args.workbench_root.resolve()
    require_clean_tracked_worktree(HASHI_ROOT, label="HASHI source")
    require_clean_tracked_worktree(workbench_root, label="Workbench source")
    hashi_revision = git_revision(HASHI_ROOT)
    expected_workbench_revision = git_revision(workbench_root)
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
        app_workbench = staging / "app" / "workbench"
        runtime_python = staging / "runtime" / "python"
        runtime_node = staging / "runtime" / "node"
        runtime_bin = staging / "runtime" / "bin"
        licenses = staging / "THIRD_PARTY_LICENSES"
        licenses.mkdir(parents=True, exist_ok=True)

        copy_hashi_source(app_hashi)
        extract_python(runtime_python, args.cache)
        install_python_dependencies(runtime_python)
        install_piper(runtime_python, app_hashi, licenses, args.cache)
        install_node(runtime_node, licenses, args.cache)
        install_ffmpeg(runtime_bin, licenses, args.cache)
        install_tesseract(app_hashi, licenses, args.cache, build_temp)
        workbench_revision = build_workbench(
            workbench_root,
            app_workbench,
            build_temp,
            skip_build=args.skip_workbench_build,
        )
        copy_workbench_licenses(workbench_root, licenses)
        configure_data(
            staging,
            args.secrets.resolve(),
            allow_missing_key=args.allow_missing_deepseek_key,
        )
        copy_launchers(staging)
        local_cache_manifest = create_local_cache_payload(staging)
        validate_image(staging)

        categories = {
            name: tree_size(staging / name)
            for name in (
                "app",
                "runtime",
                "install",
                "data",
                "THIRD_PARTY_LICENSES",
            )
        }
        build_info = {
            "schema_version": 1,
            "product": "HASHI Portable Windows x64",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "hashi_revision": hashi_revision,
            "workbench_revision": workbench_revision,
            "python_version": PYTHON_VERSION,
            "node_version": NODE_VERSION,
            "pairing_token_ttl_seconds": PAIRING_TOKEN_TTL_SECONDS,
            "maximum_image_bytes": MAX_IMAGE_BYTES,
            "target_image_bytes": TARGET_IMAGE_BYTES,
            "category_bytes_before_manifest": categories,
            "features": {
                "engine": "her-v2",
                "default_provider": "official-deepseek",
                "configurable_qwen": True,
                "shared_tui_workbench_conversation": True,
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
                "administrator_local_acceleration": True,
                "expanded_usb_fallback": True,
                "instance_scoped_host_uninstall": True,
                "verified_legacy_cache_cleanup": True,
                "verified_shutdown_quiescence": True,
                "git_tracked_source_only": True,
                "clean_tracked_inputs_required": True,
            },
            "local_cache_bundle_id": local_cache_manifest["bundle_id"],
            "local_cache_install_bytes": local_cache_manifest["install_bytes"],
            "local_cache_payload_bytes": local_cache_manifest["archive"][
                "compressed_bytes"
            ],
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
        require_clean_tracked_worktree(workbench_root, label="Workbench source")
        if git_revision(HASHI_ROOT) != hashi_revision:
            raise RuntimeError("HASHI revision changed while the image was building")
        if (
            workbench_revision != expected_workbench_revision
            or git_revision(workbench_root) != expected_workbench_revision
        ):
            raise RuntimeError("Workbench revision changed while the image was building")
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
        "--workbench-root",
        type=Path,
        default=HASHI_ROOT.parent / "hashi-workbench-v2",
    )
    parser.add_argument("--secrets", type=Path, default=HASHI_ROOT / "secrets.json")
    parser.add_argument("--skip-workbench-build", action="store_true")
    parser.add_argument("--allow-missing-deepseek-key", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        build(parse_args(argv))
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"[portable] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
