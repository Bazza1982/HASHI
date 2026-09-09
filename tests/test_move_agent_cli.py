from __future__ import annotations

import sys

import pytest

from scripts import move_agent


def test_keep_source_compatibility_flag_stages_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        move_agent,
        "prepare_outbound_move",
        lambda *args, **kwargs: pytest.fail("retired copy mode staged a transfer"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["move_agent.py", "kasumi", "hashi3", "--keep-source"],
    )

    with pytest.raises(SystemExit):
        move_agent.main()

    assert "/clone" in capsys.readouterr().err
