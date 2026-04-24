from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_test_env import (
    find_loaded_extension_id,
    normalize_windows_like_path,
    update_native_host_allowed_origins,
)


def test_normalize_windows_like_path() -> None:
    assert (
        normalize_windows_like_path("C:/Users/thene/Desktop/HASHI_browser_bridge_recovery_extension/")
        == "c:\\users\\thene\\desktop\\hashi_browser_bridge_recovery_extension"
    )


def test_find_loaded_extension_id_by_extension_path(tmp_path: Path) -> None:
    secure_prefs = tmp_path / "Secure Preferences"
    secure_prefs.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        "abc123": {
                            "path": "C:\\Users\\thene\\Desktop\\HASHI_browser_bridge_extension",
                        },
                        "real456": {
                            "path": "C:\\Users\\thene\\Desktop\\HASHI_browser_bridge_recovery_extension",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    extension_id = find_loaded_extension_id(
        secure_prefs,
        "C:/Users/thene/Desktop/HASHI_browser_bridge_recovery_extension/",
    )
    assert extension_id == "real456"


def test_update_native_host_allowed_origins_preserves_and_dedupes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "com.hashi.browser_bridge.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "com.hashi.browser_bridge",
                "allowed_origins": [
                    "chrome-extension://oldid/",
                ],
            }
        ),
        encoding="utf-8",
    )

    result = update_native_host_allowed_origins(
        manifest_path,
        [
            "chrome-extension://oldid/",
            "chrome-extension://newid/",
        ],
    )

    assert result["allowed_origins"] == [
        "chrome-extension://oldid/",
        "chrome-extension://newid/",
    ]
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["allowed_origins"] == result["allowed_origins"]
