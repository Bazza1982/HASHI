"""
TLS certificate management for Hashi Remote.
Adapted from Lily Remote — storage path changed to ~/.hashi-remote/certs/
"""

import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


CERT_DIR = Path.home() / ".hashi-remote" / "certs"
CERT_VALIDITY_DAYS = 365


def _generate_self_signed_cert(hostname: str, cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed TLS certificate for the given hostname."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Hashi Remote"),
    ])

    san_list = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
    ]

    # Try to add actual local IP
    try:
        import ipaddress
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        san_list.append(x509.IPAddress(ipaddress.IPv4Address(local_ip)))
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)


def _is_cert_expired(cert_path: Path) -> bool:
    try:
        from cryptography.x509 import load_pem_x509_certificate
        cert = load_pem_x509_certificate(cert_path.read_bytes())
        return cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=7)
    except Exception:
        return True


def load_or_generate_cert(hostname: str = None) -> Tuple[Path, Path]:
    """Return (cert_path, key_path), generating if missing or expired."""
    if hostname is None:
        hostname = socket.gethostname()

    cert_path = CERT_DIR / "server.crt"
    key_path = CERT_DIR / "server.key"

    if not cert_path.exists() or not key_path.exists() or _is_cert_expired(cert_path):
        _generate_self_signed_cert(hostname, cert_path, key_path)

    return cert_path, key_path


def get_cert_fingerprint(cert_path: Path) -> str:
    """Return the SHA256 fingerprint of the certificate."""
    try:
        from cryptography.x509 import load_pem_x509_certificate
        cert = load_pem_x509_certificate(cert_path.read_bytes())
        fp = cert.fingerprint(hashes.SHA256()).hex()
        return ":".join(fp[i:i+2].upper() for i in range(0, len(fp), 2))
    except Exception:
        return "unknown"
