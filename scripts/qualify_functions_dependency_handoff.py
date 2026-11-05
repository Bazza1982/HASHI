#!/usr/bin/env python3
"""Qualify two sealed FRs under one newly launched, isolated Core controller.

No installed selector, existing Core, service, user home or real provider is
changed. The synthetic effect counter tests at-most-once handoff; optional full
product preparation imports the complete FG without activating its services.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("aiohttp", "certifi", "multidict", "pillow", "yarl")
PEER = r"""
import asyncio, importlib, importlib.metadata, io, json, os, ssl, sys
from pathlib import Path
from orchestrator.function_worker_protocol import JsonConnectionPeer
PACKAGES=("aiohttp","certifi","multidict","pillow","yarl")
async def serve(connection, bootstrap):
    stopping=asyncio.Event()
    state=Path(bootstrap["bridge_home"])/"synthetic-effect.json"
    state.parent.mkdir(parents=True,exist_ok=True)
    async def request(method, params):
        if method=="prepare":
            return {"pid":os.getpid(),"generation_id":bootstrap["manifest"]["generation_id"]}
        if method=="activate" and bootstrap["arguments"].get("reject_candidate") and not bootstrap.get("restore"):
            raise RuntimeError("intentional isolated candidate activation failure")
        if method=="execute":
            count=json.loads(state.read_text()) if state.exists() else 0
            state.write_text(json.dumps(count+1))
        if method=="stop": stopping.set()
        import certifi
        from PIL import Image
        image=Image.new("RGBA",(3,2),(17,43,89,127))
        payload=io.BytesIO()
        image.save(payload,format="PNG")
        payload.seek(0)
        with Image.open(payload) as recovered:
            recovered.load()
            assert recovered.tobytes()==image.tobytes()
        context=ssl.create_default_context(cafile=certifi.where())
        assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
        assert context.cert_store_stats()["x509_ca"]>0
        owners={name:str(Path(importlib.import_module("PIL" if name=="pillow" else name).__file__).resolve()) for name in PACKAGES}
        assert all(Path(path).is_relative_to(Path(sys.prefix).resolve()) for path in owners.values())
        return {"count":json.loads(state.read_text()) if state.exists() else 0,
                "pid":os.getpid(),"prefix":sys.prefix,"executable":sys.executable,
                "packages":{name:importlib.metadata.version(name) for name in PACKAGES},
                "import_owners":owners}
    peer=JsonConnectionPeer(connection,label="isolated-dependency-qualification",request_handler=request)
    peer.start()
    while not stopping.is_set() and not peer.is_closed: await asyncio.sleep(.01)
    await peer.close()
"""


async def run_controller(payload):
    # Only stdlib and protected Core are imported by this role.
    sys.path.insert(0, str(ROOT))
    from orchestrator.runtime_contract import (
        CORE_HOST_ROLE, bind_process_runtime, enforce_runtime_contract,
        qualified_runtime_from_reference, core_source_digest,
    )
    from orchestrator.kernel_process import KernelRuntime, ProcessClient, qualify

    core_installation = qualified_runtime_from_reference(
        payload["core"], code_root=ROOT, role=CORE_HOST_ROLE)
    bind_process_runtime(core_installation, code_root=ROOT)
    core = enforce_runtime_contract(ROOT, role=CORE_HOST_ROLE)
    assert core.dependency_digest == "sha256:" + hashlib.sha256(b"").hexdigest()
    source_digest = core_source_digest(ROOT)
    controller_pid = os.getpid()
    home = Path(payload["home"])
    kernel = KernelRuntime(ROOT, home, core)
    observations = {}
    if payload["prepare_product"]:
        product_ids = []
        for name in ("old", "new"):
            functions = qualified_runtime_from_reference(payload[name], code_root=ROOT)
            release = await qualify(ROOT, home / "product", core, functions_runtime=functions)
            product_kernel = KernelRuntime(ROOT, home / "product", core, functions_runtime=functions)
            client = await ProcessClient.prepare(product_kernel.bootstrap(release, initial=True))
            try:
                assert client.process.admission["runtime"] == functions.fingerprint.to_dict()
                product_ids.append(release["manifest"]["generation_id"])
                observations["product_" + name] = {
                    "ready": client.ready_metadata,
                    "modules": len(release["manifest"]["entries"]),
                    "resources": len(release["manifest"].get("assets", [])),
                    "functions_runtime_id": functions.runtime_id,
                    "activation_attempted": False,
                }
            finally:
                await client.close()
            assert not client.process.is_alive()
        assert product_ids[0] == product_ids[1]
    releases = payload["releases"]
    await kernel.start(releases[0])
    try:
        observations["old"] = await kernel.active.peer.request("execute")
        assert observations["old"]["count"] == 1
        assert await kernel.replace(releases[1])
        observations["new"] = await kernel.active.peer.request("inspect")
        assert observations["new"]["packages"] == payload["new_packages"]
        kernel.arguments = {"reject_candidate": True}
        assert not await kernel.replace(releases[1])
        kernel.arguments = {}
        observations["failed_candidate_rollback"] = await kernel.active.peer.request("inspect")
        assert observations["failed_candidate_rollback"]["packages"] == payload["new_packages"]
        assert await kernel.replace(releases[0])
        observations["downgrade"] = await kernel.active.peer.request("inspect")
        assert observations["downgrade"]["packages"] == payload["old_packages"]
        assert await kernel.replace(releases[1])
        observations["second_upgrade"] = await kernel.active.peer.request("inspect")
        for name in ("old", "new", "failed_candidate_rollback", "downgrade", "second_upgrade"):
            assert observations[name]["count"] == 1
            assert observations[name]["pid"] != controller_pid
        assert observations["old"]["packages"] == payload["old_packages"]
        assert observations["second_upgrade"]["packages"] == payload["new_packages"]
        assert observations["old"]["prefix"] != observations["new"]["prefix"]
        assert enforce_runtime_contract(ROOT, role=CORE_HOST_ROLE) == core
        assert core_source_digest(ROOT) == source_digest
        assert not any(name == "aiohttp" or name.startswith("aiohttp.") for name in sys.modules)
        result = {"core_pid": controller_pid, "core": core.to_dict(),
                  "core_source_digest": source_digest,
                  "old_fr_id": payload["old_fr_id"], "new_fr_id": payload["new_fr_id"],
                  "qualification_fg_id": releases[0]["manifest"]["generation_id"],
                  "observations": observations, "real_accounts_used": False,
                  "existing_core_or_selector_modified": False}
    finally:
        await kernel.active.close()
    result["owned_functions_retired"] = not kernel.active.process.is_alive()
    return result


def qualify_handoff(old_selection, candidate, work_root, *, prepare_product=False):
    sys.path.insert(0, str(ROOT))
    from orchestrator.runtime_contract import (
        CORE_HOST_ROLE, qualified_runtime_from_reference, compare_runtime_boundary,
    )
    from orchestrator.kernel_artifact import manifest_digest

    old_selection = json.loads(Path(old_selection).read_bytes())
    candidate = json.loads(Path(candidate).read_bytes())
    if candidate.get("automatic_adoption") is not False or candidate["core"] != old_selection["core"]:
        raise ValueError("Candidate must retain the exact existing Core qualification reference")
    core = qualified_runtime_from_reference(old_selection["core"], code_root=ROOT, role=CORE_HOST_ROLE)
    old = qualified_runtime_from_reference(old_selection["functions"], code_root=ROOT)
    new = qualified_runtime_from_reference(candidate["functions"], code_root=ROOT)
    compare_runtime_boundary(core.fingerprint, old.fingerprint, expected_functions=old.fingerprint)
    compare_runtime_boundary(core.fingerprint, new.fingerprint, expected_functions=new.fingerprint)
    if old.runtime_id == new.runtime_id:
        raise ValueError("An independent dependency handoff requires distinct FR identities")
    work_root = Path(work_root).absolute()
    protected = [ROOT]
    for installation in (core, old, new):
        record = json.loads(installation.manifest_path.read_bytes())
        protected.extend((Path(installation.fingerprint.environment_prefix), Path(record["base_root"])))
    if work_root.exists() or any(work_root.is_relative_to(p) or p.is_relative_to(work_root) for p in protected):
        raise ValueError("Qualification requires a new isolated directory outside program and runtimes")
    work_root.mkdir(parents=True)
    artifact = work_root / "function-code"
    module = artifact / "orchestrator/dependency_qualification.py"
    module.parent.mkdir(parents=True)
    module.write_text(PEER, encoding="utf-8")
    entries = [{"module": "orchestrator.dependency_qualification",
                "relative_path": "orchestrator/dependency_qualification.py",
                "sha256": hashlib.sha256(module.read_bytes()).hexdigest()}]
    manifest = {"generation_id": manifest_digest(entries), "entries": entries,
                "assets": [], "schema_version": 2}
    releases = [{"generation_root": str(artifact),
                 "entrypoint": "orchestrator.dependency_qualification:serve",
                 "manifest": manifest, "functions_installation": reference}
                for reference in (old_selection["functions"], candidate["functions"])]
    # Read package versions by the actual sealed metadata, not fabricated receipts.
    def packages(installation):
        from orchestrator.runtime_installation_builder import _metadata
        site = Path(installation.fingerprint.environment_prefix) / (
            "Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages")
        installed = _metadata(site)
        return {name: installed[name] for name in PACKAGES}
    payload = {"core": old_selection["core"], "old": old_selection["functions"],
               "new": candidate["functions"], "home": str(work_root / "instance"),
               "old_packages": packages(old), "new_packages": packages(new),
               "old_fr_id": old.runtime_id, "new_fr_id": new.runtime_id,
               "releases": releases, "prepare_product": prepare_product}
    if prepare_product:
        from orchestrator.pcm import render_pcm_document
        product = Path(payload["home"]) / "product"
        workspace = product / "workspaces/inactive_probe"
        workspace.mkdir(parents=True)
        (workspace / "agent.md").write_text(render_pcm_document(
            persona="Isolated qualification", system="No real provider or account use."), encoding="utf-8")
        (product / "agents.json").write_text(json.dumps({
            "global": {"instance_id": "isolated-fr-qualification"},
            "agents": [{"name": "inactive_probe", "type": "flex", "is_active": False,
                        "workspace_dir": str(workspace), "active_backend": "codex-cli",
                        "allowed_backends": [{"engine": "codex-cli"}]}]}), encoding="utf-8")
        (product / "secrets.json").write_text('{"placeholder":"offline-only"}', encoding="utf-8")
    payload_path = work_root / "input.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [core.fingerprint.executable, "-I", "-B", str(Path(__file__).resolve()),
         "--controller", str(payload_path)], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=1200)
    (work_root / "controller.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Isolated dependency qualification failed (exit {result.returncode}): "
                           + result.stderr[-3000:])
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    for reference, role in ((old_selection["core"], CORE_HOST_ROLE),
                            (old_selection["functions"], "functions"),
                            (candidate["functions"], "functions")):
        qualified_runtime_from_reference(reference, code_root=ROOT, role=role)
    output = work_root / "result.json"
    output.write_text(json.dumps(observed, indent=2), encoding="utf-8")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-selection", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--prepare-product", action="store_true")
    parser.add_argument("--controller", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.controller is not None:
        print(json.dumps(asyncio.run(run_controller(json.loads(args.controller.read_bytes())))), flush=True)
        return 0
    if any(value is None for value in (args.old_selection, args.candidate, args.work_root)):
        parser.error("--old-selection, --candidate and --work-root are required")
    print(qualify_handoff(args.old_selection, args.candidate, args.work_root,
                         prepare_product=args.prepare_product))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_a64376b004c7435d919f96de38b5c000
