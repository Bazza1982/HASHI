from pathlib import Path
from types import SimpleNamespace

from orchestrator.agent_fyi import build_agent_fyi_primer, load_agent_fyi_text
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def test_refresh_rereads_reference_and_engineering_guidance(tmp_path):
    path = tmp_path / "docs" / "AGENT_FYI.md"
    path.parent.mkdir()
    path.write_text("old reference")
    (tmp_path / "AGENTS.md").write_text("engineering boundaries")
    original = build_agent_fyi_primer(path)
    path.write_text("new reference")
    refreshed = build_agent_fyi_primer(path)
    assert "new reference" in refreshed and "old reference" not in refreshed
    assert original.splitlines()[1] != refreshed.splitlines()[1]
    assert "engineering boundaries" in refreshed
    (tmp_path / "AGENTS.md").write_text("updated engineering boundaries")
    assert build_agent_fyi_primer(path).splitlines()[1] != refreshed.splitlines()[1]


def test_shipped_reference_and_rules_fit_without_truncation():
    root = Path(__file__).resolve().parents[1]
    path = root / "docs" / "AGENT_FYI.md"
    assert load_agent_fyi_text(path) == path.read_text().strip()
    assert "[fyi trimmed]" not in build_agent_fyi_primer(path)
    assert len(build_agent_fyi_primer(path)) <= 16000


def test_explicit_fyi_preserves_request_and_reports_actual_runtime(tmp_path):
    path = tmp_path / "AGENT_FYI.md"
    path.write_text("Current source reference")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.agent_fyi_path = path
    runtime.global_config = SimpleNamespace(instance_id="TEST")
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.backend_manager = SimpleNamespace(agent_mode="flex")
    prompt = runtime._build_fyi_request_prompt("Explain the pending change")
    assert "working mode: flex" in prompt
    assert "backend: codex-cli" in prompt
    assert prompt.endswith("--- CURRENT USER REQUEST — AUTHORITATIVE ---\nExplain the pending change")
