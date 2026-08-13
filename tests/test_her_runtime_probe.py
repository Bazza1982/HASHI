from __future__ import annotations

from types import SimpleNamespace

from scripts import her_runtime_probe


def test_provider_probe_accepts_max_plus_and_sets_native_iteration_ceiling(
    monkeypatch,
    tmp_path,
    capsys,
):
    captured: dict[str, object] = {}

    monkeypatch.setattr(her_runtime_probe, "find_claw_binary", lambda _path: tmp_path / "hashi-her")
    monkeypatch.setattr(
        her_runtime_probe,
        "_provider_env",
        lambda **_kwargs: ({}, {"base_url": "https://provider.invalid", "status": "stable"}),
    )

    def fake_run_claw_task(_cwd, _prompt, _model, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(
            text="HASHI_CLAW_SMOKE_OK",
            model="provider/model",
            duration_ms=1,
            iterations=1,
            tool_uses=[],
        )

    monkeypatch.setattr(her_runtime_probe, "run_claw_task", fake_run_claw_task)

    result = her_runtime_probe.main(
        [
            "--provider",
            "provider",
            "--model",
            "provider/model",
            "--effort",
            "max+",
            "--cwd",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert captured["CLAW_EXECUTION_EFFORT"] == "max+"
    assert captured["CLAW_TASK_PLANNING"] == "1"
    assert captured["CLAW_MAX_TOOL_ITERATIONS"] == "512"
    assert "HASHI_CLAW_SMOKE_OK" in capsys.readouterr().out
