from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from orchestrator.her_rebuild import (
    BuildArtifact,
    CandidateStore,
    DevelopmentSelectionStore,
    FailureKind,
    GitSourceState,
    HERBuildController,
    HERRebuildError,
    HERSourceLayout,
    SourceFingerprint,
    ToolchainIdentity,
    build_cargo_environment,
    cargo_build_argv,
    compute_source_fingerprint,
    detect_host_target,
    inspect_toolchain,
    preflight_source,
    redact_diagnostics,
    sha256_file,
)


def _source_layout(tmp_path: Path) -> HERSourceLayout:
    code_root = tmp_path / "hashi"
    source_root = code_root / "native" / "her"
    rust_root = source_root / "rust"
    crate_root = rust_root / "crates" / "rusty-claude-cli"
    (crate_root / "src").mkdir(parents=True)
    (source_root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (source_root / "UPSTREAM_SOURCE.json").write_text(
        json.dumps({"repository": "https://example.invalid/her", "commit": "abc123"}),
        encoding="utf-8",
    )
    (rust_root / "Cargo.toml").write_text(
        """[workspace]
members = ["crates/*"]
resolver = "2"

[profile.hashi-dev]
inherits = "release"
incremental = true
opt-level = 1
""",
        encoding="utf-8",
    )
    (rust_root / "Cargo.lock").write_text("# locked\n", encoding="utf-8")
    (crate_root / "Cargo.toml").write_text(
        """[package]
name = "rusty-claude-cli"
version = "0.1.0"

[[bin]]
name = "claw"
path = "src/main.rs"
""",
        encoding="utf-8",
    )
    (crate_root / "src" / "main.rs").write_text(
        'fn main() { println!("HER"); }\n',
        encoding="utf-8",
    )
    return HERSourceLayout.from_code_root(code_root)


def _toolchain(cargo_path: str = "/toolchain/cargo") -> ToolchainIdentity:
    return ToolchainIdentity(
        cargo_path=cargo_path,
        cargo_version="cargo 1.90.0",
        rustc_path="/toolchain/rustc",
        rustc_version="rustc 1.90.0",
    )


def _fingerprint(
    layout: HERSourceLayout,
    *,
    target: str = "x86_64-unknown-linux-gnu",
) -> SourceFingerprint:
    return compute_source_fingerprint(
        layout,
        toolchain=_toolchain(),
        target=target,
        git_state=GitSourceState(head="a" * 40, dirty=False),
    )


def test_detect_host_target_supports_linux_and_windows_x64() -> None:
    assert detect_host_target(system="Linux", machine="x86_64") == "x86_64-unknown-linux-gnu"
    assert detect_host_target(system="Windows", machine="AMD64") == "x86_64-pc-windows-msvc"


def test_detect_host_target_rejects_unsupported_host() -> None:
    with pytest.raises(HERRebuildError) as caught:
        detect_host_target(system="Darwin", machine="arm64")
    assert caught.value.failure_kind == FailureKind.UNSUPPORTED_PLATFORM


def test_preflight_requires_integrated_source(tmp_path: Path) -> None:
    layout = HERSourceLayout.from_code_root(tmp_path / "hashi")
    with pytest.raises(HERRebuildError) as caught:
        preflight_source(layout)
    assert caught.value.failure_kind == FailureKind.SOURCE_MISSING


def test_preflight_requires_dedicated_profile(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    layout.cargo_manifest.write_text("[workspace]\nmembers = []\n", encoding="utf-8")
    with pytest.raises(HERRebuildError, match="hashi-dev") as caught:
        preflight_source(layout)
    assert caught.value.failure_kind == FailureKind.SOURCE_INVALID


def test_preflight_rejects_integrated_source_symlink(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    outside = tmp_path / "outside.rs"
    outside.write_text("private external source\n", encoding="utf-8")
    link = layout.rust_root / "crates" / "rusty-claude-cli" / "src" / "linked.rs"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this test host")
    with pytest.raises(HERRebuildError, match="symbolic link") as caught:
        preflight_source(layout)
    assert caught.value.failure_kind == FailureKind.SOURCE_INVALID


def test_fingerprint_is_deterministic_and_tracks_relevant_source(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    first = _fingerprint(layout)
    second = _fingerprint(layout)
    assert first == second
    assert first.file_count >= 6

    main_rs = layout.rust_root / "crates" / "rusty-claude-cli" / "src" / "main.rs"
    main_rs.write_text('fn main() { println!("changed"); }\n', encoding="utf-8")
    changed = _fingerprint(layout)
    assert changed.digest != first.digest


def test_fingerprint_excludes_cargo_target_and_temp_files(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    first = _fingerprint(layout)
    (layout.rust_root / "target" / "debug").mkdir(parents=True)
    (layout.rust_root / "target" / "debug" / "claw").write_bytes(b"binary")
    (layout.rust_root / ".tmp-editor").write_text("scratch", encoding="utf-8")
    assert _fingerprint(layout).digest == first.digest


def test_fingerprint_includes_target_profile_features_and_toolchain(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    base = _fingerprint(layout)
    windows = _fingerprint(layout, target="x86_64-pc-windows-msvc")
    assert windows.digest != base.digest

    featured = HERSourceLayout.from_code_root(
        tmp_path / "hashi",
        features=("stream-json",),
    )
    assert _fingerprint(featured).digest != base.digest

    newer = compute_source_fingerprint(
        layout,
        toolchain=ToolchainIdentity(
            cargo_path="cargo",
            cargo_version="cargo 1.91.0",
            rustc_path="rustc",
            rustc_version="rustc 1.91.0",
        ),
        target=base.target,
        git_state=GitSourceState(head="a" * 40, dirty=False),
    )
    assert newer.digest != base.digest


def test_build_environment_is_allowlisted_and_excludes_secrets(tmp_path: Path) -> None:
    environment = build_cargo_environment(
        {
            "Path": "/usr/bin",
            "HOME": "/home/tester",
            "OPENAI_API_KEY": "private",
            "TELEGRAM_TOKEN": "private",
            "RUSTFLAGS": "--malicious",
            "UNRELATED": "value",
        },
        cargo_target_dir=tmp_path / "target",
    )
    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == "/home/tester"
    assert environment["CARGO_INCREMENTAL"] == "1"
    assert environment["CARGO_TARGET_DIR"] == str((tmp_path / "target").resolve())
    assert "OPENAI_API_KEY" not in environment
    assert "TELEGRAM_TOKEN" not in environment
    assert "RUSTFLAGS" not in environment
    assert "UNRELATED" not in environment


def test_cargo_argv_is_argument_array_and_locked(tmp_path: Path) -> None:
    layout = HERSourceLayout.from_code_root(
        tmp_path / "hashi",
        features=("zeta", "alpha"),
    )
    argv = cargo_build_argv(
        layout,
        cargo_executable="/toolchain/cargo",
        target="x86_64-unknown-linux-gnu",
    )
    assert argv == (
        "/toolchain/cargo",
        "build",
        "--locked",
        "--profile",
        "hashi-dev",
        "--package",
        "rusty-claude-cli",
        "--target",
        "x86_64-unknown-linux-gnu",
        "--features",
        "alpha,zeta",
    )


def test_diagnostics_are_actionable_bounded_and_redacted() -> None:
    text = """Compiling dependency
OPENAI_API_KEY=not-selected-because-not-actionable
error[E123]: compiler failed OPENAI_API_KEY=private-value token=second-private Authorization: Bearer third-private
Caused by: linking with cc failed
"""
    rendered = redact_diagnostics(text, limit=200)
    assert "compiler failed" in rendered
    assert "token=<redacted>" in rendered
    assert "private-value" not in rendered
    assert "third-private" not in rendered
    assert len(rendered) <= 200


def _fake_cargo_script(path: Path, *, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


async def test_toolchain_probe_uses_allowlisted_environment(tmp_path: Path) -> None:
    toolchain_dir = tmp_path / "toolchain"
    toolchain_dir.mkdir()
    for name, version in (("cargo", "cargo 1.90.0"), ("rustc", "rustc 1.90.0")):
        _fake_cargo_script(
            toolchain_dir / name,
            body=f"""import os
if "OPENAI_API_KEY" in os.environ:
    raise SystemExit(9)
print({version!r})
""",
        )
    identity = await inspect_toolchain(
        environment={
            "PATH": f"{toolchain_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "OPENAI_API_KEY": "must-not-pass",
        }
    )
    assert identity.cargo_version == "cargo 1.90.0"
    assert identity.rustc_version == "rustc 1.90.0"


async def test_toolchain_probe_reports_missing_executables(tmp_path: Path) -> None:
    with pytest.raises(HERRebuildError) as caught:
        await inspect_toolchain(environment={"PATH": str(tmp_path)})
    assert caught.value.failure_kind == FailureKind.TOOLCHAIN_MISSING


async def test_build_controller_runs_isolated_fake_cargo(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo",
        body="""import os
import pathlib
import sys

target = sys.argv[sys.argv.index("--target") + 1]
profile = sys.argv[sys.argv.index("--profile") + 1]
output = pathlib.Path(os.environ["CARGO_TARGET_DIR"]) / target / profile / "claw"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake-her-binary")
print("Finished fake HER build")
print("SECRET_PRESENT", "OPENAI_API_KEY" in os.environ)
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    fingerprint = compute_source_fingerprint(
        layout,
        toolchain=toolchain,
        target="x86_64-unknown-linux-gnu",
    )
    controller = HERBuildController(layout, state_root=tmp_path / "state")
    artifact = await controller.build(
        job_id="rebuild-test-success",
        fingerprint=fingerprint,
        toolchain=toolchain,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "OPENAI_API_KEY": "must-not-pass",
        },
    )
    assert artifact.binary_path.read_bytes() == b"fake-her-binary"
    log = artifact.build_log_path.read_text(encoding="utf-8")
    assert "Finished fake HER build" in log
    assert "SECRET_PRESENT False" in log
    assert "must-not-pass" not in log


async def test_build_controller_reports_compiler_failure_without_secret(
    tmp_path: Path,
) -> None:
    layout = _source_layout(tmp_path)
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo-fail",
        body="""import sys
print("error[E999]: broken token=private-value")
sys.exit(7)
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    controller = HERBuildController(layout, state_root=tmp_path / "state")
    with pytest.raises(HERRebuildError) as caught:
        await controller.build(
            job_id="rebuild-test-failure",
            fingerprint=compute_source_fingerprint(
                layout,
                toolchain=toolchain,
                target="x86_64-unknown-linux-gnu",
            ),
            toolchain=toolchain,
            environment={"PATH": os.environ.get("PATH", "")},
        )
    assert caught.value.failure_kind == FailureKind.CARGO_FAILED
    assert caught.value.exit_code == 7
    assert "token=<redacted>" in (caught.value.diagnostics or "")
    assert "private-value" not in (caught.value.diagnostics or "")


async def test_build_controller_timeout_terminates_process(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo-timeout",
        body="""import time
print("building forever", flush=True)
time.sleep(30)
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    controller = HERBuildController(
        layout,
        state_root=tmp_path / "state",
        build_timeout_seconds=0.05,
    )
    with pytest.raises(HERRebuildError) as caught:
        await controller.build(
            job_id="rebuild-test-timeout",
            fingerprint=compute_source_fingerprint(
                layout,
                toolchain=toolchain,
                target="x86_64-unknown-linux-gnu",
            ),
            toolchain=toolchain,
            environment={"PATH": os.environ.get("PATH", "")},
        )
    assert caught.value.failure_kind == FailureKind.CARGO_TIMEOUT


async def test_build_controller_rejects_source_change_during_build(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    main_rs = layout.rust_root / "crates" / "rusty-claude-cli" / "src" / "main.rs"
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo-source-change",
        body=f"""import os
import pathlib
import sys

pathlib.Path({str(main_rs)!r}).write_text("changed during build\\n", encoding="utf-8")
target = sys.argv[sys.argv.index("--target") + 1]
profile = sys.argv[sys.argv.index("--profile") + 1]
output = pathlib.Path(os.environ["CARGO_TARGET_DIR"]) / target / profile / "claw"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"uncorrelated")
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    fingerprint = compute_source_fingerprint(
        layout,
        toolchain=toolchain,
        target="x86_64-unknown-linux-gnu",
    )
    controller = HERBuildController(layout, state_root=tmp_path / "state")
    with pytest.raises(HERRebuildError) as caught:
        await controller.build(
            job_id="rebuild-test-source-change",
            fingerprint=fingerprint,
            toolchain=toolchain,
            environment={"PATH": os.environ.get("PATH", "")},
        )
    assert caught.value.failure_kind == FailureKind.FINGERPRINT_FAILED


async def test_build_controller_bounds_local_log_without_blocking_cargo(
    tmp_path: Path,
) -> None:
    layout = _source_layout(tmp_path)
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo-large-log",
        body="""import os
import pathlib
import sys

target = sys.argv[sys.argv.index("--target") + 1]
profile = sys.argv[sys.argv.index("--profile") + 1]
output = pathlib.Path(os.environ["CARGO_TARGET_DIR"]) / target / profile / "claw"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake-her")
print("x" * 10000)
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    fingerprint = compute_source_fingerprint(
        layout,
        toolchain=toolchain,
        target="x86_64-unknown-linux-gnu",
    )
    controller = HERBuildController(
        layout,
        state_root=tmp_path / "state",
        max_log_bytes=1024,
    )
    artifact = await controller.build(
        job_id="rebuild-test-large-log",
        fingerprint=fingerprint,
        toolchain=toolchain,
        environment={"PATH": os.environ.get("PATH", "")},
    )
    assert artifact.log_truncated is True
    assert artifact.build_log_path.stat().st_size <= 1024


async def test_build_controller_cancellation_terminates_cargo(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    pid_path = tmp_path / "cargo.pid"
    fake_cargo = _fake_cargo_script(
        tmp_path / "fake-cargo-cancel",
        body=f"""import os
import pathlib
import time

pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
""",
    )
    toolchain = _toolchain(str(fake_cargo))
    fingerprint = compute_source_fingerprint(
        layout,
        toolchain=toolchain,
        target="x86_64-unknown-linux-gnu",
    )
    controller = HERBuildController(layout, state_root=tmp_path / "state")
    task = asyncio.create_task(
        controller.build(
            job_id="rebuild-test-cancel",
            fingerprint=fingerprint,
            toolchain=toolchain,
            environment={"PATH": os.environ.get("PATH", "")},
        )
    )
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_path.exists()
    cargo_pid = int(pid_path.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(cargo_pid, 0)


def _artifact(tmp_path: Path, layout: HERSourceLayout) -> BuildArtifact:
    binary = tmp_path / "cargo-output" / "claw"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"immutable-her")
    binary.chmod(0o755)
    log = tmp_path / "build.log"
    log.write_text("build complete\n", encoding="utf-8")
    return BuildArtifact(
        job_id="rebuild-test-candidate",
        fingerprint=_fingerprint(layout),
        binary_path=binary,
        build_log_path=log,
        build_started_at="2026-08-16T00:00:00+00:00",
        build_finished_at="2026-08-16T00:00:02+00:00",
        build_duration_seconds=2.0,
        cargo_argv=("cargo", "build"),
        diagnostics="",
        log_truncated=False,
    )


def test_candidate_staging_is_immutable_and_digest_verified(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    artifact = _artifact(tmp_path, layout)
    store = CandidateStore(tmp_path / "state" / "candidates")
    candidate = store.stage(
        artifact,
        toolchain=_toolchain(),
        quick_verification={"result": "passed"},
    )
    assert candidate.development_build is True
    assert candidate.production_certified is False
    assert sha256_file(Path(candidate.binary_path)) == candidate.binary_sha256
    assert Path(candidate.build_log_path).parent == Path(candidate.candidate_dir)
    assert Path(candidate.build_log_path).read_text(encoding="utf-8") == "build complete\n"
    quick_path = Path(candidate.candidate_dir) / "quick-verification.json"
    assert json.loads(quick_path.read_text(encoding="utf-8")) == {"result": "passed"}
    assert (
        store.stage(
            artifact,
            toolchain=_toolchain(),
            quick_verification={"result": "passed"},
        ).candidate_id
        == candidate.candidate_id
    )

    candidate_binary = Path(candidate.binary_path)
    candidate_binary.chmod(0o755)
    candidate_binary.write_bytes(b"tampered")
    with pytest.raises(HERRebuildError) as caught:
        store.read(candidate.candidate_id)
    assert caught.value.failure_kind == FailureKind.CANDIDATE_DIGEST_MISMATCH


def test_candidate_staging_rejects_unpassed_verification(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    artifact = _artifact(tmp_path, layout)
    store = CandidateStore(tmp_path / "state" / "candidates")
    with pytest.raises(HERRebuildError) as caught:
        store.stage(
            artifact,
            toolchain=_toolchain(),
            quick_verification={"result": "failed", "reason": "version probe"},
        )
    assert caught.value.failure_kind == FailureKind.QUICK_TEST_FAILED


def test_selection_is_atomic_and_retains_rollback_candidate(tmp_path: Path) -> None:
    layout = _source_layout(tmp_path)
    candidates_root = tmp_path / "state" / "candidates"
    candidate_store = CandidateStore(candidates_root)
    first_artifact = _artifact(tmp_path, layout)
    first = candidate_store.stage(
        first_artifact,
        toolchain=_toolchain(),
        quick_verification={"result": "passed"},
    )
    selection = DevelopmentSelectionStore(
        tmp_path / "state" / "active-development.json",
        candidates_root=candidates_root,
    )
    selected_first = selection.select(first.candidate_id, job_id="rebuild-one")
    assert selected_first["active"]["candidate_id"] == first.candidate_id
    assert selected_first["previous"] is None

    second_binary = first_artifact.binary_path
    second_binary.write_bytes(b"second-her")
    second_fingerprint = SourceFingerprint(
        **{
            **first_artifact.fingerprint.__dict__,
            "digest": "f" * 64,
            "dirty": True,
        }
    )
    second_artifact = BuildArtifact(
        **{
            **first_artifact.__dict__,
            "job_id": "rebuild-two",
            "fingerprint": second_fingerprint,
        }
    )
    second = candidate_store.stage(
        second_artifact,
        toolchain=_toolchain(),
        quick_verification={"result": "passed"},
    )
    selected_second = selection.select(second.candidate_id, job_id="rebuild-two")
    assert selected_second["active"]["candidate_id"] == second.candidate_id
    assert selected_second["previous"]["candidate_id"] == first.candidate_id

    restored = selection.restore_previous(job_id="rebuild-two-rollback")
    assert restored is not None
    assert restored["active"]["candidate_id"] == first.candidate_id
    assert restored["adoption_state"] == "rollback_selected"
