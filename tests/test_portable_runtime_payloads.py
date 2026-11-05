"""Payload fixtures verify bytes only; they contain no runnable or qualified Python."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from orchestrator.function_generation import build_source_manifest_from_entries
from orchestrator.runtime_contract import CORE_SOURCE_PATHS, RuntimeContractError, core_source_digest
from orchestrator.runtime_installation_builder import file_table
from orchestrator import runtime_release_capsule as capsule

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def image(tmp_path):
    code = tmp_path / "app/hashi"
    for relative in CORE_SOURCE_PATHS:
        source, target = ROOT / relative, code / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    roles = {}
    for name, (relative, role) in capsule.ROLE_ROOTS.items():
        directory = tmp_path / relative
        directory.mkdir(parents=True)
        (directory / "fixture.bin").write_bytes((name + " bytes").encode())
        roles[name] = {"root": relative, "runtime_id": "unqualified-test-only-" + name,
                       "fingerprint": {"role": role}, "files": file_table(directory)}
    value = {"schema_version": 1, "core_source_digest": core_source_digest(code),
             "roles": roles, "sidecars": {}, "function_source": None,
             "function_manifest": build_source_manifest_from_entries(()).to_dict()}
    (tmp_path / capsule.CAPSULE_PATH).write_text(json.dumps(value), encoding="utf-8")
    return tmp_path, code, value


def test_payload_roundtrip_preserves_every_declared_byte_and_rejects_early_qualification(image):
    root, code, original = image
    packed = capsule.pack_release_payloads(root, code, roots=("runtime/functions",))
    assert packed["schema_version"] == 2
    assert not (root / "runtime/functions").exists()
    assert capsule.verify_capsule(root, code)["roles"] == original["roles"]
    with pytest.raises(RuntimeContractError, match="fully expanded"):
        capsule.qualify_relocated_roles(root, code)
    capsule.materialize_release_payloads(root, code)
    assert file_table(root / "runtime/functions") == original["roles"]["functions"]["files"]
    assert (root / "runtime/core/fixture.bin").read_bytes() == b"core bytes"


def test_corrupt_archive_is_rejected_before_any_destination_is_created(image):
    root, code, _original = image
    capsule.pack_release_payloads(root, code, roots=("runtime/functions",))
    archive = root / "runtime/functions.payload.zip"
    archive.write_bytes(archive.read_bytes() + b"corruption")
    with pytest.raises(RuntimeContractError, match="archive checksum"):
        capsule.materialize_release_payloads(root, code)
    assert not (root / "runtime/functions").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a\\b",
                                  "folder/../escape", "NUL.txt", "a./file"])
def test_archive_members_cannot_escape_or_alias_their_root(tmp_path, name):
    archive = tmp_path / "runtime/functions.payload.zip"
    archive.parent.mkdir()
    data = b"untrusted bytes"
    with zipfile.ZipFile(archive, "x") as output:
        output.writestr(name, data)
    table = {name: hashlib.sha256(data).hexdigest()}
    record = {"archive": "runtime/functions.payload.zip",
              "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "file_count": 1, "uncompressed_bytes": len(data)}
    with pytest.raises(RuntimeContractError, match="path"):
        capsule._verified_payload(tmp_path, "runtime/functions", record, table)


def test_payload_expansion_cannot_overwrite_existing_changed_files(image):
    root, code, _original = image
    capsule.pack_release_payloads(root, code, roots=("runtime/functions",))
    destination = root / "runtime/functions"
    destination.mkdir()
    witness = destination / "fixture.bin"
    witness.write_bytes(b"preserve unknown bytes")
    with pytest.raises(RuntimeContractError, match="hash mismatch"):
        capsule.materialize_release_payloads(root, code)
    assert witness.read_bytes() == b"preserve unknown bytes"


def test_registered_runtime_cannot_be_repacked_or_expanded(image):
    root, code, _original = image
    selector = root / "runtime/hashi-runtime-release.json"
    selector.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="installed"):
        capsule.pack_release_payloads(root, code, roots=("runtime/functions",))
    with pytest.raises(RuntimeContractError, match="Qualified"):
        capsule.materialize_release_payloads(root, code)
# HASHI-Fingerprint: hfp_990ae4fcfd1f483e8a38afcf1e72d1fb
