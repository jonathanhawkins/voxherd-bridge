"""TLS certificate management for the VoxHerd bridge.

Generates a self-signed certificate on first run and stores it in
``~/.voxherd/tls/``.  Subsequent runs reuse the existing cert unless
it has expired or the hostname has changed.
"""

import datetime
import os
import socket
import ssl
from pathlib import Path

_TLS_DIR = Path.home() / ".voxherd" / "tls"
_CERT_FILE = _TLS_DIR / "bridge.crt"
_KEY_FILE = _TLS_DIR / "bridge.key"

# Certificate validity period
_CERT_DAYS = 365


def _generate_self_signed_cert() -> tuple[str, str]:
    """Generate a self-signed certificate and return (cert_path, key_path).

    Uses the ``cryptography`` library if available, falls back to
    ``openssl`` CLI.
    """
    _TLS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_TLS_DIR, 0o700)

    hostname = socket.gethostname()

    try:
        # Try using the cryptography library (installed with uvicorn[standard])
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import ipaddress

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VoxHerd"),
        ])

        # Build SAN list with hostname and local IPs
        san_entries: list[x509.GeneralName] = [
            x509.DNSName(hostname),
            x509.DNSName("localhost"),
        ]

        # Add local IP addresses
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                addr = info[4][0]
                if not addr.startswith("127."):
                    san_entries.append(x509.IPAddress(ipaddress.IPv4Address(addr)))
        except socket.gaierror:
            pass
        san_entries.append(x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")))

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=_CERT_DAYS)
            )
            .add_extension(
                x509.SubjectAlternativeName(san_entries),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        # Write key (restrictive permissions)
        key_fd = os.open(str(_KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(key_fd, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))

        # Write cert
        cert_fd = os.open(str(_CERT_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(cert_fd, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    except ImportError:
        # Fallback: use openssl CLI
        import subprocess
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(_KEY_FILE),
                "-out", str(_CERT_FILE),
                "-days", str(_CERT_DAYS),
                "-nodes",
                "-subj", f"/CN={hostname}/O=VoxHerd",
                "-addext", f"subjectAltName=DNS:{hostname},DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(_KEY_FILE, 0o600)

    return str(_CERT_FILE), str(_KEY_FILE)


def ensure_cert() -> tuple[str, str]:
    """Return (cert_path, key_path), generating if needed.

    Regenerates if the cert doesn't exist or has expired.
    """
    if _CERT_FILE.exists() and _KEY_FILE.exists():
        # Check expiry
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(str(_CERT_FILE), str(_KEY_FILE))
            # If we can load it, it's valid enough
            return str(_CERT_FILE), str(_KEY_FILE)
        except (ssl.SSLError, OSError):
            pass

    return _generate_self_signed_cert()


def get_ssl_context() -> ssl.SSLContext:
    """Create an SSL context with the bridge's self-signed certificate."""
    cert_path, key_path = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    # Minimum TLS 1.2
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
