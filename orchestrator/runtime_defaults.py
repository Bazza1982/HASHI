"""Compatibility defaults used only when instance configuration is absent."""

DEFAULT_HASHI_REMOTE_PORT = 8766
DEFAULT_WORKBENCH_PORT = 18800
DEFAULT_WORKBENCH_URL = f"http://127.0.0.1:{DEFAULT_WORKBENCH_PORT}"
DEFAULT_WORKBENCH_LOCALHOST_URL = f"http://localhost:{DEFAULT_WORKBENCH_PORT}"

DEFAULT_API_GATEWAY_PORT = 18801

# Personal-instance Tool policy used when none is declared. Kept here so
# dependency-free frontends (the TUI rescue hatch) can read it without
# importing the server configuration stack.
DEFAULT_GLOBAL_ALLOWED_TOOLS = ("*",)
# HASHI-Fingerprint: hfp_ae026638b60d41b2b6ef1d5d40c8b5c6
