from tests.test_function_worker_supervisor import _verified_generation
from orchestrator import function_worker_supervisor as supervisor
from orchestrator.function_generation import VerifiedFunctionGeneration, verify_manifest_source_commit


def test_invalid_checkout_skips_old_artifact_scan_but_boot_fallback_still_verifies_it(tmp_path, monkeypatch):
    source = tmp_path / "source"
    home = tmp_path / "home"
    generation = _verified_generation(source)
    artifact = supervisor.materialize_generation_artifact(home, generation)
    supervisor.persist_qualified_generation_cache(home, generation, artifact)
    (source / "orchestrator/worker_demo.py").write_text("VALUE = 999\n")
    checked = []
    real_verify = supervisor.verify_generation_artifact

    def verify(root, candidate):
        checked.append(root)
        return real_verify(root, candidate)

    def reject_changed_checkout(self, expected):
        verify_manifest_source_commit(self.manifest, code_root=self.code_root)
        raise AssertionError("modified qualified checkout must be rejected")

    monkeypatch.setattr(VerifiedFunctionGeneration, "refresh_qualification_seal", reject_changed_checkout)
    monkeypatch.setattr(supervisor, "verify_generation_artifact", verify)
    assert supervisor.load_qualified_generation_cache(home, source, object()) is None
    assert checked == []
    monkeypatch.setattr(supervisor, "compare_function_candidate_runtime", lambda *a, **kw: None)
    fallback = supervisor.load_bootable_generation_cache(home, source, object())
    assert fallback is not None and fallback[1] == artifact
    assert checked == [artifact]
# HASHI-Fingerprint: hfp_aadc9fd2ad834f70804e1be488c5ab0e
