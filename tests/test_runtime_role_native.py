"""Native Windows/Linux sealed-role qualification; no services or external accounts."""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, CORE_SOURCE_PATHS, RuntimeFingerprint, core_source_digest,
    load_runtime_policy, locked_standard_dependencies,
    runtime_installation_reference,
)
from orchestrator.runtime_installation_builder import (
    build_reference_roles, build_reference_functions, qualify_environment, snapshot_reference,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(os.name != "nt" and sys.platform != "linux",
                       reason="This native gate qualifies Windows and Linux only"),
]


def _site_packages(environment):
    suffix = "Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    return Path(environment) / suffix


def _name(text):
    return text.lower().replace("_", "-").replace(".", "-")


@pytest.fixture(scope="module")
def native_roles(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-qualified-roles")
    # Qualify the exact protected code artifact, rather than a build workspace
    # whose project .egg-info is visible as ambient installed metadata.
    code_root = directory / "core-source"
    for relative in (*CORE_SOURCE_PATHS, "pyproject.toml", "runtime-entry.json",
                     "constraints/standard-py312.lock", "constraints/core-host-py312.lock"):
        target = code_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    assert core_source_digest(code_root) == core_source_digest(ROOT)
    seed = directory / "captured-standard-site"
    seed.mkdir()
    locked = locked_standard_dependencies(ROOT, load_runtime_policy(ROOT))
    distributions = {_name(d.metadata["Name"]): d for d in importlib.metadata.distributions()}
    # Capture the exact existing standard packages, excluding test-runner extras.
    # Their recorded wheel hashes are checked before any byte is copied.
    for name, version in locked.items():
        distribution = distributions[name]
        assert distribution.version == version
        assert distribution.files, name
        site = Path(distribution.locate_file("")).resolve()
        for entry in distribution.files:
            original = Path(distribution.locate_file(entry)).resolve()
            if not original.is_relative_to(site.resolve()) or "__pycache__" in original.parts:
                continue
            assert original.is_file(), original
            if entry.hash is not None:
                assert entry.hash.mode == "sha256"
                actual = base64.urlsafe_b64encode(hashlib.sha256(original.read_bytes()).digest()).decode().rstrip("=")
                assert actual == entry.hash.value, original
            destination = seed / original.relative_to(site.resolve())
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                assert destination.read_bytes() == original.read_bytes()
            else:
                shutil.copyfile(original, destination)
    packages = "\n".join(f"{k}=={v}" for k, v in sorted(locked.items()))
    dependency_digest = "sha256:" + hashlib.sha256(packages.encode()).hexdigest()
    reference = directory / "reference.json"
    pin = snapshot_reference(code_root, base_root=Path(sys.base_prefix).resolve(),
                             functions_site=seed, destination=reference,
                             expected_dependency_digest=dependency_digest)
    result = build_reference_roles(code_root, reference, expected_digest=pin,
                                   destination=directory / "release")
    result["code_root"] = str(code_root)
    result["reference_path"] = str(reference)
    result["locked"] = locked
    result["source_head"] = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    result["source_core_digest"] = core_source_digest(code_root)
    result["CR_ID"] = RuntimeFingerprint.from_mapping(result["core"]).runtime_id
    result["FR_ID"] = RuntimeFingerprint.from_mapping(result["functions"]).runtime_id
    (directory / "runtime-role-qualification.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    return result


def _run(python, script, *args, timeout=90, code_root=ROOT):
    result = subprocess.run([str(python), "-I", "-B", "-c", script, str(code_root), *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_core_and_functions_are_actual_distinct_interpreters_with_exact_packages(native_roles):
    result = native_roles
    assert result["core"]["role"] == CORE_HOST_ROLE
    assert result["functions"]["role"] == FUNCTIONS_ROLE
    assert result["core"]["executable"] != result["functions"]["executable"]
    assert result["core"]["environment_prefix"] != result["functions"]["environment_prefix"]
    assert result["core"]["dependency_digest"] == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert result["core"]["dependency_digest"] != result["functions"]["dependency_digest"]
    observed = _run(result["core"]["executable"], """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from orchestrator.runtime_contract import load_core_runtime_selection,bind_process_runtime,enforce_runtime_contract,CORE_HOST_ROLE
selected=load_core_runtime_selection(Path(sys.argv[1]))
bind_process_runtime(selected.core,code_root=Path(sys.argv[1]))
blocked=False
try: import aiohttp
except ModuleNotFoundError: blocked=True
assert blocked
print(json.dumps(enforce_runtime_contract(Path(sys.argv[1]),role=CORE_HOST_ROLE).to_dict()))
""", code_root=result["code_root"])
    assert observed["installation_digest"]
    assert observed["environment_prefix"] == result["core"]["environment_prefix"]
    assert observed["dependency_digest"] == result["core"]["dependency_digest"]


PEER = """
import asyncio, importlib.metadata, json, os, sys
from pathlib import Path
from orchestrator.function_worker_protocol import JsonConnectionPeer
VALUE={value!r}
FAILURE={failure!r}
async def serve(connection, bootstrap):
    stopping=asyncio.Event()
    state=Path(bootstrap["bridge_home"])/"durable.json"
    state.parent.mkdir(parents=True,exist_ok=True)
    async def request(method, params):
        if method=="prepare": return {{"pid":os.getpid(),"generation_id":bootstrap["manifest"]["generation_id"]}}
        if method=="activate" and FAILURE: raise RuntimeError("intentional activation rejection")
        if method=="execute":
            count=json.loads(state.read_text()) if state.exists() else 0
            state.write_text(json.dumps(count+1))
        if method=="stop": stopping.set()
        return {{"value":VALUE,"count":json.loads(state.read_text()) if state.exists() else 0,
                 "pid":os.getpid(),"prefix":sys.prefix,"dependency":importlib.metadata.version("hashi-runtime-fixture")}}
    peer=JsonConnectionPeer(connection,label="qualified-native-fixture",request_handler=request)
    peer.start()
    while not stopping.is_set() and not peer.is_closed: await asyncio.sleep(.01)
    await peer.close()
"""


def test_same_core_pid_can_replace_fr_dependency_and_rollback_without_replaying(native_roles, tmp_path):
    # The changed package is a harmless local qualification fixture. This proves
    # the mechanism, not PR137 security acceptance or complete business parity.
    release = Path(native_roles["destination"])
    original = Path(native_roles["functions"]["environment_prefix"])
    references = []
    runtime_ids = []
    selected = json.loads(Path(native_roles["selection"]).read_bytes())
    core_reference = selected["core"]
    for version in ("1", "2"):
        seed = tmp_path / ("captured-functions-" + version)
        shutil.copytree(_site_packages(original), seed)
        metadata = seed / ("hashi_runtime_fixture-" + version + ".dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Name: hashi-runtime-fixture\nVersion: {version}\n", encoding="utf-8")
        packages = dict(native_roles["locked"], **{"hashi-runtime-fixture": version})
        expected = "sha256:" + hashlib.sha256(
            "\n".join(f"{name}=={value}" for name, value in sorted(packages.items())).encode()).hexdigest()
        reference = tmp_path / ("reference-" + version + ".json")
        pin = snapshot_reference(
            Path(native_roles["code_root"]), base_root=release / "base", functions_site=seed,
            destination=reference, expected_dependency_digest=expected)
        candidate = build_reference_functions(
            Path(native_roles["code_root"]), reference, expected_digest=pin,
            destination=tmp_path / ("functions-" + version), core_reference=core_reference)
        assert candidate["core"] == core_reference
        assert candidate["core_runtime_id"] == native_roles["CR_ID"]
        assert candidate["automatic_adoption"] is False
        references.append(candidate["functions"])
        runtime_ids.append(candidate["functions_runtime_id"])
    requests = []
    from orchestrator.kernel_artifact import manifest_digest
    for label, failure in (("old", ""), ("new", ""), ("rejected", "activate")):
        artifact = tmp_path / label
        module = artifact / "orchestrator/role_native_fixture.py"
        module.parent.mkdir(parents=True)
        module.write_text(PEER.format(value="same-function-generation", failure=failure), encoding="utf-8")
        entries = [{"module": "orchestrator.role_native_fixture",
                    "relative_path": "orchestrator/role_native_fixture.py",
                    "sha256": hashlib.sha256(module.read_bytes()).hexdigest()}]
        requests.append({"generation_root": str(artifact), "entrypoint": "orchestrator.role_native_fixture:serve",
                         "manifest": {"generation_id": manifest_digest(entries), "entries": entries,
                                      "assets": [], "schema_version": 2},
                         "functions_installation": references[0 if label == "old" else 1]})
    assert requests[0]["manifest"]["generation_id"] == requests[1]["manifest"]["generation_id"]
    payload = tmp_path / "requests.json"
    payload.write_text(json.dumps(requests), encoding="utf-8")
    observed = _run(native_roles["core"]["executable"], """
import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from orchestrator.runtime_contract import load_core_runtime_selection,bind_process_runtime,enforce_runtime_contract,CORE_HOST_ROLE
from orchestrator.kernel_process import KernelRuntime
root=Path(sys.argv[1]);selected=load_core_runtime_selection(root)
bind_process_runtime(selected.core,code_root=root)
core=enforce_runtime_contract(root,role=CORE_HOST_ROLE)
releases=json.loads(Path(sys.argv[2]).read_text())
async def check():
    kernel=KernelRuntime(root,Path(sys.argv[3]),core,functions_runtime=selected.functions)
    await kernel.start(releases[0])
    before=await kernel.active.peer.request("execute")
    try:
        assert before["count"]==1 and before["dependency"]=="1"
        assert await kernel.replace(releases[1])
        after=await kernel.active.peer.request("inspect")
        assert after["count"]==1 and after["dependency"]=="2"
        assert not await kernel.replace(releases[2])
        rollback_new=await kernel.active.peer.request("inspect")
        assert rollback_new["count"]==1 and rollback_new["dependency"]=="2"
        assert await kernel.replace(releases[0])
        rollback=await kernel.active.peer.request("inspect")
        assert rollback["count"]==1 and rollback["dependency"]=="1"
        unchanged=enforce_runtime_contract(root,role=CORE_HOST_ROLE)
        assert unchanged==core
        print(json.dumps({"core_pid":os.getpid(),"core":core.to_dict(),
                          "before":before,"after":after,"rollback":rollback}))
    finally: await kernel.active.close()
asyncio.run(check())
""", payload, tmp_path / "instance", timeout=150, code_root=native_roles["code_root"])
    observed["CR_ID"] = RuntimeFingerprint.from_mapping(observed["core"]).runtime_id
    observed["FR_IDs"] = runtime_ids
    observed["FG_ID"] = requests[0]["manifest"]["generation_id"]
    observed["scope"] = "test-dependency mechanism; PR137 and full business parity remain unaccepted"
    (tmp_path / "functions-dependency-handoff.json").write_text(
        json.dumps(observed, indent=2), encoding="utf-8")
    assert observed["before"]["dependency"] == "1"
    assert observed["after"]["dependency"] == "2"
    assert observed["rollback"]["count"] == 1
    assert observed["before"]["prefix"] != observed["after"]["prefix"]
    assert observed["core"]["environment_prefix"] not in {
        observed["before"]["prefix"], observed["after"]["prefix"]}
    assert observed["core_pid"] not in {
        observed["before"]["pid"], observed["after"]["pid"], observed["rollback"]["pid"]}


def test_builder_refuses_to_touch_any_existing_environment(native_roles):
    with pytest.raises(ValueError, match="already exists"):
        build_reference_roles(ROOT, native_roles["reference_path"],
                              expected_digest=native_roles["reference_digest"],
                              destination=native_roles["destination"])


def _role_registry(native_roles, tmp_path):
    from types import SimpleNamespace
    from tools.registry import ToolRegistry
    home = tmp_path / "policy-instance"
    workspace = home / "workspaces/agent"
    workspace.mkdir(parents=True)
    config = SimpleNamespace(project_root=ROOT, bridge_home=home,
                             secrets_path=home/"secrets.json", instance_id="isolated-role-policy")
    owner = SimpleNamespace(core_runtime_fingerprint=RuntimeFingerprint.from_mapping(native_roles["core"]))
    registry = ToolRegistry(
        allowed_tools=["file_read", "file_write", "apply_patch", "shell", "verification_run"],
        access_root=tmp_path, access_roots=[tmp_path], workspace_dir=workspace, secrets={},
        audit_context={"global_config":config, "live_runtime_prefix":native_roles["functions"]["environment_prefix"],
                       "_runtime":SimpleNamespace(orchestrator=owner)})
    return registry, workspace


def _admit_role_write(registry, path):
    return registry._check_live_runtime_gate("file_write", {"path":str(path), "content":"never executed"},
                                            tool_call_id="isolated-role-policy")


def test_split_role_tool_denial_includes_core_bases_manifests_and_selector(native_roles, tmp_path):
    registry, workspace = _role_registry(native_roles, tmp_path)
    release = Path(native_roles["destination"])
    selector = Path(native_roles["selection"])
    record = json.loads(selector.read_bytes())
    targets = [
        Path(native_roles["core"]["environment_prefix"])/"pyvenv.cfg",
        Path(native_roles["functions"]["environment_prefix"])/"pyvenv.cfg",
        release/"base/BUILD", Path(record["core"]["path"]),
        Path(record["functions"]["path"]), selector,
    ]
    for path in targets:
        denied = _admit_role_write(registry, path)
        assert denied is not None and denied.is_error, path
        assert denied.details["reason"] == "live_runtime_protection", path
    assert _admit_role_write(registry, workspace/"ordinary.txt") is None
    independent = tmp_path/"independent-development/Scripts/python.exe"
    assert registry._check_live_runtime_gate(
        "shell", {"command":f'uv pip install --python "{independent}" sample'},
        tool_call_id="independent") is None
    assert registry._check_live_runtime_gate(
        "shell", {"command":f'uv pip install --python "{native_roles["core"]["executable"]}" sample'},
        tool_call_id="live-core") is not None
    assert registry._check_live_runtime_gate(
        "file_read", {"path":str(targets[0])}, tool_call_id="read-only") is None


def test_selector_change_protects_new_and_bound_old_fr_and_corruption_denies(native_roles, tmp_path, monkeypatch):
    from orchestrator.runtime_contract import qualified_runtime_from_reference
    import orchestrator.runtime_protection_targets as targets
    registry, workspace = _role_registry(native_roles, tmp_path)
    selector = Path(native_roles["selection"])
    previous = selector.read_bytes()
    record = json.loads(previous)
    bound = qualified_runtime_from_reference(record["functions"],code_root=ROOT)
    monkeypatch.setattr(targets,"current_process_runtime",lambda:bound)
    candidate = tmp_path/"new-functions"
    shutil.copytree(bound.fingerprint.environment_prefix,candidate)
    metadata = _site_packages(candidate)/"hashi_role_policy_fixture-2.dist-info"
    metadata.mkdir()
    (metadata/"METADATA").write_text("Name: hashi-role-policy-fixture\nVersion: 2\n",encoding="utf-8")
    newer = qualify_environment(Path(native_roles["code_root"]), candidate,
                                Path(native_roles["destination"])/"base",
                                tmp_path/"new-functions.json", FUNCTIONS_ROLE)
    assert newer.runtime_id != bound.runtime_id
    assert _admit_role_write(registry,workspace/"ordinary-before.txt") is None
    try:
        record["functions"] = runtime_installation_reference(newer)
        selector.write_text(json.dumps(record),encoding="utf-8")
        for target in (candidate/"pyvenv.cfg",newer.manifest_path,
                       Path(bound.fingerprint.environment_prefix)/"pyvenv.cfg",bound.manifest_path):
            result = _admit_role_write(registry,target)
            assert result is not None and result.is_error,target
            assert result.details["reason"] == "live_runtime_protection"
        assert _admit_role_write(registry,workspace/"ordinary-after.txt") is None
        selector.write_text('{"schema_version":1,"schema_version":1}',encoding="utf-8")
        denial = _admit_role_write(registry,workspace/"must-not-execute.txt")
        assert denial is not None and denial.is_error
        assert denial.details["operation"] == "runtime_qualification_unavailable"
        assert denial.details["tool_executed"] is False
        assert not (workspace/"must-not-execute.txt").exists()
    finally:
        selector.write_bytes(previous)
    assert selector.read_bytes() == previous


def test_functions_only_builder_never_writes_inside_existing_core_base(native_roles):
    destination = Path(native_roles["destination"]) / "base/forbidden-functions-build"
    core_reference = json.loads(Path(native_roles["selection"]).read_bytes())["core"]
    assert not destination.exists()
    with pytest.raises(ValueError, match="existing Python environment|Core installation"):
        build_reference_functions(
            Path(native_roles["code_root"]), native_roles["reference_path"],
            expected_digest=native_roles["reference_digest"], destination=destination,
            core_reference=core_reference)
    assert not destination.exists()


def test_management_launcher_uses_only_the_qualified_core(native_roles, monkeypatch):
    from scripts import hashi_instance_cli
    from tools.instance_registry import InstanceRegistryError

    code_root = Path(native_roles["code_root"])
    checker = code_root / "scripts/check_runtime_selection.py"
    checker.parent.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "scripts/check_runtime_selection.py", checker)
    monkeypatch.setattr(hashi_instance_cli, "_prepared_role_candidates",
                        lambda _root: [[native_roles["functions"]["executable"]]])
    command = hashi_instance_cli._select_core_runtime(code_root)
    assert command == [native_roles["core"]["executable"], "-I", "-B"]
    # The current test driver is not an installer-selected FR. Exact approved
    # Python/dependencies alone must not authorize it to choose a Core.
    monkeypatch.setattr(hashi_instance_cli, "_prepared_role_candidates",
                        lambda _root: [[sys.executable]])
    with pytest.raises(InstanceRegistryError, match="installer-qualified"):
        hashi_instance_cli._select_core_runtime(code_root)


def test_standalone_capsule_relocates_without_changing_role_bytes_or_ids(native_roles, tmp_path):
    from orchestrator.kernel_artifact import manifest_digest
    from orchestrator.runtime_contract import RuntimeContractError, qualified_runtime_from_reference
    from orchestrator.runtime_release_capsule import (
        create_capsule, materialize_release_payloads, pack_release_payloads,
        qualify_relocated_roles, verify_capsule, verify_installed_roles,
    )

    image = tmp_path / "built-image"
    code = image / "app/hashi"
    shutil.copytree(Path(native_roles["code_root"]), code)
    module = code / "orchestrator/capsule_fixture.py"
    module.write_text("VALUE = 'immutable capsule fixture'\n", encoding="utf-8")
    entries = [{"module": "orchestrator.capsule_fixture",
                "relative_path": "orchestrator/capsule_fixture.py",
                "sha256": hashlib.sha256(module.read_bytes()).hexdigest()}]
    manifest = {"schema_version": 2, "generation_id": manifest_digest(entries),
                "entries": entries, "assets": []}
    base = Path(native_roles["destination"]) / "base"
    for name in ("core", "functions"):
        environment = image / "runtime" / name
        shutil.copytree(base, environment)
        site = _site_packages(environment)
        if site.exists():
            assert not any(site.iterdir())
        else:
            site.mkdir(parents=True)
        if name == "functions":
            site.rmdir()
            shutil.copytree(_site_packages(native_roles["functions"]["environment_prefix"]), site)
    capsule = create_capsule(image, code, tmp_path / "build-observations",
                             function_manifest=manifest)
    original_ids = {name: record["runtime_id"] for name, record in capsule["roles"].items()}
    capsule = pack_release_payloads(image, code, roots=("runtime/core", "runtime/functions"))
    assert (image / "runtime/functions.payload.zip").is_file()
    assert not (image / "runtime/functions").exists()
    assert {name: record["runtime_id"] for name, record in capsule["roles"].items()} == original_ids
    serialized = json.dumps(capsule)
    assert str(tmp_path) not in serialized
    assert str(ROOT) not in serialized
    original_reference = {
        "path": str(tmp_path / "build-observations/core.json"),
        "digest": "sha256:" + hashlib.sha256(
            (tmp_path / "build-observations/core.json").read_bytes()).hexdigest()}
    moved = tmp_path / "relocated space 中文" / "image"
    moved.parent.mkdir()
    shutil.move(str(image), moved)
    code = moved / "app/hashi"
    with pytest.raises(RuntimeContractError):
        qualified_runtime_from_reference(original_reference, code_root=code, role=CORE_HOST_ROLE)
    with pytest.raises(RuntimeContractError, match="fully expanded"):
        qualify_relocated_roles(moved, code)
    materialize_release_payloads(moved, code)
    selection = qualify_relocated_roles(moved, code)
    assert verify_installed_roles(moved, code) == selection
    observed = _run(moved / ("runtime/core/python.exe" if os.name == "nt" else "runtime/core/bin/python3"),
                    """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from orchestrator.runtime_contract import load_core_runtime_selection,bind_process_runtime,enforce_runtime_contract,CORE_HOST_ROLE
root=Path(sys.argv[1]);selected=load_core_runtime_selection(root)
bind_process_runtime(selected.core,code_root=root)
core=enforce_runtime_contract(root,role=CORE_HOST_ROLE)
assert not any(name=="aiohttp" or name.startswith("aiohttp.") for name in sys.modules)
print(json.dumps({"core_id":selected.core.runtime_id,"fr_id":selected.functions.runtime_id,
                  "core_dependency_digest":core.dependency_digest}))
""", code_root=code)
    assert observed["core_id"] == capsule["roles"]["core"]["runtime_id"]
    assert observed["fr_id"] == capsule["roles"]["functions"]["runtime_id"]
    assert observed["core_dependency_digest"] == "sha256:" + hashlib.sha256(b"").hexdigest()
    policy = moved / "runtime/functions/runtime-policy/pyproject.toml"
    unchanged = policy.read_bytes()
    policy.write_bytes(unchanged + b"\n")
    try:
        with pytest.raises(RuntimeContractError, match="hash"):
            verify_capsule(moved, code)
    finally:
        policy.write_bytes(unchanged)
    capsule_path = moved / "runtime/role-capsule.json"
    original = capsule_path.read_bytes()
    invalid = json.loads(original)
    invalid["roles"]["functions"]["root"] = "../foreign-python"
    capsule_path.write_text(json.dumps(invalid), encoding="utf-8")
    try:
        with pytest.raises(RuntimeContractError, match="role/root"):
            verify_capsule(moved, code)
    finally:
        capsule_path.write_bytes(original)
    assert verify_installed_roles(moved, code) == selection
