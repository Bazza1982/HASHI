from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_harness import (
    build_extension_bundle,
    create_harness_layout,
    rewrite_manifest_name,
    rewrite_service_worker_host_name,
    write_native_host_manifest,
)


def test_rewrite_service_worker_host_name() -> None:
    content = 'const HOST_NAME = "com.hashi.browser_bridge";\nconsole.log("ok");\n'
    rewritten = rewrite_service_worker_host_name(content, "com.hashi.browser_bridge.test")
    assert 'const HOST_NAME = "com.hashi.browser_bridge.test";' in rewritten
    assert 'com.hashi.browser_bridge";' not in rewritten


def test_rewrite_manifest_name() -> None:
    content = '{\n  "name": "HASHI Browser Bridge",\n  "version": "0.1.0"\n}\n'
    rewritten = rewrite_manifest_name(content, "HASHI Browser Bridge Test")
    assert '"name": "HASHI Browser Bridge Test"' in rewritten


def test_create_harness_layout(tmp_path: Path) -> None:
    layout = create_harness_layout(tmp_path / "harness")
    assert Path(layout["extension_dir"]).is_dir()
    assert Path(layout["native_host_dir"]).is_dir()
    assert Path(layout["logs_dir"]).is_dir()
    assert Path(layout["state_dir"]).is_dir()


def test_build_extension_bundle(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "out"
    source.mkdir()
    (source / "manifest.json").write_text(
        '{\n  "name": "HASHI Browser Bridge",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (source / "service_worker.js").write_text(
        'const HOST_NAME = "com.hashi.browser_bridge";\n',
        encoding="utf-8",
    )

    build_extension_bundle(
        source,
        target,
        host_name="com.hashi.browser_bridge.test",
        extension_name="HASHI Browser Bridge Test",
    )

    assert '"name": "HASHI Browser Bridge Test"' in (target / "manifest.json").read_text(encoding="utf-8")
    assert 'const HOST_NAME = "com.hashi.browser_bridge.test";' in (
        target / "service_worker.js"
    ).read_text(encoding="utf-8")


def test_write_native_host_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "native_host" / "com.hashi.browser_bridge.test.json"
    manifest = write_native_host_manifest(
        manifest_path,
        host_name="com.hashi.browser_bridge.test",
        host_command_path="C:\\test\\host.cmd",
        allowed_origins=["chrome-extension://testid/"],
    )
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved == manifest
    assert saved["allowed_origins"] == ["chrome-extension://testid/"]
