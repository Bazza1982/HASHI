from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tools.browser_extension_identity import (
    BrowserExtensionIdentityError,
    extension_id_for_public_key,
    load_or_create_identity,
)


def test_instance_extension_identity_is_stable_and_chrome_shaped(tmp_path):
    first = load_or_create_identity(tmp_path, "hashi3-abc123")
    second = load_or_create_identity(tmp_path, "hashi3-abc123")

    assert second == first
    assert len(first["extension_id"]) == 32
    assert set(first["extension_id"]) <= set("abcdefghijklmnop")
    assert base64.b64decode(first["manifest_key"], validate=True)


def test_extension_id_matches_public_key_digest(tmp_path):
    identity = load_or_create_identity(tmp_path, "hashi3-xyz789")
    public_key = base64.b64decode(identity["manifest_key"], validate=True)

    assert identity["extension_id"] == extension_id_for_public_key(public_key)


def test_extension_id_algorithm_matches_existing_chrome_identity():
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "chrome_extension"
        / "hashi_browser_bridge"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert extension_id_for_public_key(
        base64.b64decode(manifest["key"], validate=True)
    ) == "jdeaedmoejdapldleofeggedgenogpka"


def test_extension_identity_rejects_invalid_namespace(tmp_path):
    with pytest.raises(BrowserExtensionIdentityError, match="namespace"):
        load_or_create_identity(tmp_path, "HASHI 3/other")
