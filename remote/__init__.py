"""
Hashi Remote — LAN peer-to-peer communication and control for HASHI instances.

Born from Lily Remote (the first real project by Barry & XiaoLei 🌸),
repurposed and upgraded as an official HASHI component.

Architecture:
  - peer/     : Discovery abstraction (LAN mDNS + Tailscale)
  - api/      : FastAPI server (hchat relay, terminal exec, peer listing)
  - security/ : TLS, auth, pairing
  - terminal/ : Shell command execution with authorization levels
  - audit/    : Audit logging

Usage:
    python -m hashi.remote          # start with system tray
    python -m hashi.remote --no-tray  # headless mode
"""

__version__ = "1.0.0"
__author__ = "Barry Li & XiaoLei (小蕾) 🌸"
