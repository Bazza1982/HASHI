from pathlib import Path

from orchestrator.private_wol import describe_wol_targets, private_wol_available, run_private_wol


def _write_config(root: Path, text: str) -> None:
    private_dir = root / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    (private_dir / "wol_targets.json").write_text(text, encoding="utf-8")


def test_private_wol_available_when_hashi1_and_target_present(tmp_path: Path):
    _write_config(
        tmp_path,
        """
        {
          "allowed_instances": ["HASHI1"],
          "targets": {
            "msi": {
              "label": "MSI",
              "description": "Wake MSI",
              "runner": "command",
              "command": ["echo", "ok"]
            }
          }
        }
        """,
    )

    assert private_wol_available(tmp_path) is True
    assert describe_wol_targets(tmp_path) == [
        {"name": "msi", "label": "MSI", "description": "Wake MSI"}
    ]


def test_private_wol_rejects_unknown_target(tmp_path: Path):
    _write_config(
        tmp_path,
        """
        {
          "allowed_instances": ["HASHI1"],
          "targets": {
            "msi": {
              "label": "MSI",
              "runner": "command",
              "command": ["echo", "ok"]
            }
          }
        }
        """,
    )

    result = run_private_wol(tmp_path, "intel")
    assert result["ok"] is False
    assert result["available_targets"] == ["msi"]


def test_private_wol_runs_command_target(tmp_path: Path):
    _write_config(
        tmp_path,
        """
        {
          "allowed_instances": ["HASHI1"],
          "targets": {
            "msi": {
              "label": "MSI",
              "runner": "command",
              "command": ["echo", "magic-packet"]
            }
          }
        }
        """,
    )

    result = run_private_wol(tmp_path, "msi")
    assert result["ok"] is True
    assert "magic-packet" in result["stdout"]
