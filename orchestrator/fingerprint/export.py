"""Selective, unsigned evidence bundles without private audit/log content."""
from __future__ import annotations

import json
from pathlib import Path
import zipfile

from .execution import observe
from .manifest import KIND, read_manifest, public_receipt, sidecar_for
from .models import canonical_json, relative_target
from .verify import verify, check_receipt

IDENTITY_KEYS = ("instance_id", "agent_id", "session_id", "run_id", "request_id")


def minimized_receipt(record: dict) -> dict:
    check_receipt(record)
    projected = public_receipt(record)
    projected["identity"] = {
        key: value if isinstance(value := record.get("identity", {}).get(key), str)
        and len(value) <= 256 and not any(char in value for char in ("/", "\\", "\n", "\r", "\x00")) else "redacted"
        for key in IDENTITY_KEYS
    }
    # Export coverage declarations, never arbitrary nested runtime metadata.
    projected["coverage"] = {
        "scope": "selected operation only", "complete": False,
        "external_editors_locked": False,
        "attribution": "controlled_observation" if record["evidence_grade"] == "controlled_execution" else "unknown",
    }
    for key in ("before", "after"):
        projected[key] = {field: record[key].get(field) for field in ("state", "sha256", "size")}
    return projected


def export_bundle(root: Path, output: Path, *, relative: str | None = None, include_content: bool = False) -> dict:
    root, output = Path(root).resolve(), Path(output).absolute()
    manifest = read_manifest(root)
    if manifest is None:
        raise ValueError("no fingerprint manifest")
    selected = [relative_target(relative)] if relative is not None else list(manifest["artifacts"])
    artifacts, receipts, contents = {}, {}, {}
    reserved = {".hashi-fingerprint/manifest.json", "fingerprint_readme.txt"}
    if relative is not None:
        reserved.add((selected[0] + ".hashi.json").casefold())
    seen = set(reserved)
    for target in selected:
        folded = target.casefold()
        if any(folded == other or folded.startswith(other + "/") or other.startswith(folded + "/") for other in seen) or folded.startswith(".hashi-fingerprint/"):
            raise ValueError("artifact collides with evidence bundle namespace")
        seen.add(folded)
    for target in selected:
        if target not in manifest["artifacts"]:
            raise ValueError("selected file has no evidence")
        source = manifest["artifacts"][target]
        from .verify import inspect_entry
        if inspect_entry(root, target, manifest)["evidence_link"] != "consistent":
            raise ValueError("selected artifact evidence links are corrupt")
        artifacts[target] = {key: source[key] for key in
            ("artifact_id", "state", "sha256", "latest_receipt", "receipts") if key in source}
        for event_id in source.get("receipts", []):
            receipt = minimized_receipt(manifest["receipts"][event_id])
            if receipt["event_id"] != event_id or receipt["relative_path"] != target:
                raise ValueError("invalid selected receipt link")
            receipts[event_id] = receipt
        if include_content and source["state"] == "present":
            path = root / target
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("content target escaped root")
            observation = observe(path)
            if observation.version.sha256 != source.get("sha256") or observation.data is None:
                raise ValueError("content version changed or exceeds bounded export size")
            contents[target] = observation.data
    portable = {"kind": KIND, "schema": 1, "root_id": manifest["root_id"],
        "authentication": "unsigned", "artifacts": artifacts, "receipts": receipts,
        "coverage": {"scope": "selected artifact versions only", "complete_repository": False,
                     "complete_audit_chain": False, "content_included": include_content}}
    instructions = (
        "HASHI Fingerprint evidence\n"
        "Use: python -m orchestrator.fingerprint verify <unpacked-project>\n"
        "Hashes compare exact bytes; links cover selected records only.\n"
        "This bundle is unsigned. It does not authenticate an author or prove a complete audit chain.\n"
        "Without content, supply the corresponding artifact files separately.\n"
    )
    # Exclusive creation preserves an unrelated existing output.
    with output.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(".hashi-fingerprint/manifest.json", canonical_json(portable) + b"\n")
            archive.writestr("FINGERPRINT_README.txt", instructions)
            for target, data in contents.items():
                archive.writestr(relative_target(target), data)
            if relative is not None:
                entry = artifacts[selected[0]]
                record = receipts[entry["latest_receipt"]]
                archive.writestr(selected[0] + ".hashi.json", canonical_json(sidecar_for(Path(selected[0]), record)) + b"\n")
    return {"status": "exported", "files": len(artifacts), "receipts": len(receipts),
            "content_included": include_content, "authentication": "unsigned", "coverage_complete": False}
