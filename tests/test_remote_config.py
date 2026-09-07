from remote.main import _load_remote_config


def test_load_remote_config_parses_yaml_null_as_none(tmp_path):
    config_dir = tmp_path / "remote"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "security:\n"
        "  pairing_token_ttl_seconds: null\n",
        encoding="utf-8",
    )

    config = _load_remote_config(tmp_path)

    assert config["security"]["pairing_token_ttl_seconds"] is None


def test_load_remote_config_preserves_quoted_null_as_text(tmp_path):
    config_dir = tmp_path / "remote"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "security:\n"
        "  pairing_token_ttl_seconds: \"null\"\n",
        encoding="utf-8",
    )

    config = _load_remote_config(tmp_path)

    assert config["security"]["pairing_token_ttl_seconds"] == "null"
