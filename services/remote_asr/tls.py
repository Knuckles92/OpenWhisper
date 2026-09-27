"""The host's TLS identity and the client's pinning context.

The host keeps one self-signed certificate for good. A client records its
SHA-256 fingerprint when it pairs, then refuses any other certificate, the
way Syncthing and KDE Connect trust their peers. No certificate authority or
hostname is involved, so the host can be reached by IP, name or tailnet
address alike.
"""
from __future__ import annotations

import datetime
import logging
import os
import socket
import ssl
from dataclasses import dataclass

from services.remote_asr.protocol import certificate_fingerprint

logger = logging.getLogger(__name__)

CERT_FILENAME = "remote_engine_host_cert.pem"
KEY_FILENAME = "remote_engine_host_key.pem"
_VALIDITY_DAYS = 20 * 365


@dataclass(frozen=True)
class HostIdentity:
    cert_path: str
    key_path: str
    fingerprint: str


def ensure_host_identity(directory: str) -> HostIdentity:
    """Load the host certificate from ``directory``, creating it once."""
    cert_path = os.path.join(directory, CERT_FILENAME)
    key_path = os.path.join(directory, KEY_FILENAME)
    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            return HostIdentity(cert_path, key_path, _fingerprint_of(cert_path))
        except (OSError, ValueError):
            logger.warning("Remote engine certificate is unreadable; creating a new one")
    _generate(cert_path, key_path)
    return HostIdentity(cert_path, key_path, _fingerprint_of(cert_path))


def _fingerprint_of(cert_path: str) -> str:
    with open(cert_path, encoding="ascii") as stream:
        return certificate_fingerprint(ssl.PEM_cert_to_DER_cert(stream.read()))


def _generate(cert_path: str, key_path: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"OpenWhisper host {socket.gethostname()}"[:64]),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # Owner-only on POSIX; Windows keeps the per-user profile's ACL.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key_bytes)
    with open(cert_path, "wb") as stream:
        stream.write(certificate.public_bytes(serialization.Encoding.PEM))
    logger.info("Created the remote engine host certificate")


def server_context(identity: HostIdentity) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # No TLS 1.3 session tickets. They arrive after the handshake, while the
    # websockets client's reader thread and its handshake write share the
    # SSL socket, and on Windows that stalled about one connection in ten
    # (the client never sent its HTTP upgrade; 11 of 120 in a loop, 0 of 160
    # without tickets). Clients never resume sessions, so nothing is lost.
    context.num_tickets = 0
    context.load_cert_chain(identity.cert_path, identity.key_path)
    return context


def client_context() -> ssl.SSLContext:
    """Encrypt without CA checks; the caller pins the peer's fingerprint.

    ``peer_fingerprint`` must be compared before anything secret is sent.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def peer_fingerprint(sock) -> str:
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("The host presented no certificate")
    return certificate_fingerprint(der)
