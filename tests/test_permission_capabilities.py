"""Coverage reports must not infer enforcement from a requested level."""
from dataclasses import replace

import pytest

from orchestrator.permissions.backend_capabilities import (
    CONTROLS,
    ActivePath,
    BackendBinding,
    ControlFinding,
    ServiceObservation,
    changed_controls,
    project_controls,
)
from orchestrator.permissions.models import PermissionLevel


@pytest.fixture
def evidence():
    binding = BackendBinding("instance", "herv3", "v1", "sandbox", "v2", "service-1", "rev-1")
    paths = (ActivePath("managed", CONTROLS),)
    findings = tuple(ControlFinding("managed", control, "enforced", "L2", "verified") for control in CONTROLS)
    return binding, paths, ServiceObservation(binding, 100.0, 130.0, True, findings)


def _project(evidence, **overrides):
    binding, paths, observation = evidence
    arguments = dict(
        requested_level=PermissionLevel.L2,
        binding=binding,
        active_paths=paths,
        inventory_complete=True,
        observation=observation,
        now=110.0,
    )
    arguments.update(overrides)
    return project_controls(**arguments)


def test_fresh_observations_cover_each_active_control(evidence):
    report = _project(evidence)
    assert [name for name, _ in report.controls] == list(CONTROLS)
    assert all(result.state == "enforced" and result.layer == "L2" for _, result in report.controls)


@pytest.mark.parametrize("field", [
    "instance_id", "backend_id", "backend_version", "executor_profile", "executor_version",
    "service_generation", "capability_revision",
])
def test_old_backend_executor_and_generation_evidence_cannot_claim_enforcement(evidence, field):
    binding, _, _ = evidence
    changed = replace(binding, **{field: "changed"})
    report = _project(evidence, binding=changed)
    assert all(result.state == "unknown" for _, result in report.controls)


@pytest.mark.parametrize("override", [
    {"inventory_complete": False},
    {"active_paths": ()},
    {"observation": None},
    {"now": 99.0},
    {"now": 130.0},
    {"now": 125.0, "max_age_seconds": 10.0},
    {"requested_level": PermissionLevel.L0},
])
def test_missing_stale_or_inapplicable_evidence_is_never_enforced(evidence, override):
    report = _project(evidence, **override)
    assert all(result.state == "unknown" for _, result in report.controls)


def test_unhealthy_service_cannot_reuse_its_previous_enforced_findings(evidence):
    _, _, observation = evidence
    report = _project(evidence, observation=replace(observation, healthy=False))
    assert all(result.state == "unavailable" for _, result in report.controls)


def test_mixed_native_channel_is_reported_as_partial_without_changing_backend(evidence):
    binding, paths, observation = evidence
    paths += (ActivePath("external-native", ("files", "scripts")),)
    findings = observation.findings + (
        ControlFinding("external-native", "files", "unmanaged", None, "Native file access is not managed."),
        ControlFinding("external-native", "scripts", "unmanaged", None, "Native process access is not managed."),
    )
    report = _project(evidence, active_paths=paths, observation=replace(observation, findings=findings))
    assert report.binding == binding
    assert report.control("files").state == "partial"
    assert report.control("scripts").state == "partial"
    assert report.control("network").state == "enforced"


def test_missing_one_active_path_result_remains_unknown(evidence):
    paths = evidence[1] + (ActivePath("external-native", ("files",)),)
    report = _project(evidence, active_paths=paths)
    assert report.control("files").state == "unknown"
    assert report.control("scripts").state == "enforced"


def test_l2_cannot_claim_a_path_or_dependency_enforced_only_at_l1(evidence):
    _, _, observation = evidence
    findings = tuple(
        replace(item, layer="L1") if item.control == "approval"
        else replace(item, dependencies=(("managed", "approval"),)) if item.control == "files"
        else item
        for item in observation.findings
    )
    report = _project(evidence, observation=replace(observation, findings=findings))
    assert report.control("approval").state == "partial"
    assert report.control("files").state == "partial"
    assert report.control("network").state == "enforced"


@pytest.mark.parametrize("dependency", [
    (("managed", "approval"),),
    (("missing", "approval"),),
])
def test_unknown_dependency_propagates_instead_of_claiming_enforced(evidence, dependency):
    _, _, observation = evidence
    findings = tuple(
        replace(item, dependencies=dependency) if item.control == "files"
        else replace(item, state="unknown", layer=None) if item.control == "approval"
        else item
        for item in observation.findings
    )
    report = _project(evidence, observation=replace(observation, findings=findings))
    assert report.control("files").state == "unknown"


def test_dependency_cycle_is_unknown(evidence):
    _, _, observation = evidence
    findings = tuple(
        replace(item, dependencies=(("managed", "approval"),)) if item.control == "files"
        else replace(item, dependencies=(("managed", "files"),)) if item.control == "approval"
        else item
        for item in observation.findings
    )
    report = _project(evidence, observation=replace(observation, findings=findings))
    assert report.control("files").state == "unknown"
    assert report.control("approval").state == "unknown"


def test_incomplete_proof_and_duplicate_evidence_fail_closed(evidence):
    binding, paths, observation = evidence
    with pytest.raises(ValueError):
        ActivePath("managed", ("files", "files"))
    with pytest.raises(ValueError):
        replace(observation, findings=observation.findings + (observation.findings[0],))
    with pytest.raises(ValueError):
        _project(evidence, active_paths=paths + paths)
    with pytest.raises(ValueError):
        _project(evidence, now=float("nan"))
    with pytest.raises(ValueError):
        _project(evidence, max_age_seconds=301)
    with pytest.raises(ValueError):
        ControlFinding("managed", "files", "enforced", None, "unverified")


def test_backend_change_explains_only_material_control_changes(evidence):
    original = _project(evidence)
    _, _, observation = evidence
    refreshed = _project(evidence, observation=replace(observation, observed_at=101.0), now=111.0)
    assert changed_controls(original, refreshed) == ()
    findings = tuple(
        replace(item, state="unmanaged", layer=None, reason="Native execution not managed.")
        if item.control == "scripts" else item for item in observation.findings
    )
    switched = _project(evidence, observation=replace(observation, findings=findings))
    assert changed_controls(original, switched) == ("scripts",)

@pytest.mark.parametrize("first", ["partial", "unknown"])
def test_all_dependencies_are_considered_independently_of_order(evidence, first):
    _, _, observation = evidence
    dependencies = (("managed", "scripts"), ("managed", "approval"))
    if first == "unknown":
        dependencies = tuple(reversed(dependencies))
    findings = tuple(
        replace(item, dependencies=dependencies) if item.control == "files"
        else replace(item, state="partial") if item.control == "scripts"
        else replace(item, state="unknown", layer=None) if item.control == "approval"
        else item
        for item in observation.findings
    )
    report = _project(evidence, observation=replace(observation, findings=findings))
    assert report.control("files").state == "unknown"
    assert "approval" in report.control("files").reason


def test_mixed_path_report_names_the_unmanaged_path(evidence):
    _, paths, observation = evidence
    paths += (ActivePath("external-native", ("files",)),)
    finding = ControlFinding(
        "external-native", "files", "unmanaged", None, "Native file access is not managed."
    )
    report = _project(evidence, active_paths=paths,
                      observation=replace(observation, findings=observation.findings + (finding,)))
    assert report.control("files").state == "partial"
    assert "external-native" in report.control("files").reason
    assert "Native file access" in report.control("files").reason

def test_required_unavailable_dependency_overrides_other_partial_dependencies(evidence):
    _, _, observation = evidence
    findings = tuple(
        replace(item, dependencies=(("managed", "scripts"), ("managed", "approval")))
        if item.control == "files"
        else replace(item, state="partial") if item.control == "scripts"
        else replace(item, state="unavailable", layer=None) if item.control == "approval"
        else item
        for item in observation.findings
    )
    report = _project(evidence, observation=replace(observation, findings=findings))
    assert report.control("files").state == "unavailable"
    assert "approval" in report.control("files").reason


def test_invalid_evidence_and_huge_clock_values_are_rejected(evidence):
    _, _, observation = evidence
    with pytest.raises(ValueError):
        replace(observation, observed_at=10 ** 1000)
    with pytest.raises(ValueError):
        _project(evidence, now=10 ** 1000)
    with pytest.raises(ValueError):
        ControlFinding("managed", "files", [], "L1", "invalid")
    with pytest.raises(ValueError):
        ControlFinding("managed", "files", "enforced", [], "invalid")
    with pytest.raises(ValueError):
        ActivePath("managed", (["files"],))

    with pytest.raises(ValueError):
        _project(evidence, active_paths=(ActivePath(str(i), ("files",)) for i in range(65)))
    with pytest.raises(ValueError):
        replace(observation, findings=observation.findings * 65)
    with pytest.raises(ValueError):
        ControlFinding("managed", "files", "partial", None, "unsafe\x1btext")
