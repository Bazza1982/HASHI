from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools import instance_registry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(instance_registry, "_port_available", lambda _port: True)
    program = tmp_path / "program"
    program.mkdir()
    return instance_registry.InstanceRegistry(
        program_root=program,
        program_version="4.0.0-test.1",
        registry_root=tmp_path / "config",
        data_root=tmp_path / "data",
        environment_id="test-os",
        now=lambda: "2026-09-08T12:00:00+00:00",
    )


def test_managed_create_allocates_isolated_identity_without_secrets(registry):
    record = registry.create("alpha")
    home = Path(record["bridge_home"])

    assert record["managed"] is True
    assert home == registry.instances_root / "alpha"
    assert not (home / "secrets.json").exists()
    config = json.loads((home / "agents.json").read_text(encoding="utf-8"))
    assert config == {
        "agents": [],
        "global": {
            "api_gateway_port": 18801,
            "instance_id": "alpha",
            "ui_language": "en",
            "workbench_port": 18800,
        },
    }
    marker = json.loads(
        (home / ".hashi-instance.json").read_text(encoding="utf-8")
    )
    assert marker["record_id"] == record["record_id"]
    selected, source = registry.select(cwd=registry.program_root)
    assert selected["name"] == "alpha"
    assert source == "only_instance"


def test_instance_names_are_case_insensitively_unique(registry):
    registry.create("Alpha")

    with pytest.raises(instance_registry.InstanceRegistryError, match="already exists"):
        registry.create("alpha")
    with pytest.raises(instance_registry.InstanceRegistryError, match="Instance names"):
        registry.create("../escape")


def test_selection_precedence_is_explicit_binding_default_only_then_prompt(
    registry, tmp_path
):
    alpha = registry.create("alpha")
    beta = registry.create("beta")
    bound = tmp_path / "projects" / "beta"
    nested = bound / "src"
    nested.mkdir(parents=True)
    registry.bind("beta", bound)
    registry.set_default("alpha")

    selected, source = registry.select(explicit="alpha", cwd=nested)
    assert selected == alpha
    assert source == "explicit"
    selected, source = registry.select(cwd=nested)
    assert selected == beta
    assert source == "cwd_binding"
    selected, source = registry.select(cwd=tmp_path / "elsewhere")
    assert selected == alpha
    assert source == "default"

    payload = registry.load()
    payload["default_instance"] = None
    registry._write(payload)
    with pytest.raises(instance_registry.InstanceSelectionError, match="Multiple"):
        registry.select(cwd=tmp_path / "elsewhere", interactive=False)
    with pytest.raises(instance_registry.InstanceSelectionError, match="Invalid"):
        registry.select(
            cwd=tmp_path / "elsewhere",
            interactive=True,
            input_fn=lambda _prompt: "0",
            output_fn=lambda _line: None,
        )
    output = []
    selected, source = registry.select(
        cwd=tmp_path / "elsewhere",
        interactive=True,
        input_fn=lambda _prompt: "2",
        output_fn=output.append,
    )
    assert selected["name"] == "beta"
    assert source == "interactive"
    assert output[0] == "Select a HASHI instance:"


def test_existing_git_registration_preserves_every_source_byte(registry, tmp_path):
    checkout = tmp_path / "existing git"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / "scripts").mkdir()
    (checkout / "main.py").write_text("# existing\n", encoding="utf-8")
    (checkout / "runtime-entry.json").write_text("{}\n", encoding="utf-8")
    (checkout / "scripts" / "check_runtime_contract.py").write_text(
        "# existing\n", encoding="utf-8"
    )
    (checkout / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "HASHI-EXISTING",
                    "workbench_port": 19020,
                },
                "agents": [{"name": "kept"}],
            }
        ),
        encoding="utf-8",
    )
    before = {
        path.relative_to(checkout).as_posix(): path.read_bytes()
        for path in checkout.rglob("*")
        if path.is_file()
    }

    record = registry.create("existing", source_root=checkout)

    after = {
        path.relative_to(checkout).as_posix(): path.read_bytes()
        for path in checkout.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert record["managed"] is False
    assert record["source_kind"] == "git"
    assert record["instance_id"] == "HASHI-EXISTING"
    assert record["api_port"] == 19020
    selected, source = registry.select(cwd=checkout / "nested")
    assert selected["name"] == "existing"
    assert source == "cwd_binding"
    with pytest.raises(instance_registry.InstanceRegistryError, match="already registered"):
        registry.create("duplicate", source_root=checkout)


def test_duplicate_runtime_identity_is_rejected_even_with_distinct_paths(registry, tmp_path):
    for name, port in (("first", 19020), ("second", 19022)):
        checkout = tmp_path / name
        (checkout / "scripts").mkdir(parents=True)
        (checkout / "main.py").write_text("# existing\n", encoding="utf-8")
        (checkout / "runtime-entry.json").write_text("{}\n", encoding="utf-8")
        (checkout / "scripts" / "check_runtime_contract.py").write_text(
            "# existing\n", encoding="utf-8"
        )
        (checkout / "agents.json").write_text(
            json.dumps(
                {
                    "global": {
                        "instance_id": "SAME-IDENTITY",
                        "workbench_port": port,
                    },
                    "agents": [],
                }
            ),
            encoding="utf-8",
        )

    registry.create("first", source_root=tmp_path / "first")
    with pytest.raises(instance_registry.InstanceRegistryError, match="identity"):
        registry.create("second", source_root=tmp_path / "second")


def test_program_upgrade_requires_separate_stopped_instance_adoption(registry):
    record = registry.create("alpha")
    config_path = Path(record["bridge_home"]) / "agents.json"
    original = config_path.read_bytes()
    upgraded = instance_registry.InstanceRegistry(
        program_root=registry.program_root,
        program_version="4.0.0-test.2",
        registry_root=registry.registry_root,
        data_root=registry.data_root,
        environment_id="test-os",
        now=registry.now,
    )

    [listed] = upgraded.records()
    assert listed["update_pending"] is True
    assert config_path.read_bytes() == original
    adopted = upgraded.adopt("alpha")
    assert adopted["adopted_program_version"] == "4.0.0-test.2"
    assert config_path.read_bytes() == original


def test_recoverable_remove_and_restore_preserve_managed_data(registry):
    record = registry.create("alpha", make_default=True)
    home = Path(record["bridge_home"])
    sentinel = home / "workspaces" / "agent" / "identity.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep-me", encoding="utf-8")

    removed = registry.remove("alpha")

    assert registry.records() == []
    assert not home.exists()
    assert Path(removed["trash_path"]).is_dir()
    restored = registry.restore("alpha")
    assert restored["record_id"] == record["record_id"]
    assert sentinel.read_text(encoding="utf-8") == "keep-me"
    [listed] = registry.records()
    assert listed["default"] is True


def test_restore_rejects_recovery_data_with_a_mismatched_instance_marker(registry):
    registry.create("alpha")
    removed = registry.remove("alpha")
    trash_home = Path(removed["trash_path"])
    marker_path = trash_home / ".hashi-instance.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["record_id"] = "different-record"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(instance_registry.InstanceRegistryError, match="marker"):
        registry.restore("alpha")

    assert trash_home.is_dir()
    assert not (registry.instances_root / "alpha").exists()


def test_purge_requires_exact_confirmation_and_only_deletes_managed_data(registry):
    record = registry.create("Alpha")
    home = Path(record["bridge_home"])

    with pytest.raises(instance_registry.InstanceRegistryError, match="exact"):
        registry.remove("alpha", purge=True, confirmation="alpha")
    removed = registry.remove("alpha", purge=True, confirmation="Alpha")

    assert removed["purged_at"] is not None
    assert removed["trash_path"] is None
    assert not home.exists()
    with pytest.raises(instance_registry.InstanceRegistryError, match="No recoverable"):
        registry.restore("alpha")


def test_external_remove_never_moves_or_purges_checkout(registry, tmp_path):
    checkout = tmp_path / "external"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "main.py").write_text("# keep\n", encoding="utf-8")
    (checkout / "runtime-entry.json").write_text("{}\n", encoding="utf-8")
    (checkout / "scripts" / "check_runtime_contract.py").write_text(
        "# keep\n", encoding="utf-8"
    )
    (checkout / "agents.json").write_text(
        '{"global":{"instance_id":"EXT"},"agents":[]}\n', encoding="utf-8"
    )
    registry.create("external", source_root=checkout)

    with pytest.raises(instance_registry.InstanceRegistryError, match="Refusing"):
        registry.remove("external", purge=True, confirmation="external")
    registry.remove("external")

    assert (checkout / "main.py").read_text(encoding="utf-8") == "# keep\n"
    assert (checkout / "agents.json").is_file()
    registry.restore("external")


def test_registry_environment_mismatch_fails_closed(registry):
    registry.create("alpha")
    other = instance_registry.InstanceRegistry(
        program_root=registry.program_root,
        program_version=registry.program_version,
        registry_root=registry.registry_root,
        data_root=registry.data_root,
        environment_id="different-os",
    )

    with pytest.raises(instance_registry.InstanceRegistryError, match="mismatch"):
        other.load()


def test_registry_and_managed_data_cannot_live_inside_program_install(tmp_path):
    program = tmp_path / "program"
    program.mkdir()

    with pytest.raises(instance_registry.InstanceRegistryError, match="outside"):
        instance_registry.InstanceRegistry(
            program_root=program,
            program_version="test",
            registry_root=program / "state" / "registry",
            data_root=tmp_path / "data",
            environment_id="test-os",
        )
    with pytest.raises(instance_registry.InstanceRegistryError, match="outside"):
        instance_registry.InstanceRegistry(
            program_root=program,
            program_version="test",
            registry_root=tmp_path / "registry",
            data_root=program / "state" / "data",
            environment_id="test-os",
        )


def test_tampered_managed_path_can_never_expand_purge_scope(registry):
    record = registry.create("alpha")
    home = Path(record["bridge_home"])
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    payload["instances"]["alpha"]["bridge_home"] = str(registry.instances_root)
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(instance_registry.InstanceRegistryError, match="exact data"):
        registry.remove("alpha", purge=True, confirmation="alpha")

    assert home.is_dir()


def test_registry_write_failure_rolls_back_new_managed_directory(
    registry, monkeypatch
):
    monkeypatch.setattr(
        registry,
        "_write",
        lambda _payload: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )

    with pytest.raises(OSError, match="synthetic"):
        registry.create("alpha")

    assert not (registry.instances_root / "alpha").exists()


def test_concurrent_instance_creates_are_serialized_and_keep_unique_ports(registry):
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(registry.create, ("alpha", "beta", "gamma", "delta")))

    assert {record["name"] for record in records} == {
        "alpha",
        "beta",
        "gamma",
        "delta",
    }
    assert len({record["api_port"] for record in records}) == 4
    assert len(registry.records()) == 4
