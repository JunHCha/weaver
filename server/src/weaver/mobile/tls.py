"""Local certificate authority and server certificate for the phone API.

The phone trusts only this CA, installed through the QR pairing payload. The
server certificate names the bind IP; renewing it keeps the CA so paired
phones need no new trust step. No public certificate (and no CT log entry) is
involved, and nothing here ever disables verification.
"""

import hashlib
import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
LEAF_DAYS = 365
RENEW_BEFORE_DAYS = 30


@dataclass(frozen=True)
class TlsFiles:
    ca_cert: Path
    server_cert: Path
    server_key: Path

    @property
    def ca_der(self) -> bytes:
        return x509.load_pem_x509_certificate(self.ca_cert.read_bytes()).public_bytes(
            serialization.Encoding.DER
        )

    @property
    def ca_sha256(self) -> str:
        return hashlib.sha256(self.ca_der).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    os.chmod(path, 0o600)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _leaf_covers(path: Path, address: str, now: datetime) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        wanted = ipaddress.ip_address(address)
        covered = wanted in san.value.get_values_for_type(x509.IPAddress)
        return covered and cert.not_valid_after_utc > now + timedelta(
            days=RENEW_BEFORE_DAYS
        )
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def ensure_certificates(
    directory: Path, address: str, *, now: datetime | None = None
) -> TlsFiles:
    """Create the CA once, and (re)issue the server certificate when needed."""
    now = now or datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    files = TlsFiles(
        directory / "ca.pem", directory / "server.pem", directory / "server.key"
    )
    ca_key_path = directory / "ca.key"
    if not (files.ca_cert.is_file() and ca_key_path.is_file()):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(_name("Weaver local CA"))
            .issuer_name(_name("Weaver local CA"))
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(ca_key_path, _key_pem(ca_key))
        files.ca_cert.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    if files.server_cert.is_file() and files.server_key.is_file():
        if _leaf_covers(files.server_cert, address, now):
            return files
    signing_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), None)
    assert isinstance(signing_key, ec.EllipticCurvePrivateKey)
    ca_cert = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("Weaver phone API"))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=LEAF_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(address))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                signing_key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(signing_key, hashes.SHA256())
    )
    _write_private(files.server_key, _key_pem(leaf_key))
    files.server_cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    return files
