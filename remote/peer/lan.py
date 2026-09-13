"""
LAN peer discovery using mDNS / Zeroconf.

Adapted from Lily Remote (agent/discovery/mdns.py) — the original by Barry & XiaoLei 🌸.
Service type changed from _lilyremote._tcp.local. to _hashi._tcp.local.

Each Hashi Remote instance advertises itself on the LAN, and discovers others.
No configuration needed — plug and play on the same network.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import socket
import time
from collections.abc import Mapping
from typing import Optional, Callable

from zeroconf import IPVersion, InterfaceChoice, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_PORT
from .base import PeerDiscovery, PeerInfo, is_valid_instance_id, normalize_instance_id

logger = logging.getLogger(__name__)

HASHI_SERVICE_TYPE = "_hashi._tcp.local."
HASHI_REMOTE_VERSION = "1.0.0"
_SCOPE_TO_CODE = {
    "same_host": "s",
    "lan": "l",
    "overlay": "o",
    "host_virtual": "v",
    "routable": "r",
    "peer": "p",
}
_CODE_TO_SCOPE = {value: key for key, value in _SCOPE_TO_CODE.items()}
_MDNS_TXT_RECORD_MAX_BYTES = 255
_MDNS_TXT_FIELD_MAX_BYTES = 16 * 1024
_MDNS_TXT_FIELD_MAX_CHUNKS = 96
_MDNS_TXT_TOTAL_MAX_BYTES = 8 * 1024
_MDNS_TXT_HINT_MAX_BYTES = 160
_MDNS_TXT_CHUNK_VERSION = 1
_MDNS_ROUTE_HINT_LIMIT = 4


class TxtRecordDecodeError(ValueError):
    """A versioned DNS-SD TXT field is incomplete, ambiguous, or invalid."""


def _metadata_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bounded_utf8_hint(value: object, max_bytes: int = _MDNS_TXT_HINT_MAX_BYTES) -> str:
    """Return a valid UTF-8 prefix suitable for fixed-size discovery hints."""
    payload = str(value or "").encode("utf-8")
    if len(payload) <= max_bytes:
        return payload.decode("utf-8")
    return payload[:max_bytes].decode("utf-8", errors="ignore")


def _split_utf8_for_txt(base_key: str, value: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in value:
        encoded = character.encode("utf-8")
        index = len(chunks)
        key = f"{base_key}_{index}"
        budget = _MDNS_TXT_RECORD_MAX_BYTES - len(key.encode("utf-8")) - 1
        if budget <= 0 or len(encoded) > budget:
            raise ValueError(f"TXT key is too long: {base_key}")
        if current and current_bytes + len(encoded) > budget:
            chunks.append("".join(current))
            if len(chunks) >= _MDNS_TXT_FIELD_MAX_CHUNKS:
                raise ValueError(f"TXT field has too many chunks: {base_key}")
            current = []
            current_bytes = 0
            index = len(chunks)
            key = f"{base_key}_{index}"
            budget = _MDNS_TXT_RECORD_MAX_BYTES - len(key.encode("utf-8")) - 1
        current.append(character)
        current_bytes += len(encoded)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def _encode_versioned_txt_records(base_key: str, value: str) -> dict[str, str]:
    """Encode one UTF-8 value with bounded, checksummed, order-free fragments."""
    text = str(value or "")
    payload = text.encode("utf-8")
    if len(payload) > _MDNS_TXT_FIELD_MAX_BYTES:
        raise ValueError(
            f"TXT field {base_key!r} exceeds {_MDNS_TXT_FIELD_MAX_BYTES} bytes"
        )
    chunks = _split_utf8_for_txt(base_key, text)
    manifest = json.dumps(
        {
            "v": _MDNS_TXT_CHUNK_VERSION,
            "n": len(chunks),
            "l": len(payload),
            "h": hashlib.sha256(payload).hexdigest(),
        },
        separators=(",", ":"),
    )
    meta_key = f"{base_key}_meta"
    if _txt_record_size(meta_key, manifest) > _MDNS_TXT_RECORD_MAX_BYTES:
        raise ValueError(f"TXT manifest key is too long: {base_key}")
    records = {meta_key: manifest}
    records.update({f"{base_key}_{index}": chunk for index, chunk in enumerate(chunks)})
    return records


def _decode_versioned_txt_records(
    properties: Mapping[str, str],
    base_key: str,
) -> str:
    """Decode a checksummed TXT field and reject gaps or ambiguous indices."""
    meta_key = f"{base_key}_meta"
    if meta_key not in properties:
        raise TxtRecordDecodeError(f"missing {base_key} manifest")
    try:
        manifest = json.loads(str(properties.get(meta_key) or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TxtRecordDecodeError(f"invalid {base_key} manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("v") != _MDNS_TXT_CHUNK_VERSION:
        raise TxtRecordDecodeError(f"unsupported {base_key} version")
    try:
        count = int(manifest.get("n"))
        declared_length = int(manifest.get("l"))
    except (TypeError, ValueError) as exc:
        raise TxtRecordDecodeError(f"invalid {base_key} bounds") from exc
    checksum = str(manifest.get("h") or "").lower()
    if not 1 <= count <= _MDNS_TXT_FIELD_MAX_CHUNKS:
        raise TxtRecordDecodeError(f"invalid {base_key} chunk count")
    if not 0 <= declared_length <= _MDNS_TXT_FIELD_MAX_BYTES:
        raise TxtRecordDecodeError(f"invalid {base_key} declared length")
    if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
        raise TxtRecordDecodeError(f"invalid {base_key} checksum")

    fragments: dict[int, str] = {}
    prefix = f"{base_key}_"
    for raw_key, raw_value in properties.items():
        key = str(raw_key)
        if not key.startswith(prefix) or key == meta_key:
            continue
        suffix = key[len(prefix):]
        if not suffix.isdigit():
            continue
        index = int(suffix)
        if index in fragments:
            raise TxtRecordDecodeError(f"duplicate {base_key} chunk {index}")
        if index < 0 or index >= count:
            raise TxtRecordDecodeError(f"unexpected {base_key} chunk {index}")
        fragments[index] = str(raw_value or "")
    if set(fragments) != set(range(count)):
        raise TxtRecordDecodeError(f"missing {base_key} chunk")
    text = "".join(fragments[index] for index in range(count))
    payload = text.encode("utf-8")
    if len(payload) != declared_length:
        raise TxtRecordDecodeError(f"{base_key} length mismatch")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), checksum):
        raise TxtRecordDecodeError(f"{base_key} checksum mismatch")
    return text


def _encode_txt_value_records(base_key: str, value: object) -> dict[str, str]:
    text = str(value or "")
    if _txt_record_size(base_key, text) <= _MDNS_TXT_RECORD_MAX_BYTES:
        return {base_key: text}
    return _encode_versioned_txt_records(base_key, text)


def _decode_txt_value(properties: Mapping[str, str], base_key: str, default: str = "") -> str:
    if f"{base_key}_meta" in properties:
        return _decode_versioned_txt_records(properties, base_key)
    if base_key not in properties:
        return default
    value = str(properties.get(base_key) or "")
    if _txt_record_size(base_key, value) > _MDNS_TXT_RECORD_MAX_BYTES:
        raise TxtRecordDecodeError(f"oversized legacy {base_key} record")
    return value


def _decode_service_properties(raw_properties: Mapping[object, object]) -> dict[str, str]:
    properties: dict[str, str] = {}
    total_size = 0
    for raw_key, raw_value in (raw_properties or {}).items():
        key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
        if key in properties:
            raise TxtRecordDecodeError(f"duplicate TXT key: {key}")
        value = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else str(raw_value)
        record_size = _txt_record_size(key, value)
        if record_size > _MDNS_TXT_RECORD_MAX_BYTES:
            raise TxtRecordDecodeError(f"oversized TXT record: {key}")
        total_size += 1 + record_size
        if total_size > _MDNS_TXT_TOTAL_MAX_BYTES:
            raise TxtRecordDecodeError("mDNS TXT payload exceeds aggregate byte budget")
        properties[key] = value
    return properties


def _route_hints(items: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        host = _bounded_utf8_hint(
            str(item.get("host") or "").strip(),
            _MDNS_TXT_HINT_MAX_BYTES,
        )
        if not host or host in seen:
            continue
        seen.add(host)
        result.append({**item, "host": host})
        if len(result) >= _MDNS_ROUTE_HINT_LIMIT:
            break
    return result


def _normalize_host_identity(value: str) -> str:
    value = str(value or "").strip().lower()
    return "".join(ch for ch in value if ch.isalnum())


def _encode_candidate_records(items: list[dict]) -> str:
    packed = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        scope = str(item.get("scope") or "").strip()
        source = str(item.get("source") or "").strip()
        if not host:
            continue
        packed.append([host, _SCOPE_TO_CODE.get(scope, scope[:1] or "?"), source[:2] or "?"])
    return json.dumps(packed, separators=(",", ":"))


def _decode_candidate_records(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    items: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip()
            source = str(item.get("source") or "").strip()
        elif isinstance(item, list) and item:
            host = str(item[0] or "").strip() if len(item) >= 1 else ""
            scope = _CODE_TO_SCOPE.get(str(item[1] or "").strip(), str(item[1] or "").strip()) if len(item) >= 2 else ""
            source = str(item[2] or "").strip() if len(item) >= 3 else ""
        else:
            continue
        if host:
            items.append({"host": host, "scope": scope or "unknown", "source": source or "peer"})
    return items


def _txt_record_size(key: str, value: str) -> int:
    return len(str(key).encode("utf-8")) + 1 + len(str(value).encode("utf-8"))


def _encode_csv_txt_records(base_key: str, values: list[str]) -> dict[str, str]:
    """Encode a growing CSV field as bounded DNS-SD TXT character-strings."""
    records: dict[str, str] = {}
    chunk: list[str] = []
    chunk_index = 0
    seen: set[str] = set()

    def record_key() -> str:
        return base_key if chunk_index == 0 else f"{base_key}_{chunk_index}"

    for raw_value in values or []:
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        candidate = ",".join([*chunk, value])
        if _txt_record_size(record_key(), candidate) <= _MDNS_TXT_RECORD_MAX_BYTES:
            chunk.append(value)
            continue

        if chunk:
            records[record_key()] = ",".join(chunk)
            chunk_index += 1
            chunk = []
        if _txt_record_size(record_key(), value) > _MDNS_TXT_RECORD_MAX_BYTES:
            logger.warning(
                "LanDiscovery: omitted oversized %s item from mDNS metadata (%d bytes)",
                base_key,
                len(value.encode("utf-8")),
            )
            continue
        chunk.append(value)

    if chunk:
        records[record_key()] = ",".join(chunk)
    return records


def _decode_csv_txt_records(properties: dict[str, str], base_key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    chunk_index = 0
    while True:
        key = base_key if chunk_index == 0 else f"{base_key}_{chunk_index}"
        if key not in properties:
            break
        for raw_value in str(properties.get(key) or "").split(","):
            value = raw_value.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
        chunk_index += 1
    return values


def _is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _pick_best_peer_host(
    parsed_addresses: list[str],
    address_candidates: list[dict],
    observed_candidates: list[dict],
    fallback_host: str,
) -> str:
    preferred_scopes = {"lan", "overlay", "routable", "peer"}

    for host in parsed_addresses or []:
        host = str(host or "").strip()
        if host and not _is_loopback_host(host):
            return host

    for items in (observed_candidates, address_candidates):
        for item in items or []:
            host = str((item or {}).get("host") or "").strip()
            scope = str((item or {}).get("scope") or "").strip().lower()
            if host and scope in preferred_scopes and not _is_loopback_host(host):
                return host

    for host in parsed_addresses or []:
        host = str(host or "").strip()
        if host:
            return host

    return str(fallback_host or "127.0.0.1")


def _get_local_ip() -> str:
    candidates, _observed = _collect_ipv4_candidates()
    for item in candidates:
        if str(item.get("scope") or "") in {"lan", "overlay", "routable"}:
            return str(item.get("host") or "127.0.0.1")
    return "127.0.0.1"


def _classify_address_scope(ip_str: str, adapter_name: str = "") -> str:
    import ipaddress

    try:
        addr = ipaddress.IPv4Address(ip_str)
    except Exception:
        return "unknown"
    if addr.is_unspecified or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return "unknown"
    if str(ip_str).startswith("255."):
        return "unknown"
    if addr.is_loopback:
        return "same_host"
    adapter_hint = str(adapter_name or "").strip().lower()
    if adapter_hint.startswith("lo"):
        return "host_virtual"
    if adapter_hint and any(token in adapter_hint for token in ("wsl", "hyper-v", "vethernet", "virtualbox", "vmware", "host-only")):
        return "host_virtual"
    if addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return "overlay"
    if addr.is_private:
        return "lan"
    return "routable"


def _collect_ipv4_candidates() -> tuple[list[dict], list[dict]]:
    import ipaddress
    import subprocess

    seen: set[str] = set()
    observed_seen: set[tuple[str, str]] = set()
    routing_items: list[tuple[int, dict]] = []
    observed_items: list[tuple[int, dict]] = []

    def _priority(host: str, scope: str) -> int:
        try:
            addr = ipaddress.IPv4Address(host)
        except Exception:
            return 90
        if addr.is_loopback:
            return 0
        if scope == "lan" and addr in ipaddress.IPv4Network("192.168.0.0/16"):
            return 10
        if scope == "lan" and addr in ipaddress.IPv4Network("10.0.0.0/8"):
            return 20
        if scope == "host_virtual":
            return 30
        if scope == "lan" and addr in ipaddress.IPv4Network("172.16.0.0/12"):
            return 35
        if scope == "overlay":
            return 40
        return 50

    def _record_observed(host: str, scope: str, source: str) -> None:
        key = (host, scope)
        if key in observed_seen:
            return
        observed_seen.add(key)
        observed_items.append(
            (
                _priority(host, scope),
                {
                    "host": host,
                    "scope": scope,
                    "source": source,
                },
            )
        )

    def _add(host: str, source: str, adapter_name: str = "") -> None:
        host = str(host or "").strip()
        if not host or host in seen:
            return
        scope = _classify_address_scope(host, adapter_name=adapter_name)
        if scope == "unknown":
            return
        _record_observed(host, scope, source)
        if scope == "host_virtual":
            return
        seen.add(host)
        routing_items.append(
            (
                _priority(host, scope),
                {
                    "host": host,
                    "scope": scope,
                    "source": source,
                },
            )
        )

    _add("127.0.0.1", "loopback")

    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            adapter_name = str(getattr(adapter, "nice_name", "") or getattr(adapter, "name", "") or "")
            for addr in adapter.ips:
                if isinstance(addr.ip, str):
                    _add(addr.ip, "interface_scan", adapter_name=adapter_name)
    except Exception:
        pass

    try:
        result = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=3)
        current_adapter = ""
        for line in result.stdout.splitlines():
            if line and not line.startswith(" "):
                try:
                    current_adapter = line.split(":", 2)[1].strip()
                except Exception:
                    current_adapter = ""
                continue
            line = line.strip()
            if line.startswith("inet "):
                _add(line.split()[1].split("/")[0], "ip_addr_show", adapter_name=current_adapter)
    except Exception:
        pass

    routing_items.sort(key=lambda item: (item[0], item[1]["host"]))
    observed_items.sort(key=lambda item: (item[0], item[1]["host"]))
    return (
        [item for _, item in routing_items],
        [item for _, item in observed_items],
    )


def build_local_network_profile(info: PeerInfo) -> dict:
    candidates, observed_candidates = _collect_ipv4_candidates()
    environment_kind = str(info.platform or "unknown").lower()
    return {
        "host_identity": _normalize_host_identity(socket.gethostname()),
        "environment_kind": environment_kind,
        "address_candidates": candidates,
        "observed_candidates": observed_candidates,
    }


def _service_info_to_peer(info: ServiceInfo, self_instance_id: str) -> Optional[PeerInfo]:
    """Convert a zeroconf ServiceInfo into a PeerInfo. Returns None if it's ourselves."""
    try:
        props = _decode_service_properties(info.properties or {})
        address_raw = _decode_txt_value(props, "address_candidates_json", "[]")
        observed_raw = _decode_txt_value(props, "observed_candidates_json", "[]")
        address_candidates = _decode_candidate_records(address_raw)
        observed_candidates = _decode_candidate_records(observed_raw)

        instance_id = normalize_instance_id(_decode_txt_value(props, "instance_id"))
        if not is_valid_instance_id(instance_id):
            logger.debug(
                "LanDiscovery: ignoring service without valid instance_id: server=%s port=%s",
                getattr(info, "server", ""),
                getattr(info, "port", ""),
            )
            return None
        if instance_id == self_instance_id.upper():
            return None  # Don't include ourselves

        addresses = info.parsed_addresses()
        host = _pick_best_peer_host(addresses, address_candidates, observed_candidates, info.server.rstrip("."))

        return PeerInfo(
            instance_id=instance_id,
            display_name=_decode_txt_value(props, "display_name", instance_id),
            host=host,
            port=info.port,
            workbench_port=int(_decode_txt_value(props, "workbench_port", str(DEFAULT_WORKBENCH_PORT))),
            platform=_decode_txt_value(props, "platform", "unknown"),
            version=_decode_txt_value(props, "version", "unknown"),
            hashi_version=_decode_txt_value(props, "hashi_version", "unknown"),
            display_handle=_decode_txt_value(props, "display_handle", f"@{instance_id.lower()}"),
            protocol_version=_decode_txt_value(props, "protocol_version", "1.0"),
            capabilities=_decode_csv_txt_records(props, "capabilities"),
            properties={
                "discovery": "lan",
                "metadata_schema": _decode_txt_value(props, "metadata_schema", "1"),
                "host_identity": _normalize_host_identity(_decode_txt_value(props, "host_identity", "")),
                "environment_kind": _decode_txt_value(props, "environment_kind", "").strip().lower(),
                "agent_snapshot_version": _decode_txt_value(props, "agent_snapshot_version", ""),
                "directory_state": _decode_txt_value(props, "directory_state", ""),
                "identity_digest": _decode_txt_value(props, "identity_digest", ""),
                "capabilities_digest": _decode_txt_value(props, "capabilities_digest", ""),
                "address_candidates_digest": _decode_txt_value(props, "address_candidates_digest", ""),
                "address_candidates": address_candidates,
                "observed_candidates": observed_candidates,
            },
        )
    except Exception as e:
        logger.warning("Failed to parse ServiceInfo: %s", e)
        return None


class _HashiListener(ServiceListener):
    """Zeroconf listener that tracks discovered HASHI peers."""

    def __init__(self, self_instance_id: str, on_change: Optional[Callable] = None):
        self._self_id = self_instance_id
        self._peers: dict[str, PeerInfo] = {}
        self._on_change = on_change

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            peer = _service_info_to_peer(info, self._self_id)
            if peer:
                self._peers[peer.instance_id] = peer
                logger.info("Discovered peer: %s @ %s:%d", peer.instance_id, peer.host, peer.port)
                if self._on_change:
                    self._on_change(list(self._peers.values()))

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        # Extract instance_id from service name if possible
        for iid, peer in list(self._peers.items()):
            if iid.lower() in name.lower():
                del self._peers[iid]
                logger.info("Peer left: %s", iid)
                if self._on_change:
                    self._on_change(list(self._peers.values()))
                break

    def get_peers(self) -> list[PeerInfo]:
        return list(self._peers.values())


class LanDiscovery(PeerDiscovery):
    """
    mDNS-based LAN discovery for Hashi Remote.

    Uses zeroconf to both advertise this instance and discover others on the
    same local network. No configuration needed.

    Tailscale note: When Tailscale is used, all machines appear as if on the
    same LAN — this discovery backend works transparently with Tailscale too.
    """

    def __init__(self, self_instance_id: str, on_peers_changed: Optional[Callable] = None):
        self._self_id = self_instance_id
        self._on_peers_changed = on_peers_changed
        self._zeroconf: Optional[Zeroconf] = None
        self._service_info: Optional[ServiceInfo] = None
        self._listener: Optional[_HashiListener] = None
        self._browser: Optional[ServiceBrowser] = None
        self._advertising = False
        self._browsing = False
        self._last_error = ""
        self._last_failure_stage = ""
        self._last_attempt_at = 0.0
        self._last_success_at = 0.0
        self._next_retry_at = 0.0
        self._retry_count = 0
        self._stopped = False

    @property
    def backend_name(self) -> str:
        return "LAN/mDNS"

    async def advertise(self, info: PeerInfo) -> bool:
        """Register this instance on the LAN via mDNS."""
        if self._advertising and self._browsing:
            return True

        self._stopped = False
        self._last_attempt_at = time.time()
        try:
            loop = asyncio.get_event_loop()
            if self._zeroconf or self._service_info or self._browser:
                await loop.run_in_executor(None, self._cleanup_sync)
            await loop.run_in_executor(None, self._start_advertising, info)
            self._record_success()
            return self._advertising
        except Exception as e:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._cleanup_sync)
            except Exception:
                pass
            self._record_failure("startup", e)
            logger.error("LanDiscovery: advertise failed: %s", e)
            return False

    async def update_advertisement(self, info: PeerInfo) -> bool:
        """Refresh mDNS properties without restarting Remote."""
        if not self._advertising or not self._zeroconf or not self._service_info:
            return await self.advertise(info)
        self._last_attempt_at = time.time()
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._update_service_info, info)
            self._record_success()
            return True
        except Exception as e:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._cleanup_sync)
            except Exception:
                pass
            self._record_failure("update", e)
            logger.warning("LanDiscovery: advertisement update failed: %s", e)
            return False

    def retry_due(self) -> bool:
        return not self._stopped and not (
            self._advertising and self._browsing
        ) and time.time() >= self._next_retry_at

    def get_status(self) -> dict:
        if self._stopped:
            readiness = "stopped"
        elif self._advertising and self._browsing and not self._last_error:
            readiness = "ready"
        elif self._last_attempt_at:
            readiness = "degraded"
        else:
            readiness = "starting"
        return {
            "backend": "lan",
            "name": self.backend_name,
            "readiness": readiness,
            "advertising": bool(self._advertising),
            "browsing": bool(self._browsing),
            "peer_count": len(self._listener.get_peers()) if self._listener else 0,
            "last_error": self._last_error,
            "last_failure_stage": self._last_failure_stage,
            "retry_count": self._retry_count,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "next_retry_at": self._next_retry_at,
        }

    def _record_success(self) -> None:
        self._last_success_at = time.time()
        self._last_error = ""
        self._last_failure_stage = ""
        self._next_retry_at = 0.0
        self._retry_count = 0

    def _record_failure(self, stage: str, error: Exception) -> None:
        self._last_error = f"{type(error).__name__}: {error}"
        self._last_failure_stage = str(stage)
        self._retry_count += 1
        backoff = min(30.0, float(2 ** min(self._retry_count - 1, 5)))
        self._next_retry_at = time.time() + backoff

    def _service_info_for_peer(self, info: PeerInfo) -> ServiceInfo:
        hostname = socket.gethostname()
        local_ip = _get_local_ip()
        network_profile = build_local_network_profile(info)
        extra = dict(info.properties or {})

        scalar_props = {
            "metadata_schema": "2",
            "instance_id": info.instance_id,
            "display_name": info.display_name,
            "display_handle": info.display_handle or f"@{info.instance_id.lower()}",
            "platform": info.platform,
            "workbench_port": str(info.workbench_port),
            "version": HASHI_REMOTE_VERSION,
            "hashi_version": info.hashi_version,
            "protocol_version": info.protocol_version or "1.0",
            "host_identity": str(network_profile.get("host_identity") or ""),
            "environment_kind": str(network_profile.get("environment_kind") or ""),
            "agent_snapshot_version": str(extra.get("agent_snapshot_version") or ""),
            "directory_state": str(extra.get("directory_state") or ""),
        }
        identity_digest = _metadata_digest(
            {
                "instance_id": info.instance_id,
                "display_name": info.display_name,
                "display_handle": info.display_handle or f"@{info.instance_id.lower()}",
                "platform": info.platform,
                "hashi_version": info.hashi_version,
                "protocol_version": info.protocol_version or "1.0",
            }
        )
        props: dict[str, str] = {}
        for key, value in scalar_props.items():
            props.update(_encode_txt_value_records(key, _bounded_utf8_hint(value)))
        address_candidates = list(network_profile.get("address_candidates") or [])
        observed_candidates = list(network_profile.get("observed_candidates") or [])
        props.update(
            _encode_versioned_txt_records(
                "address_candidates_json",
                _encode_candidate_records(_route_hints(address_candidates)),
            )
        )
        props.update(
            _encode_versioned_txt_records(
                "observed_candidates_json",
                _encode_candidate_records(_route_hints(observed_candidates)),
            )
        )
        props.update(_encode_txt_value_records("capabilities_digest", _metadata_digest(info.capabilities or [])))
        props.update(_encode_txt_value_records("address_candidates_digest", _metadata_digest(address_candidates)))
        props.update(_encode_txt_value_records("identity_digest", identity_digest))
        for key, value in props.items():
            if _txt_record_size(key, value) > _MDNS_TXT_RECORD_MAX_BYTES:
                raise ValueError(f"mDNS TXT record exceeds 255 bytes: {key}")
        total_size = sum(1 + _txt_record_size(key, value) for key, value in props.items())
        if total_size > _MDNS_TXT_TOTAL_MAX_BYTES:
            raise ValueError(
                f"mDNS TXT payload exceeds {_MDNS_TXT_TOTAL_MAX_BYTES} bytes"
            )
        props_bytes = {k: v.encode("utf-8") for k, v in props.items()}
        service_name = f"{info.instance_id} - Hashi Remote.{HASHI_SERVICE_TYPE}"
        return ServiceInfo(
            type_=HASHI_SERVICE_TYPE,
            name=service_name,
            port=info.port,
            properties=props_bytes,
            server=f"{hostname}.local.",
            addresses=[socket.inet_aton(local_ip)],
        )

    def _start_advertising(self, info: PeerInfo) -> None:
        self._service_info = self._service_info_for_peer(info)

        # Bind to all interfaces so mDNS multicast reaches LAN even when
        # Tailscale is the default route (which would otherwise shadow the LAN NIC).
        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only, interfaces=InterfaceChoice.All)

        # Register first, then browse. A failure in either phase is rolled back
        # by advertise(), so health never reports a half-started backend.
        self._zeroconf.register_service(self._service_info)
        self._advertising = True
        self._listener = _HashiListener(self._self_id, self._on_peers_changed)
        self._browser = ServiceBrowser(self._zeroconf, HASHI_SERVICE_TYPE, self._listener)
        self._browsing = True
        logger.info(
            "LanDiscovery: advertising as %s @ %s:%d",
            info.instance_id, _get_local_ip(), info.port,
        )

    def _update_service_info(self, info: PeerInfo) -> None:
        if not self._zeroconf:
            raise RuntimeError("mDNS backend is not initialized")
        self._service_info = self._service_info_for_peer(info)
        self._zeroconf.update_service(self._service_info)
        logger.info("LanDiscovery: refreshed advertisement for %s", info.instance_id)

    def _cleanup_sync(self) -> None:
        browser = self._browser
        zeroconf = self._zeroconf
        service_info = self._service_info
        try:
            cancel = getattr(browser, "cancel", None)
            if callable(cancel):
                cancel()
        except Exception:
            pass
        try:
            if zeroconf and service_info and self._advertising:
                zeroconf.unregister_service(service_info)
        except Exception:
            pass
        try:
            if zeroconf:
                zeroconf.close()
        finally:
            self._advertising = False
            self._browsing = False
            self._zeroconf = None
            self._service_info = None
            self._browser = None
            self._listener = None

    async def discover(self) -> list[PeerInfo]:
        """Return currently known peers."""
        if self._listener:
            return self._listener.get_peers()
        return []

    async def stop(self) -> None:
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._cleanup_sync)
        except Exception as e:
            logger.warning("LanDiscovery: error during stop: %s", e)
        finally:
            self._stopped = True
            self._next_retry_at = 0.0
            logger.info("LanDiscovery: stopped")


def create_lan_discovery(
    self_instance_id: str,
    on_peers_changed: Optional[Callable] = None,
) -> LanDiscovery:
    return LanDiscovery(self_instance_id, on_peers_changed)
