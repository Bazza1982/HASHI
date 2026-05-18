from __future__ import annotations

from orchestrator import banner


def test_wsl_defaults_to_ascii_banner(monkeypatch):
    monkeypatch.delenv("BRIDGE_FORCE_ASCII_BANNER", raising=False)
    monkeypatch.delenv("BRIDGE_ALLOW_UNICODE_BANNER", raising=False)
    monkeypatch.setattr(banner, "_running_under_wsl", lambda: True)

    assert banner._stdout_looks_unicode_safe() is False


def test_wsl_unicode_banner_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("BRIDGE_FORCE_ASCII_BANNER", raising=False)
    monkeypatch.setenv("BRIDGE_ALLOW_UNICODE_BANNER", "1")
    monkeypatch.setattr(banner, "_running_under_wsl", lambda: True)

    assert banner._stdout_looks_unicode_safe() is True


def test_force_ascii_overrides_unicode_opt_in(monkeypatch):
    monkeypatch.setenv("BRIDGE_FORCE_ASCII_BANNER", "1")
    monkeypatch.setenv("BRIDGE_ALLOW_UNICODE_BANNER", "1")
    monkeypatch.setattr(banner, "_running_under_wsl", lambda: True)

    assert banner._stdout_looks_unicode_safe() is False
