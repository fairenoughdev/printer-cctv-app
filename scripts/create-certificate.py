#!/usr/bin/env python3
"""Generate a self-signed local HTTPS certificate, never printing private key data."""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lan-host", action="append", default=[],
                        help="LAN IP or DNS hostname to add to the certificate (repeatable).")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent / ".runtime" / "tls")
    args = parser.parse_args()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        parser.error("Run uv sync first, then .venv/bin/python scripts/create-certificate.py.")
    os.umask(0o077)
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate = destination / "certificate.pem"
    private_key = destination / "private-key.pem"
    if certificate.exists() or private_key.exists():
        parser.error("Certificate/key already exists; choose a new --output-dir rather than overwrite.")
    hosts = ["localhost", "127.0.0.1", "::1", *args.lan_host]
    alternatives = []
    for host in dict.fromkeys(hosts):
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            if not host or any(not (char.isalnum() or char in ".-") for char in host):
                parser.error("--lan-host must be a plain IP address or DNS hostname.")
            alternatives.append(x509.DNSName(host))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Printer CCTV Local")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    # Exclusive creation prevents accidental replacement even with concurrent calls.
    with private_key.open("xb") as output:
        output.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    private_key.chmod(0o600)
    with certificate.open("xb") as output:
        output.write(cert.public_bytes(serialization.Encoding.PEM))
    certificate.chmod(0o644)
    print(f"Certificate: {certificate}\nPrivate key: {private_key} (mode 600)")
    print("Set PRINTWATCH_TLS_CERT and PRINTWATCH_TLS_KEY to these file paths before launch.")
    print("Trust the certificate on display devices; never share the private key.")


if __name__ == "__main__":
    try:
        main()
    except OSError:
        print("Could not create certificate files; check the destination directory permissions.", file=sys.stderr)
        raise SystemExit(1)
