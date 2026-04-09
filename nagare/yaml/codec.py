from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

import yaml


_CANONICAL_TOP_LEVEL_KEYS = {
    "workflow",
    "meta",
    "pre_flight",
    "agents",
    "steps",
    "error_handling",
    "success_criteria",
    "evaluation",
    "output",
    "x-nagare-viz",
}

_TOP_LEVEL_BLOCK_PATTERN = re.compile(r"(?m)^(?P<key>[A-Za-z0-9_-]+):(?!\S)")


@dataclass(frozen=True)
class FidelityWarning:
    code: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True)
class GraphValidationResult:
    duplicate_step_ids: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()
    missing_agents: tuple[str, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()

    @property
    def is_valid(self) -> bool:
        return not (
            self.duplicate_step_ids
            or self.missing_dependencies
            or self.missing_agents
            or self.cycles
        )


@dataclass
class WorkflowDocument:
    data: dict[str, Any]
    source: str
    workflow_path: Path | None = None
    compatibility_class: str = "A"
    warnings: list[FidelityWarning] = field(default_factory=list)
    unknown_top_level_keys: tuple[str, ...] = ()
    graph_validation: GraphValidationResult = field(default_factory=GraphValidationResult)

    def export(self, *, editor_metadata: dict[str, Any] | None = None) -> str:
        return export_workflow_document(self, editor_metadata=editor_metadata)


def load_workflow_file(path: str | Path) -> WorkflowDocument:
    workflow_path = Path(path)
    source = workflow_path.read_text(encoding="utf-8")
    return load_workflow_document(source, workflow_path=workflow_path)


def load_workflow_document(
    source: str,
    *,
    workflow_path: str | Path | None = None,
) -> WorkflowDocument:
    parsed = yaml.safe_load(source) or {}
    if not isinstance(parsed, dict):
        raise ValueError("Workflow YAML must deserialize to a mapping at the top level.")

    warnings: list[FidelityWarning] = []
    compatibility_class = "A"

    unknown_top_level_keys = tuple(
        key for key in parsed.keys() if key not in _CANONICAL_TOP_LEVEL_KEYS
    )
    if unknown_top_level_keys:
        warnings.append(
            FidelityWarning(
                code="unknown-top-level-fields",
                message=(
                    "Unknown top-level fields require preservation-aware export and are "
                    "not yet safe for arbitrary visual edits."
                ),
            )
        )
        compatibility_class = "B"

    if source_has_comments(source):
        warnings.append(
            FidelityWarning(
                code="comments-present",
                message=(
                    "The workflow contains comments. No-op export preserves them, but "
                    "non-textual edits need an explicit preservation path."
                ),
            )
        )
        compatibility_class = max_compatibility_class(compatibility_class, "B")

    if is_legacy_workflow_shape(parsed):
        warnings.append(
            FidelityWarning(
                code="legacy-dialect",
                message=(
                    "This workflow uses the legacy HASHI dialect and should stay in raw "
                    "YAML mode until a lossless migration path exists."
                ),
            )
        )
        compatibility_class = "C"

    graph_validation = validate_workflow_graph(parsed)
    if graph_validation.duplicate_step_ids:
        warnings.append(
            FidelityWarning(
                code="duplicate-step-ids",
                message="Duplicate step ids block safe export.",
                severity="error",
            )
        )
    if graph_validation.missing_dependencies:
        warnings.append(
            FidelityWarning(
                code="missing-dependencies",
                message="Some step dependencies do not resolve to a known step id.",
                severity="error",
            )
        )
    if graph_validation.missing_agents:
        warnings.append(
            FidelityWarning(
                code="missing-agents",
                message="Some steps reference agents that are not declared.",
                severity="error",
            )
        )
    if graph_validation.cycles:
        warnings.append(
            FidelityWarning(
                code="cycles-detected",
                message="The workflow graph contains dependency cycles.",
                severity="error",
            )
        )

    return WorkflowDocument(
        data=parsed,
        source=source,
        workflow_path=Path(workflow_path) if workflow_path is not None else None,
        compatibility_class=compatibility_class,
        warnings=warnings,
        unknown_top_level_keys=unknown_top_level_keys,
        graph_validation=graph_validation,
    )


def export_workflow_document(
    document: WorkflowDocument,
    *,
    editor_metadata: dict[str, Any] | None = None,
) -> str:
    if editor_metadata is None or editor_metadata == document.data.get("x-nagare-viz"):
        return document.source

    exported = replace_or_append_top_level_block(
        document.source,
        "x-nagare-viz",
        {"x-nagare-viz": editor_metadata},
    )
    document.data["x-nagare-viz"] = editor_metadata
    document.source = exported
    return exported


def validate_workflow_graph(workflow: dict[str, Any]) -> GraphValidationResult:
    steps = workflow.get("steps")
    agents_block = workflow.get("agents") or {}
    workers = agents_block.get("workers") or []
    worker_ids = {
        worker["id"]
        for worker in workers
        if isinstance(worker, dict) and isinstance(worker.get("id"), str)
    }

    if not isinstance(steps, list):
        return GraphValidationResult()

    duplicates: list[str] = []
    seen: set[str] = set()
    step_by_id: dict[str, dict[str, Any]] = {}

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str):
            continue
        if step_id in seen and step_id not in duplicates:
            duplicates.append(step_id)
        seen.add(step_id)
        step_by_id[step_id] = step

    missing_dependencies: set[str] = set()
    missing_agents: set[str] = set()
    adjacency: dict[str, list[str]] = {step_id: [] for step_id in step_by_id}

    for step_id, step in step_by_id.items():
        depends = step.get("depends") or []
        if isinstance(depends, list):
            for dependency in depends:
                if not isinstance(dependency, str):
                    continue
                if dependency not in step_by_id:
                    missing_dependencies.add(f"{step_id}->{dependency}")
                else:
                    adjacency[step_id].append(dependency)

        agent_id = step.get("agent")
        if isinstance(agent_id, str) and worker_ids and agent_id not in worker_ids:
            missing_agents.add(f"{step_id}->{agent_id}")

    cycles = detect_cycles(adjacency)

    return GraphValidationResult(
        duplicate_step_ids=tuple(sorted(duplicates)),
        missing_dependencies=tuple(sorted(missing_dependencies)),
        missing_agents=tuple(sorted(missing_agents)),
        cycles=tuple(tuple(cycle) for cycle in cycles),
    )


def detect_cycles(adjacency: dict[str, list[str]]) -> list[list[str]]:
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []
    cycles: list[list[str]] = []
    recorded: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle_start = path.index(node)
            cycle = path[cycle_start:] + [node]
            signature = tuple(cycle)
            if signature not in recorded:
                recorded.add(signature)
                cycles.append(cycle)
            return

        visiting.add(node)
        path.append(node)
        for dependency in adjacency.get(node, []):
            visit(dependency)
        path.pop()
        visiting.remove(node)
        visited.add(node)

    for node in adjacency:
        visit(node)

    return cycles


def source_has_comments(source: str) -> bool:
    return any(
        line.lstrip().startswith("#")
        for line in source.splitlines()
        if line.strip()
    )


def is_legacy_workflow_shape(parsed: dict[str, Any]) -> bool:
    return "workflow" not in parsed and ("tasks" in parsed or "workers" in parsed)


def replace_or_append_top_level_block(source: str, key: str, value: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(
        value,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip() + "\n"

    block_range = find_top_level_block_range(source, key)
    if block_range is None:
        if source.endswith("\n") or not source:
            separator = "" if not source or source.endswith("\n\n") else "\n"
            return f"{source}{separator}{rendered}"
        return f"{source}\n\n{rendered}"

    start, end = block_range
    prefix = source[:start]
    suffix = source[end:]
    if prefix and not prefix.endswith("\n"):
        prefix = f"{prefix}\n"
    return f"{prefix}{rendered}{suffix.lstrip(chr(10))}"


def find_top_level_block_range(source: str, key: str) -> tuple[int, int] | None:
    matches = list(_TOP_LEVEL_BLOCK_PATTERN.finditer(source))
    for index, match in enumerate(matches):
        if match.group("key") != key:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        return start, end
    return None


def max_compatibility_class(left: str, right: str) -> str:
    order = {"A": 0, "B": 1, "C": 2}
    return left if order[left] >= order[right] else right
