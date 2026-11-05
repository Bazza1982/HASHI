"""Payload fixtures verify bytes only; they contain no runnable or qualified Python."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
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
@pytest.fixture
def source_asset_image(image):
    root, code, value = image
    for relative in capsule.SOURCE_ASSET_ROOTS:
        target = root / relative / "fixture.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"complete opaque native resource fixture")
        value["sidecars"][relative] = file_table(root / relative)
    value["function_source"] = {
        "schema_version": 1, "source_commit": "1" * 40,
        "core_source_digest": value["core_source_digest"],
        "files": {path.relative_to(code).as_posix(): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "executable": bool(path.stat().st_mode & 0o111),
        } for path in code.rglob("*") if path.is_file()},
    }
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(value), encoding="utf-8")
    return root, code, value


def test_opaque_source_assets_roundtrip_with_full_publication_and_function_manifest(source_asset_image):
    root, code, original = source_asset_image
    before = file_table(code)
    packed = capsule.pack_release_payloads(root, code, roots=capsule.SOURCE_ASSET_ROOTS)
    assert packed["function_manifest"] == original["function_manifest"]
    assert packed["function_source"] == original["function_source"]
    assert all(not (root / relative).exists() for relative in capsule.SOURCE_ASSET_ROOTS)
    assert capsule.verify_capsule(root, code) == packed
    capsule.materialize_release_payloads(root, code)
    after = file_table(code)
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == {
        "voice_models/piper.payload.zip", "hashi_assets/ocr.payload.zip"}
    assert capsule.verify_capsule(root, code)["roles"] == original["roles"]


def test_packed_resource_must_match_the_entire_published_source_table(source_asset_image):
    root, code, _original = source_asset_image
    packed = capsule.pack_release_payloads(root, code, roots=capsule.SOURCE_ASSET_ROOTS)
    packed["function_source"]["files"]["voice_models/piper/fixture.bin"]["sha256"] = "0" * 64
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(packed), encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="publication bytes"):
        capsule.materialize_release_payloads(root, code)
    assert not (root / capsule.SOURCE_ASSET_ROOTS[0]).exists()


def test_executable_function_members_cannot_hide_in_opaque_source_payloads(source_asset_image):
    from orchestrator.function_generation import AssetEntry

    root, code, _original = source_asset_image
    packed = capsule.pack_release_payloads(root, code, roots=capsule.SOURCE_ASSET_ROOTS)
    asset = "voice_models/piper/fixture.bin"
    published = packed["function_source"]["files"][asset]
    packed["function_manifest"] = build_source_manifest_from_entries(
        (), assets=[AssetEntry(asset, published["sha256"], published["executable"])]).to_dict()
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(packed), encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="must remain expanded"):
        capsule.verify_capsule(root, code)


def test_opaque_resource_payload_cannot_introduce_python_source(source_asset_image):
    root, code, value = source_asset_image
    target = root / capsule.SOURCE_ASSET_ROOTS[0] / "uncommitted.py"
    target.write_text("raise AssertionError('must never execute')", encoding="utf-8")
    value["sidecars"][capsule.SOURCE_ASSET_ROOTS[0]] = file_table(target.parent)
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="cannot contain Python"):
        capsule.pack_release_payloads(root, code, roots=capsule.SOURCE_ASSET_ROOTS)
    assert target.is_file()
def _core_transfer_fixture(image):
    root, code, value = image
    (root / "runtime/core/python.exe").write_bytes(b"Non-executable transfer test fixture")
    value["roles"]["core"]["files"] = file_table(root / "runtime/core")
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(value), encoding="utf-8")
    return root, code, capsule.pack_release_payloads(root, code, roots=("runtime/core",))


def _native_expand_core(root, tmp_path, *, without_file_hash=False):
    # Load only these function definitions, never the installer's main body.
    installer = ROOT / "packaging/portable_windows/templates/launcher/Install-To-PC.ps1"
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = tmp_path / "expand-core-only.ps1"
    script.write_text(
        "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
        "$tokens=$null; $errors=$null\n"
        "$ast=[Management.Automation.Language.Parser]::ParseFile("
        + quote(installer) + ",[ref]$tokens,[ref]$errors)\n"
        "if ($errors.Count) { throw 'Installer parse failed' }\n"
        "$names=@('Read-JsonObject','Get-Sha256','Assert-OrdinaryDirectory',"
        "'Assert-TreeHasNoReparsePoints','Expand-PortableCorePayload')\n"
        "foreach ($name in $names) {\n"
        " $matches=@($ast.FindAll({param($node) $node -is "
        "[Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true))\n"
        " if ($matches.Count -ne 1) { throw 'Missing exact installer function' }\n"
        " . ([ScriptBlock]::Create($matches[0].Extent.Text))\n}\n"
        + (
            "Import-Module Microsoft.PowerShell.Management,Microsoft.PowerShell.Utility -ErrorAction Stop\n"
            "Remove-Item Function:\\Get-FileHash -ErrorAction SilentlyContinue\n"
            "$PSModuleAutoLoadingPreference='None'\n"
            "if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) { throw 'Hash command still available' }\n"
            if without_file_hash else ""
        )
        + "Expand-PortableCorePayload -Root " + quote(root) + "\n",
        encoding="utf-8-sig",
    )
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)],
                          text=True, capture_output=True, timeout=60)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows installer transfer boundary")
@pytest.mark.parametrize("without_file_hash", (False, True))
def test_native_core_expansion_verifies_every_byte_without_executing_a_runtime(image, tmp_path, without_file_hash):
    root, _code, value = _core_transfer_fixture(image)
    result = _native_expand_core(root, tmp_path, without_file_hash=without_file_hash)
    assert result.returncode == 0, result.stdout + result.stderr
    assert file_table(root / "runtime/core") == value["roles"]["core"]["files"]
    assert not (root / "runtime/hashi-runtime-release.json").exists()
    assert not (root / "runtime-qualifications").exists()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows installer transfer boundary")
@pytest.mark.parametrize("failure", ("checksum", "extra", "escape", "startup", "oversize", "existing"))
def test_native_core_expansion_refuses_corruption_aliases_hooks_and_overwrites(image, tmp_path, failure):
    root, _code, value = _core_transfer_fixture(image)
    archive = root / "runtime/core.payload.zip"
    record = value["payloads"]["runtime/core"]
    if failure == "checksum":
        archive.write_bytes(archive.read_bytes() + b"modified archive")
    elif failure in {"extra", "escape", "startup"}:
        name = {"extra": "unlisted.bin", "escape": "../escape.bin",
                "startup": "Lib/sitecustomize.py"}[failure]
        with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr(name, b"must never be used")
        record["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        if failure != "extra":
            value["roles"]["core"]["files"][name] = hashlib.sha256(b"must never be used").hexdigest()
            record["file_count"] += 1
            record["uncompressed_bytes"] += len(b"must never be used")
    elif failure == "oversize":
        record["uncompressed_bytes"] -= 1
    else:
        destination = root / "runtime/core"
        destination.mkdir()
        (destination / "unknown.txt").write_bytes(b"keep this")
    (root / capsule.CAPSULE_PATH).write_text(json.dumps(value), encoding="utf-8")
    result = _native_expand_core(root, tmp_path)
    assert result.returncode != 0
    assert not (root / "escape.bin").exists()
    if failure == "existing":
        assert (root / "runtime/core/unknown.txt").read_bytes() == b"keep this"
    else:
        assert not (root / "runtime/core").exists()
    assert not (root / "runtime/hashi-runtime-release.json").exists()
# HASHI-Fingerprint: hfp_990ae4fcfd1f483e8a38afcf1e72d1fb
