"""CI contract tests: scope, real Git baselines, direct consumers and failure propagation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("ci_plan", Path(__file__).with_name("ci_plan.py"))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

POLICY = {
    "global_paths": ["package-lock.json", "requirements.lock"],
    "static_paths": ["*.md", "docs/**", ".github/**"],
    "groups": {
        "guard": {"always": True},
        "server": {"paths": ["server/**"]},
        "ui": {"paths": ["src/**"]},
        "windows": {"paths": ["electron/**"]},
    },
}


class ScopeTests(unittest.TestCase):
    def test_documentation_does_not_start_product_or_native_checks(self):
        plan = ci.select(POLICY, ["README.md", "docs/setup.md"])
        self.assertFalse(plan["full"])
        self.assertEqual([k for k, v in plan["groups"].items() if v], ["guard"])

    def test_frontend_change_keeps_native_and_server_out(self):
        plan = ci.select(POLICY, ["src/theme.css"])
        self.assertTrue(plan["groups"]["ui"])
        self.assertFalse(plan["groups"]["windows"])
        self.assertFalse(plan["groups"]["server"])

    def test_shared_dependency_change_preserves_all_qualification(self):
        plan = ci.select(POLICY, ["package-lock.json"])
        self.assertTrue(plan["full"])
        self.assertTrue(all(plan["groups"].values()))

    def test_unowned_product_code_expands_instead_of_silently_skipping(self):
        self.assertTrue(ci.select(POLICY, ["new-engine/worker.py"])["full"])

    def test_explicit_full_candidate_includes_every_domain(self):
        self.assertTrue(all(ci.select(POLICY, ["README.md"], force=True)["groups"].values()))

    def test_ordinary_approval_label_only_retains_guard(self):
        result = ci.select(POLICY, ["server/router.js"], label_only=True)
        self.assertTrue(result["groups"]["guard"])
        self.assertFalse(result["groups"]["server"])

    def test_manifest_derived_core_paths_select_full(self):
        self.assertTrue(ci.select(POLICY, ["kernel.py"], core_paths=("kernel.py",))["full"])

    def test_changed_test_itself_is_owned(self):
        policy = {**POLICY, "groups": {"unit": {"paths": ["tests/**"]}}}
        self.assertTrue(ci.select(policy, ["tests/test_bug.py"])["groups"]["unit"])

    def test_single_star_cannot_accidentally_classify_nested_prompt_as_root_doc(self):
        self.assertTrue(ci.select(POLICY, ["engine/prompt.md"])["full"])

    def test_recursive_glob_accepts_zero_and_many_directories(self):
        self.assertTrue(ci.matches("src/theme.js", "src/**/*.js"))
        self.assertTrue(ci.matches("src/a/b/theme.js", "src/**/*.js"))
        self.assertFalse(ci.matches("src/a/theme.js", "src/*.js"))

    def test_invalid_repository_paths_are_errors(self):
        for path in ("../server.py", "/server.py", "a\\..\\server.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ci.select(POLICY, [path])


class SummaryTests(unittest.TestCase):
    def needs(self, result="success", selected="true"):
        return {"ci-plan": {"result": "success", "outputs": {"ui": selected}},
                "ui-tests": {"result": result}}

    def test_selected_success_and_explicit_non_applicability_pass(self):
        ci.check_results(self.needs(), {"ui-tests": "ui"})
        ci.check_results(self.needs("skipped", "false"), {"ui-tests": "ui"})

    def test_selected_failure_cancel_missing_or_skip_never_passes(self):
        for result in ("failure", "cancelled", "skipped", None):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                ci.check_results(self.needs(result), {"ui-tests": "ui"})

    def test_failed_selector_cannot_make_skipped_jobs_green(self):
        needs = self.needs("skipped", "false")
        needs["ci-plan"]["result"] = "failure"
        with self.assertRaises(RuntimeError):
            ci.check_results(needs, {"ui-tests": "ui"})

    def test_missing_selection_output_is_not_a_valid_plan(self):
        needs = self.needs("success")
        needs["ci-plan"]["outputs"] = {}
        with self.assertRaises(RuntimeError):
            ci.check_results(needs, {"ui-tests": "ui"})


class GitAndConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.command("init", "-q")
        self.command("config", "user.name", "CI contract fixture")
        self.command("config", "user.email", "ci-fixture@example.invalid")

    def command(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root).decode().strip()

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self):
        self.command("add", ".")
        self.command("commit", "-qm", "fixture")
        return self.command("rev-parse", "HEAD")

    def plan(self, event):
        self.write("event.json", json.dumps(event))
        self.write("policy.json", json.dumps(POLICY))
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(self.root / "event.json"),
                                   "GITHUB_EVENT_NAME": "pull_request"}):
            return ci.read_plan(self.root, self.root / "policy.json")

    def test_deleted_and_renamed_paths_keep_both_owners(self):
        self.write("server/router.js", "export const x = 1;")
        base = self.commit()
        (self.root / "server/router.js").unlink()
        self.write("src/router.js", "export const x = 1;")
        self.commit()
        plan = self.plan({"pull_request": {"base": {"sha": base}}})
        self.assertTrue(plan["groups"]["server"])
        self.assertTrue(plan["groups"]["ui"])

    def test_missing_baseline_fails_instead_of_claiming_no_changes(self):
        self.write("README.md", "fixture")
        self.commit()
        with self.assertRaises(subprocess.CalledProcessError):
            self.plan({"pull_request": {"base": {"sha": "1" * 40}}})

    def test_empty_initial_comparison_requires_full(self):
        self.write("README.md", "fixture")
        self.commit()
        self.assertTrue(self.plan({"before": "0" * 40})["full"])

    def test_package_description_only_is_static_but_dependency_change_is_not(self):
        self.write("package.json", json.dumps({"version": "1.0.0", "description": "old",
                                              "dependencies": {"x": "1"}}))
        base = self.commit()
        self.write("package.json", json.dumps({"version": "1.0.0", "description": "new",
                                              "dependencies": {"x": "1"}}))
        self.assertTrue(ci.semantic_metadata_only("package.json", base, self.root))
        self.write("package.json", json.dumps({"version": "1.0.0", "dependencies": {"x": "2"}}))
        self.assertFalse(ci.semantic_metadata_only("package.json", base, self.root))

    def test_python_transitive_consumer_keeps_related_test_only(self):
        self.write("app/a.py", "VALUE = 1")
        self.write("app/b.py", "from .a import VALUE")
        self.write("tests/test_b.py", "from app.b import VALUE")
        self.write("tests/test_other.py", "import json")
        files = self.command("ls-files", "--others", "--exclude-standard").splitlines()
        selected = ci.focused_files(self.root, files, ["app/a.py"],
                                    ["tests/test_b.py", "tests/test_other.py"], "python")
        self.assertEqual(selected, ["tests/test_b.py"])

    def test_javascript_shared_contract_selects_direct_and_transitive_consumers(self):
        self.write("shared/a.js", "export const value = 1")
        self.write("server/b.js", "export {value} from '../shared/a.js';")
        self.write("server/b.test.js", "import {value} from './b.js';")
        self.write("src/theme.test.js", "import assert from 'node:assert';")
        files = self.command("ls-files", "--others", "--exclude-standard").splitlines()
        self.assertEqual(ci.focused_files(self.root, files, ["shared/a.js"],
                        ["server/b.test.js", "src/theme.test.js"], "javascript"), ["server/b.test.js"])

    def test_new_server_tests_automatically_join_the_canonical_npm_scope(self):
        self.write("package.json", json.dumps({"scripts": {"test:server": "node --test server/*.test.js"}}))
        suites = {"server": {"npm_scripts": ["test:server"]}}
        first = ["server/existing.test.js"]
        self.assertEqual(ci.suite_candidates("server", suites, first, self.root), first)
        later = first + ["server/new_contract.test.js"]
        self.assertEqual(ci.suite_candidates("server", suites, later, self.root), later)

    def test_explicit_ui_pretests_survive_cross_job_deduplication(self):
        scripts = {"pretest:ui": "node --test src/foundation.test.js src/draft.test.js",
                   "test:ui": "node --test src/theme.test.js",
                   "test:draft": "node --test src/draft.test.js"}
        self.write("package.json", json.dumps({"scripts": scripts}))
        suites = {"core": {"npm_scripts": ["pretest:ui", "test:ui"], "exclude_suites": ["draft"]},
                  "draft": {"npm_scripts": ["test:draft"]}}
        files = ["src/foundation.test.js", "src/draft.test.js", "src/theme.test.js"]
        core = ci.suite_candidates("core", suites, files, self.root)
        draft = ci.suite_candidates("draft", suites, files, self.root)
        self.assertEqual(set(core + draft), set(files))
        self.assertFalse(set(core) & set(draft))

    def test_runner_cache_invalidates_when_tracked_imports_change(self):
        self.write("app/a.py", "VALUE = 1")
        self.write("app/c.py", "VALUE = 2")
        self.write("app/b.py", "from .a import VALUE")
        self.write("tests/test_a.py", "from app.a import VALUE")
        self.write("tests/test_b.py", "from app.b import VALUE")
        self.commit()
        files = self.command("ls-files").splitlines()
        candidates = ["tests/test_a.py", "tests/test_b.py"]
        runner = self.root / "runner"
        runner.mkdir()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(runner)}):
            self.assertEqual(ci.focused_files(self.root, files, ["app/a.py"],
                                             candidates, "python"), candidates)
            self.write("app/b.py", "from .c import VALUE")
            self.assertEqual(ci.focused_files(self.root, files, ["app/a.py"],
                                             candidates, "python"), ["tests/test_a.py"])

    def test_opaque_consumer_is_retained_alongside_a_static_consumer(self):
        self.write("app.py", "VALUE = 1")
        self.write("test_static.py", "from app import VALUE")
        self.write("test_dynamic.py", "import importlib\na = importlib.import_module('app')")
        candidates = ["test_static.py", "test_dynamic.py"]
        self.assertEqual(ci.focused_files(self.root, ["app.py"] + candidates, ["app.py"],
                                         candidates, "python"), candidates)

    def test_dynamic_or_unmodelled_import_uses_complete_component(self):
        self.write("app.py", "VALUE = 1")
        self.write("test_a.py", "import importlib\na = importlib.import_module('app')")
        self.write("test_b.py", "import json")
        candidates = ["test_a.py", "test_b.py"]
        self.assertEqual(ci.focused_files(self.root, ["app.py"] + candidates, ["app.py"],
                                         candidates, "python"), candidates)


class RepositoryPolicyTests(unittest.TestCase):
    def test_actual_repository_routing_examples(self):
        folder = Path(__file__).parent
        case_file = folder / "ci_examples.json"
        if not case_file.exists():
            self.skipTest("generic engine fixture without a repository policy")
        policy = json.loads((folder / "ci_policy.json").read_text(encoding="utf-8"))
        for case in json.loads(case_file.read_text(encoding="utf-8")):
            with self.subTest(paths=case["paths"]):
                result = ci.select(policy, case["paths"])
                self.assertEqual(result["full"], case["full"])
                for name in case.get("true", []):
                    self.assertTrue(result["groups"][name], name)
                for name in case.get("false", []):
                    self.assertFalse(result["groups"][name], name)
                if case["full"]:
                    self.assertTrue(all(result["groups"].values()))


if __name__ == "__main__":
    unittest.main()
# HASHI-Fingerprint: hfp_d0db7b0002c14d8684daedc27f7ea45b
