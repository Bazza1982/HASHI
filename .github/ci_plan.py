#!/usr/bin/env python3
"""Repository-local CI impact selection. Standard library only; unknown impact expands."""
from __future__ import annotations
import argparse
import ast
import fnmatch
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys

VERSION = 1
ROOT = Path(__file__).resolve().parent.parent
POLICY = Path(__file__).with_name("ci_policy.json")


def git(*args, root=ROOT):
    return subprocess.check_output(["git", *args], cwd=root).decode("utf-8")


@functools.lru_cache(maxsize=16384)
def matches(path, pattern):
    """Git-style segments: * never crosses /; ** may consume zero or more."""
    parts, pats = path.split("/"), pattern.split("/")
    @functools.lru_cache(maxsize=None)
    def walk(i, j):
        if j == len(pats):
            return i == len(parts)
        if pats[j] == "**":
            return walk(i, j + 1) or (i < len(parts) and walk(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], pats[j]) and walk(i + 1, j + 1)
    return walk(0, 0)


def any_path(path, patterns):
    return any(matches(path, p) for p in patterns)


def additive_test_registration(old, new, root):
    if any(key not in new for key in old):
        return False
    for key, command in new.items():
        if old.get(key) == command:
            continue
        if not key.startswith(("test:", "pretest:", "posttest:", "ci:")):
            return False
        def parts(value):
            args = shlex.split(value)
            if not args or args[0] != "node" or "--test" not in args:
                return None
            options = [p for p in args[1:] if p.startswith("-")]
            if any(p not in {"--test", "--test-concurrency=1"} for p in options):
                return None
            return options, [p for p in args[1:] if not p.startswith("-")]
        current = parts(command)
        previous = parts(old[key]) if key in old else None
        if current is None or not current[1]:
            return False
        if key in old and (previous is None or previous[0] != current[0] or
                           not set(previous[1]).issubset(current[1])):
            return False
        for path in current[1]:
            if path.startswith("/") or ".." in PurePosixPath(path).parts or "\\" in path:
                return False
            if not any(p.is_file() for p in root.glob(path)):
                raise RuntimeError("registered test entry has no source files: " + path)
    return True


def semantic_metadata_only(path, base, root=ROOT):
    if not base or path not in {"package.json", "pyproject.toml"}:
        return False
    try:
        before = git("show", base + ":" + path, root=root)
        after = (root / path).read_text(encoding="utf-8")
        if path.endswith(".json"):
            old, new = json.loads(before), json.loads(after)
            metadata = {"description", "author", "authors", "contributors", "maintainers",
                        "license", "repository", "homepage", "bugs", "keywords", "funding", "readme",
                        "version", "scripts"}
            previous = {k: v for k, v in old.items() if k not in metadata}
            current = {k: v for k, v in new.items() if k not in metadata}
            return (previous == current
                    and str(old.get("version", "")).split(".")[0] == str(new.get("version", "")).split(".")[0]
                    and additive_test_registration(old.get("scripts", {}), new.get("scripts", {}), root))
        import tomllib
        old, new = tomllib.loads(before), tomllib.loads(after)
        old_version = str(old.get("project", {}).get("version", ""))
        new_version = str(new.get("project", {}).get("version", ""))
        if old_version != new_version:
            if (old_version.split(".")[0] != new_version.split(".")[0] or
                not re.fullmatch(r"\d+(?:\.\d+){1,2}(?:(?:a|b|rc)\d+)?(?:\.post\d+|\.dev\d+)?(?:\+[A-Za-z0-9.]+)?", new_version)):
                return False
        for value in (old, new):
            value["project"] = {k: v for k, v in value.get("project", {}).items()
                                if k not in {"description", "authors", "maintainers", "readme", "urls",
                                             "keywords", "classifiers", "license", "version"}}
        return old == new
    except (ValueError, OSError, ImportError, subprocess.CalledProcessError):
        return False


def select(policy, changed, *, force=False, label_only=False, core_paths=()):
    groups = policy["groups"]
    selected = {name: bool(rule.get("always")) for name, rule in groups.items()}
    full = bool(force)
    reasons = ["explicit full candidate"] if force else []
    if label_only and not force:
        return {"full": False, "groups": selected, "changed": changed, "reasons": ["approval metadata only"]}
    for path in changed:
        if path.startswith("/") or ".." in PurePosixPath(path).parts or "\\" in path:
            raise ValueError("invalid repository-relative path")
        if path in core_paths or any_path(path, policy.get("global_paths", [])):
            full = True
            reasons.append("shared boundary/dependency: " + path)
            continue
        owned = False
        for name, rule in groups.items():
            if not rule.get("full_only") and any_path(path, rule.get("paths", [])):
                selected[name] = True
                owned = True
        if not owned and not any_path(path, policy.get("static_paths", [])):
            full = True
            reasons.append("unbounded impact: " + path)
    if full:
        selected = {name: True for name in groups}
    return {"full": full, "groups": selected, "changed": changed, "reasons": reasons}


def read_plan(root=ROOT, policy_path=POLICY):
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    inputs = event.get("inputs") or {}
    labels = [v["name"] for v in event.get("pull_request", {}).get("labels", [])]
    force = inputs.get("full") in (True, "true") or "ci-full" in labels
    action = event.get("action")
    label_only = event_name == "pull_request" and action in {"labeled", "unlabeled"}
    # Ordinary approval labels never rerun product tests. A requested full label does.
    if label_only and event.get("label", {}).get("name") != "ci-full":
        force = False
    base = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
    if base and base.strip("0"):
        # A missing baseline is an error, never an empty diff.
        git("cat-file", "-e", base + "^{commit}", root=root)
        changed = git("diff", "--name-only", "--no-renames", "-z", base, "HEAD", root=root).split("\0")
    else:
        changed = git("ls-files", "-z", root=root).split("\0")
        force = True  # First push/manual candidate without a comparison base.
    changed = [p for p in changed if p]
    changed = [p for p in changed if not semantic_metadata_only(p, base, root)]
    core_paths = ()
    if policy.get("core_manifest"):
        manifest = ast.parse((root / policy["core_manifest"]).read_text(encoding="utf-8"))
        assignment = next(n for n in manifest.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "CORE_SOURCE_PATHS" for t in n.targets))
        core_paths = ast.literal_eval(assignment.value)
    result = select(policy, changed, force=force, label_only=label_only, core_paths=core_paths)
    result["version"] = VERSION
    result["base"] = base
    result["head"] = git("rev-parse", "HEAD", root=root).strip()
    return result


def check_results(needs, expected):
    if needs.get("ci-plan", {}).get("result") != "success":
        raise RuntimeError("impact selection did not succeed")
    flags = needs["ci-plan"].get("outputs", {})
    for job, group in expected.items():
        result = needs.get(job, {}).get("result")
        if flags.get(group) not in {"true", "false"}:
            raise RuntimeError("missing or invalid selection: " + group)
        if flags.get(group) == "true":
            if result != "success":
                raise RuntimeError(job + ": required selected check " + str(result))
        elif result not in {"success", "skipped"}:
            raise RuntimeError(job + ": unexpected " + str(result))


def dependency_graph(root, files, language):
    graph = {}
    opaque = set()
    if language == "python":
        modules = {}
        for path in files:
            if path.endswith(".py"):
                name = path[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[:-9]
                modules[name] = path
                if name.startswith("backend."):
                    modules[name[8:]] = path
        for path in files:
            if not path.endswith(".py"):
                continue
            try:
                text = (root / path).read_text(encoding="utf-8-sig")
                tree = ast.parse(text)
            except (SyntaxError, OSError, UnicodeError):
                # Do not silently omit unreadable code from a selected component.
                return None
            if re.search(r"\b(?:importlib|runpy|subprocess)\b|__import__", text):
                opaque.add(path)
            refs = set()
            module = path[:-3].replace("/", ".")
            package = module.rsplit(".", 1)[0] if "/" in path else ""
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = node.module or ""
                    if node.level:
                        bits = package.split(".")
                        prefix = ".".join(bits[:len(bits) - node.level + 1] + ([prefix] if prefix else []))
                    names = [prefix] + [prefix + "." + a.name if prefix else a.name for a in node.names]
                for name in names:
                    if name in modules:
                        refs.add(modules[name])
            graph[path] = refs
    else:
        import_re = re.compile(r"""(?:\bfrom\s*|\bimport\s*(?:\(\s*)?|\brequire\s*\(\s*)['"]([^'"]+)['"]""")
        file_set = set(files)
        for path in files:
            if not path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")):
                continue
            try:
                text = (root / path).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                return None
            dynamic = re.findall(r"\b(?:import|require)\s*\(\s*([^\s])", text)
            if "child_process" in text or any(c not in {chr(34), chr(39)} for c in dynamic):
                opaque.add(path)
            refs = set()
            for spec in import_re.findall(text):
                if not spec.startswith("."):
                    continue
                target = os.path.normpath(str(PurePosixPath(path).parent / spec)).replace("\\", "/")
                for candidate in [target] + [target + ext for ext in
                       (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", "/index.js", "/index.ts")]:
                    if candidate in file_set:
                        refs.add(candidate)
                        break
            graph[path] = refs
    graph["@opaque"] = opaque
    return graph


def cached_graph(root, files, language):
    # A runner checkout is immutable; include tracked dirty bytes and inventory too.
    # Cache lives outside the source/qualified runtime tree.
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("RUNNER_TEMP"):
        return dependency_graph(root, files, language)
    try:
        fingerprint = (str(root.resolve()) + language + git("rev-parse", "HEAD", root=root)
                       + git("diff", "--no-ext-diff", "--binary", "HEAD", root=root)
                       + "\0".join(files) + Path(__file__).read_text(encoding="utf-8"))
        key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        folder = Path(os.environ["RUNNER_TEMP"]) / "ci-impact-cache-v1"
        folder.mkdir(exist_ok=True)
        cache = folder / (key + ".json")
        if cache.exists():
            return {k: set(v) for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
        graph = dependency_graph(root, files, language)
        if graph is not None:
            cache.write_text(json.dumps({k: sorted(v) for k, v in graph.items()}), encoding="utf-8")
        return graph
    except (OSError, ValueError, subprocess.CalledProcessError):
        return dependency_graph(root, files, language)


def focused_files(root, files, changed, candidates, language):
    if any(p.endswith(("conftest.py", "__init__.py")) for p in changed):
        return candidates
    graph = cached_graph(root, files, language)
    if graph is None:
        return candidates
    def expand(seed):
        affected = set(seed)
        while True:
            extra = {p for p, refs in graph.items() if refs & affected} - affected
            if not extra:
                return affected
            affected.update(extra)
    direct = expand(changed)
    if not any(p.split("::", 1)[0] in direct for p in candidates):
        return candidates
    # Retain opaque consumers alongside known direct/transitive consumers.
    affected = expand(direct | graph.get("@opaque", set()))
    return [p for p in candidates if p.split("::", 1)[0] in affected]



def suite_candidates(name, suites, files, root, stack=()):
    if name in stack:
        raise RuntimeError("cyclic component inventory")
    suite = suites[name]
    patterns = list(suite.get("tests", []))
    if suite.get("npm_scripts"):
        scripts = json.loads((root / "package.json").read_text(encoding="utf-8"))["scripts"]
        for key in suite["npm_scripts"]:
            args = shlex.split(scripts[key])
            if not args or args[0] != "node" or "--test" not in args:
                raise RuntimeError("component entry must be an explicit node test command: " + key)
            for arg in args[1:]:
                if arg.startswith("-"):
                    if arg not in {"--test", "--test-concurrency=1"}:
                        raise RuntimeError("review changed node test options: " + key)
                else:
                    patterns.append(arg)
    candidates = []
    for item in patterns:
        if any(c in item for c in "*?["):
            found = [p for p in files if matches(p, item)]
            if not found:
                raise RuntimeError("empty registered test pattern: " + item)
            candidates.extend(found)
        else:
            candidates.append(item)
    excluded = set()
    for other in suite.get("exclude_suites", []):
        excluded.update(suite_candidates(other, suites, files, root, stack + (name,)))
    return [p for p in dict.fromkeys(candidates) if p not in excluded]


def run_suite(name, root=ROOT):
    suites = json.loads(Path(__file__).with_name("ci_suites.json").read_text(encoding="utf-8"))
    suite = suites[name]
    plan = read_plan(root)
    files = git("ls-files", "-z", root=root).split("\0")
    candidates = suite_candidates(name, suites, files, root)
    if not candidates:
        raise RuntimeError("empty component test inventory: " + name)
    if not plan["full"] and suite.get("partitions"):
        allowed = set()
        for partition in suite["partitions"]:
            if any(any_path(p, partition["paths"]) for p in plan["changed"]):
                allowed.update(p for p in candidates if any_path(p.split("::", 1)[0], partition["tests"]))
        # Unknown cross-component impact retains the complete component inventory.
        if allowed:
            candidates = [p for p in candidates if p in allowed]
    selected = candidates if plan["full"] else focused_files(
        root, files, plan["changed"], candidates, suite["language"]
    )

    prefix = [sys.executable if x == "{python}" else x for x in suite["command"]]
    print(json.dumps({"suite": name, "selected": len(selected), "inventory": len(candidates), "full": plan["full"]}), flush=True)
    return subprocess.call(prefix + selected, cwd=root)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    check = sub.add_parser("summary")
    check.add_argument("--jobs", required=True, help="JSON mapping of job id to selection output")
    run = sub.add_parser("test")
    run.add_argument("suite")
    args = parser.parse_args()
    if args.command == "test":
        return run_suite(args.suite)
    if args.command == "summary":
        check_results(json.loads(os.environ["CI_NEEDS"]), json.loads(args.jobs))
        print("All selected checks passed; non-selected scopes are explicitly not applicable.")
        return 0
    plan = read_plan()
    flags = {**plan["groups"], "full": plan["full"]}
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as output:
        for key, value in flags.items():
            output.write(key + "=" + str(value).lower() + "\n")
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        native = plan["full"] or plan["groups"].get("windows", False)
        linux = policy.get("linux_runner", "ubuntu-latest")
        platforms = [linux, policy.get("windows_runner", "windows-latest")] if native else [linux]
        output.write("linux_os=" + json.dumps(platforms) + "\n")

        output.write("python_versions=" + json.dumps(["3.10", "3.11", "3.12"] if plan["full"] else ["3.12"]) + "\n")
        output.write("node_versions=" + json.dumps(["22", "24"] if plan["full"] else ["22"]) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Selected CI scope\n\n")
            stream.write("- Full candidate: " + str(plan["full"]).lower() + "\n")
            for key, value in plan["groups"].items():
                stream.write("- " + key + ": " + ("required" if value else "not applicable") + "\n")
            for reason in plan["reasons"]:
                stream.write("- Reason: " + reason.replace("\n", " ") + "\n")
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_f397c3db8d3646ca8f4161de69ec2c40
