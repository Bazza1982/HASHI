"""
Browser Bridge — auto-detect and connect to user's Chrome via CDP.

This module handles:
  - Detecting the WSL2 host IP (Windows gateway)
  - Probing Chrome CDP endpoints on configurable ports
  - Caching discovered CDP URL for reuse within the session
  - Health checking the connection
  - Comprehensive logging for debugging

Usage:
    from tools.browser_bridge import get_cdp_url, check_health

    cdp_url = get_cdp_url()       # Returns "http://<ip>:9222" or None
    health  = check_health()       # Returns dict with status info
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("hashi.browser_bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_CDP_PORT = 9222
_PROBE_TIMEOUT_S = 3
_STATE_FILE = Path(__file__).parent / "browser_bridge_state.json"

# Cache within process lifetime
_cached_cdp_url: Optional[str] = None
_cache_time: float = 0
_CACHE_TTL_S = 300  # re-probe every 5 minutes


# ---------------------------------------------------------------------------
# WSL2 host IP detection
# ---------------------------------------------------------------------------

def detect_wsl_host_ip() -> Optional[str]:
    """
    Detect the Windows host IP from inside WSL2.
    Tries multiple methods in order of reliability.
    """
    # Method 1: /proc/net/route default gateway
    try:
        with open("/proc/net/route") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    hex_ip = fields[2]
                    ip = ".".join(
                        str(int(hex_ip[i : i + 2], 16))
                        for i in range(6, -1, -2)
                    )
                    if ip and ip != "0.0.0.0":
                        logger.debug("WSL host IP from /proc/net/route: %s", ip)
                        return ip
    except (OSError, ValueError, IndexError):
        pass

    # Method 2: ip route show default
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for part in result.stdout.split():
            if part.count(".") == 3:
                logger.debug("WSL host IP from ip route: %s", part)
                return part
    except (subprocess.SubprocessError, OSError):
        pass

    # Method 3: /etc/resolv.conf nameserver
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.strip().startswith("nameserver"):
                    ip = line.strip().split()[-1]
                    if ip.count(".") == 3 and ip != "127.0.0.1":
                        logger.debug("WSL host IP from resolv.conf: %s", ip)
                        return ip
    except OSError:
        pass

    logger.warning("Could not detect WSL host IP")
    return None


def is_wsl() -> bool:
    """Check if we're running inside WSL."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CDP probing
# ---------------------------------------------------------------------------

def probe_cdp(host: str, port: int = _DEFAULT_CDP_PORT) -> Optional[dict]:
    """
    Probe a Chrome CDP endpoint. Returns version info dict or None.
    """
    url = f"http://{host}:{port}/json/version"
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                logger.info(
                    "CDP probe success: %s:%d — %s",
                    host, port, data.get("Browser", "unknown"),
                )
                return data
    except (URLError, OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("CDP probe failed for %s:%d — %s", host, port, e)
    return None


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def discover_cdp_url(
    port: int = _DEFAULT_CDP_PORT,
    force: bool = False,
) -> Optional[str]:
    """
    Auto-discover a usable CDP URL. Checks in order:
      1. HASHI_CDP_URL environment variable (explicit override)
      2. localhost (if Chrome is running locally or in same network namespace)
      3. WSL2 host IP (if running in WSL)

    Returns CDP URL string or None.
    """
    global _cached_cdp_url, _cache_time

    # Check env override first
    env_url = os.environ.get("HASHI_CDP_URL")
    if env_url:
        logger.info("Using HASHI_CDP_URL from environment: %s", env_url)
        return env_url

    # Use cache if fresh
    if not force and _cached_cdp_url and (time.time() - _cache_time) < _CACHE_TTL_S:
        logger.debug("Using cached CDP URL: %s", _cached_cdp_url)
        return _cached_cdp_url

    # Probe localhost first
    if probe_cdp("localhost", port):
        url = f"http://localhost:{port}"
        _cached_cdp_url = url
        _cache_time = time.time()
        _save_state(url)
        return url

    # If in WSL, try Windows host
    if is_wsl():
        host_ip = detect_wsl_host_ip()
        if host_ip and probe_cdp(host_ip, port):
            url = f"http://{host_ip}:{port}"
            _cached_cdp_url = url
            _cache_time = time.time()
            _save_state(url)
            return url

    logger.info("No CDP endpoint found (port %d)", port)
    _cached_cdp_url = None
    _save_state(None)
    return None


def get_cdp_url(port: int = _DEFAULT_CDP_PORT) -> Optional[str]:
    """
    Get the CDP URL, using cache when available.
    This is the primary function agents should call.
    """
    return discover_cdp_url(port=port)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_health(port: int = _DEFAULT_CDP_PORT) -> dict:
    """
    Return a health status dict for the browser bridge.
    Useful for diagnostics and bridge startup verification.
    """
    result: dict = {
        "status": "disconnected",
        "is_wsl": is_wsl(),
        "cdp_port": port,
        "env_override": os.environ.get("HASHI_CDP_URL"),
        "cached_url": _cached_cdp_url,
    }

    if is_wsl():
        result["wsl_host_ip"] = detect_wsl_host_ip()

    cdp_url = discover_cdp_url(port=port, force=True)
    if cdp_url:
        result["status"] = "connected"
        result["cdp_url"] = cdp_url
        info = probe_cdp(
            cdp_url.replace("http://", "").split(":")[0],
            port,
        )
        if info:
            result["browser"] = info.get("Browser")
            result["ws_url"] = info.get("webSocketDebuggerUrl")

    return result


# ---------------------------------------------------------------------------
# State persistence (session-level, gitignored)
# ---------------------------------------------------------------------------

def _save_state(cdp_url: Optional[str]) -> None:
    """Save discovered CDP URL to disk for other processes."""
    try:
        state = {
            "cdp_url": cdp_url,
            "discovered_at": time.time(),
            "is_wsl": is_wsl(),
        }
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        logger.debug("Could not save bridge state: %s", e)


def load_saved_state() -> Optional[str]:
    """Load previously discovered CDP URL from disk."""
    try:
        if not _STATE_FILE.exists():
            return None
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        url = state.get("cdp_url")
        age = time.time() - state.get("discovered_at", 0)
        if url and age < _CACHE_TTL_S:
            logger.debug("Loaded saved CDP URL: %s (age=%.0fs)", url, age)
            return url
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI for testing browser bridge discovery."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    import argparse
    parser = argparse.ArgumentParser(description="HASHI Browser Bridge")
    parser.add_argument("--port", type=int, default=_DEFAULT_CDP_PORT)
    parser.add_argument("--health", action="store_true", help="Run health check")
    args = parser.parse_args()

    if args.health:
        health = check_health(port=args.port)
        print(json.dumps(health, indent=2))
        return 0 if health["status"] == "connected" else 1

    url = discover_cdp_url(port=args.port, force=True)
    if url:
        print(f"CDP URL: {url}")
        return 0
    else:
        print("No CDP endpoint found.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
