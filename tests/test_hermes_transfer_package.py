from __future__ import annotations

import zipfile

import pytest

from orchestrator.hermes_transfer import (
    DryRunReport,
    PlannedWrite,
    TransferPackageError,
    TransferSchemaError,
    create_transfer_package,
    default_profile_policy,
    default_secrets_policy,
    read_transfer_package,
    validate_manifest,
    validate_normalized_agent,
    verify_package_checksums,
)
from orchestrator.hermes_transfer.schema import new_manifest


def _manifest() -> dict:
    return new_manifest(
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id="zelda",
        display_name="Zelda",
    )


def _normalized_agent() -> dict:
    return {
        "agent_id": "zelda",
        "display_name": "Zelda",
        "identity_text_path": "identity/agent.md",
        "capabilities": {
            "hchat": True,
            "remote": True,
        },
        "memory": {
            "strategy": "portable_files_first",
            "notes_path": "memory/import_notes.json",
            "max_chars_per_item": 2200,
            "session_state_included": False,
        },
        "secrets": {
            "included": False,
            "encrypted": False,
            "keys": [],
        },
    }


def test_manifest_requires_hermes_safe_defaults():
    manifest = _manifest()

    validate_manifest(manifest)

    assert manifest["profile_directory_policy"] == "whitelist_only"
    assert manifest["cron_import_policy"] == "paused_review_drafts"
    assert manifest["session_import_policy"] == "never"
    assert manifest["secrets_policy_file"] == "secrets.policy.json"


def test_manifest_rejects_same_runtime_transfer():
    manifest = _manifest()
    manifest["target_runtime"] = "hashi"

    with pytest.raises(TransferSchemaError, match="must differ"):
        validate_manifest(manifest)


def test_normalized_agent_rejects_session_state():
    agent = _normalized_agent()
    agent["memory"]["session_state_included"] = True

    with pytest.raises(TransferSchemaError, match="session_state_included"):
        validate_normalized_agent(agent)


def test_default_policies_exclude_dangerous_hermes_state():
    profile_policy = default_profile_policy()
    secrets_policy = default_secrets_policy()

    assert "sessions" in profile_policy["blocked_profile_subdirs"]
    assert profile_policy["cron_policy"]["default_state"] == "paused"
    assert secrets_policy["included"] is False
    assert "telegram_token" in secrets_policy["blocked_secret_classes"]


def test_create_and_read_transfer_package_roundtrip(tmp_path):
    package = tmp_path / "zelda.hashi-hermes-agent"
    dry_run = DryRunReport(
        operation="export",
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id="zelda",
        planned_writes=[
            PlannedWrite(
                path="identity/agent.md",
                action="create",
                description="copy HASHI agent identity",
            )
        ],
        warnings=["target profile remains disabled"],
    )

    result = create_transfer_package(
        package,
        manifest=_manifest(),
        normalized_agent=_normalized_agent(),
        files={
            "identity/agent.md": "# Zelda\n",
            "memory/import_notes.json": "{}\n",
        },
        dry_run_report=dry_run,
        migration_report="# Migration Report\n",
        post_migration_self_check="# Self Check\n",
    )

    read_back = read_transfer_package(package)

    assert result.package_path == package
    assert read_back.manifest["agent_id"] == "zelda"
    assert read_back.normalized_agent["identity_text_path"] == "identity/agent.md"
    assert read_back.dry_run_plan["planned_writes"][0]["path"] == "identity/agent.md"
    assert "profile_policy.json" in read_back.names
    assert "secrets.policy.json" in read_back.names
    assert "audit/migration_report.md" in read_back.names
    assert verify_package_checksums(package)["identity/agent.md"]


def test_package_writer_rejects_unsafe_entry_name(tmp_path):
    with pytest.raises(TransferPackageError, match="unsafe package entry"):
        create_transfer_package(
            tmp_path / "bad.hashi-hermes-agent",
            manifest=_manifest(),
            normalized_agent=_normalized_agent(),
            files={"../outside.txt": "bad"},
        )


def test_package_reader_rejects_checksum_mismatch(tmp_path):
    package = tmp_path / "zelda.hashi-hermes-agent"
    create_transfer_package(
        package,
        manifest=_manifest(),
        normalized_agent=_normalized_agent(),
        files={"identity/agent.md": "# Zelda\n"},
    )

    tampered = tmp_path / "tampered.hashi-hermes-agent"
    with zipfile.ZipFile(package, "r") as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "identity/agent.md":
                content = b"changed\n"
            dst.writestr(item.filename, content)

    with pytest.raises(TransferPackageError, match="checksum mismatch"):
        read_transfer_package(tampered)
