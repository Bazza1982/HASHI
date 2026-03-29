"""Security module — TLS, auth, and pairing for Hashi Remote."""

from .auth import verify_token, set_pairing_manager, set_lan_mode
from .tls import load_or_generate_cert
from .pairing import PairingManager

__all__ = ["verify_token", "set_pairing_manager", "set_lan_mode", "load_or_generate_cert", "PairingManager"]
