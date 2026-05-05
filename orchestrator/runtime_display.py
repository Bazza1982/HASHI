from __future__ import annotations


def _show_logo_animation():
    """Play the BRIDGE logo animation in the console (logo only, no status)."""
    from orchestrator.banner import show_startup_banner

    show_startup_banner(agent_names=[], logo_only=True)
