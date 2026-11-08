"""Reference capture cannot write inside any source or Python environment."""
from pathlib import Path

import pytest

from orchestrator.runtime_installation_builder import snapshot_reference
from tests.test_runtime_installation import ROOT, installation


@pytest.mark.parametrize("location", ["environment", "base", "site"])
def test_reference_snapshot_never_modifies_an_existing_runtime(tmp_path, location):
    manifest, value, _, site = installation(tmp_path)
    targets = {"environment": manifest.parent, "base": Path(value["base_root"]), "site": site}
    output = targets[location] / "unsafe-reference.json"
    with pytest.raises(ValueError, match="outside|Python environment"):
        snapshot_reference(ROOT, base_root=Path(value["base_root"]),
                           functions_site=site, destination=output,
                           expected_dependency_digest=value["runtime"]["dependency_digest"])
    assert not output.exists()


def test_fresh_build_cannot_be_nested_inside_an_existing_python_environment(tmp_path):
    from orchestrator.runtime_installation_builder import build_reference_roles
    manifest, value, _, site = installation(tmp_path)
    reference = tmp_path / "safe-reference.json"
    digest = snapshot_reference(
        ROOT, base_root=Path(value["base_root"]), functions_site=site,
        destination=reference,
        expected_dependency_digest=value["runtime"]["dependency_digest"])
    destination = manifest.parent / "nested-new-runtime"
    with pytest.raises(ValueError, match="Python environment"):
        build_reference_roles(ROOT, reference, expected_digest=digest, destination=destination)
    assert not destination.exists()
