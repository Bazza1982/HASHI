from __future__ import annotations

import json
from pathlib import Path

import pytest

from exp.loader import ExpStore


def _exp_store(tmp_path: Path) -> ExpStore:
    root = tmp_path / "exp"
    domain = root / "sample-user" / "office_desktop"
    playbooks = domain / "playbooks"
    playbooks.mkdir(parents=True)
    (domain / "manifest.json").write_text(
        json.dumps(
            {
                "id": "sample-user/office_desktop",
                "type": "exp",
                "owner": "sample-user",
                "summary": "Context-specific office automation guidance.",
                "playbooks": {"powerpoint": "playbooks/powerpoint.exp.md"},
            }
        ),
        encoding="utf-8",
    )
    (domain / "EXP.md").write_text("# Office desktop\n", encoding="utf-8")
    (playbooks / "powerpoint.exp.md").write_text(
        "# PowerPoint EXP\nAvoid large pasted text blocks.\n",
        encoding="utf-8",
    )
    return ExpStore(root)


def test_exp_store_lists_context_fixture(tmp_path: Path):
    store = _exp_store(tmp_path)

    assert store.list_ids() == ["sample-user/office_desktop"]


def test_exp_store_loads_manifest_and_playbook(tmp_path: Path):
    store = _exp_store(tmp_path)

    manifest = store.get_manifest("sample-user/office_desktop")
    playbook = store.get_playbook("sample-user/office_desktop", "powerpoint")

    assert manifest["type"] == "exp"
    assert manifest["owner"] == "sample-user"
    assert "Context-specific" in manifest["summary"]
    assert "PowerPoint EXP" in playbook
    assert "large pasted text blocks" in playbook


@pytest.mark.parametrize(
    "exp_id",
    ("../outside", r"..\outside/domain", "/absolute", "owner/../domain"),
)
def test_exp_store_rejects_ids_that_can_escape_the_root(
    tmp_path: Path,
    exp_id: str,
):
    store = _exp_store(tmp_path)

    with pytest.raises(ValueError, match="shape '<owner>/<domain>'"):
        store.get_manifest(exp_id)
