import json

import pytest

from scripts import backfill_codex_tokens as backfill
from tools import token_tracker


def seed_workspace(tmp_path, monkeypatch, records, usages):
    monkeypatch.setattr(backfill, "WORKSPACE_ROOT", tmp_path)
    workspace = tmp_path / "probe"
    workspace.mkdir()
    path = workspace / "token_usage.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    (workspace / "codex_exec_events.jsonl").write_text("".join(
        json.dumps({"type": "turn.completed", "usage": usage}) + "\n"
        for usage in usages
    ))
    return path


def test_backfill_preserves_unknown_provider_cost_and_shared_tier(tmp_path, monkeypatch):
    records = [
        {"backend": "codex-cli", "model": "gpt-5", "input": 1, "cost_usd": None},
        {"backend": "codex-cli", "model": "unlisted", "input": 1,
         "cost_usd": 0.0, "cost_source": "provider"},
        {"backend": "codex-cli", "model": "gpt-5.6-sol", "input": 1,
         "cost_usd": 0.1, "cost_source": "pricing_table"},
    ]
    usage = {"input_tokens": 300_000, "cached_input_tokens": 100_000,
             "output_tokens": 10_000}
    path = seed_workspace(tmp_path, monkeypatch, records, [usage] * len(records))
    before = path.read_bytes()
    preview = backfill.backfill_agent("probe", dry_run=True)
    assert path.read_bytes() == before
    assert not path.with_suffix(".jsonl.bak").exists()
    applied = backfill.backfill_agent("probe", dry_run=False)
    assert applied == preview
    assert path.with_suffix(".jsonl.bak").read_bytes() == before
    unknown, provider_zero, known = [json.loads(s) for s in path.read_text().splitlines()]
    assert (unknown["cost_usd"], unknown["cost_known"], unknown["cost_source"]) == (
        None, False, "unknown")
    assert (provider_zero["cost_usd"], provider_zero["cost_source"]) == (0.0, "provider")
    assert known["cost_usd"] == pytest.approx(0.99)
    assert known["cost_source"] == "pricing_table"
    assert known["pricing_revisions"] == [token_tracker.PRICING_REVISION]
    assert applied["new_unknown_cost_requests"] == 1
    assert token_tracker.get_summary(path.parent)["all_time"]["unknown_cost_requests"] == 1


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.4-future", "", None])
def test_backfill_does_not_invent_a_price_for_unknown_models(model):
    assert backfill.calc_cost_with_cache(1000, 100, 500, model) is None


def test_backfill_preview_reports_unknown_instead_of_zero(tmp_path, monkeypatch, capsys):
    path = seed_workspace(tmp_path, monkeypatch,
        [{"backend": "codex-cli", "input": 1, "cost_usd": 0.0}],
        [{"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 500}])
    before = path.read_bytes()
    monkeypatch.setattr("sys.argv", ["backfill_codex_tokens.py", "--dry-run"])
    backfill.main()
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith(("probe ", "TOTAL "))]
    assert len(lines) == 2
    assert all("unknown" in line.lower() and "n/a" in line.lower() for line in lines)
    assert path.read_bytes() == before
