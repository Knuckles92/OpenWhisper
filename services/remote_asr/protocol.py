"""Wire format for the remote engine.

One TLS WebSocket per client. The first message is a JSON text frame, either
``hello`` (a paired device's token) or ``pair`` (a pairing code from the
host's screen). After the host answers ``ready``, every request is a single
binary frame and every reply a JSON text frame. The operations are those the
local speech worker takes over stdin (services/local_asr/worker.py):
``transcribe``, ``stream`` and ``cancel_stream``, plus ``describe``.

A request frame is a 4-byte big-endian header length, the JSON header, then
the audio as 16 kHz mono signed 16-bit little-endian PCM. Dictation audio is
recorded as 16-bit and resampled to 16-bit before it is decoded, so the
conversion loses nothing and halves what float32 would send.
"""
from __future__ import annotations

import hashlib
import json
import struct
from typing import Optional, Tuple

import numpy as np

PROTOCOL_VERSION = 1
DEFAULT_PORT = 47821
PATH = "/openwhisper/v1/engine"
SAMPLE_RATE = 16000

#: Longest audio accepted in one request. Clients send windows of at most
#: 30 s (services/local_asr/audio.py), so this only bounds a misbehaving peer.
MAX_AUDIO_SECONDS = 120
MAX_HEADER_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4 + MAX_HEADER_BYTES + MAX_AUDIO_SECONDS * SAMPLE_RATE * 2
MAX_REPLY_BYTES = 8 * 1024 * 1024

#: WebSocket close codes the host uses (4000-4999 is the application range).
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_TIMEOUT = 4408
CLOSE_ENGINE_CHANGED = 4409

_HEADER = struct.Struct(">I")


class ProtocolError(ValueError):
    """A frame that does not follow this protocol."""


def encode_audio(audio) -> bytes:
    """Float samples in [-1, 1] as little-endian int16 PCM."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    scaled = np.clip(np.rint(samples * 32768.0), -32768, 32767)
    return scaled.astype("<i2").tobytes()


def decode_audio(data: bytes) -> np.ndarray:
    """int16 PCM back to the float32 samples the engines take."""
    if len(data) % 2:
        raise ProtocolError("Audio payload has an odd byte count")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def pack_request(header: dict, audio=None) -> bytes:
    body = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_HEADER_BYTES:
        raise ProtocolError("Request header is too large")
    pcm = b"" if audio is None else encode_audio(audio)
    return _HEADER.pack(len(body)) + body + pcm


def unpack_request(frame: bytes) -> Tuple[dict, np.ndarray]:
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise ProtocolError("Requests must be binary frames")
    frame = bytes(frame)
    if len(frame) < _HEADER.size:
        raise ProtocolError("Request frame is truncated")
    (length,) = _HEADER.unpack_from(frame)
    if length > MAX_HEADER_BYTES or _HEADER.size + length > len(frame):
        raise ProtocolError("Request header length is invalid")
    try:
        header = json.loads(frame[_HEADER.size:_HEADER.size + length])
    except ValueError as exc:
        raise ProtocolError("Request header is not JSON") from exc
    if not isinstance(header, dict):
        raise ProtocolError("Request header must be an object")
    audio = frame[_HEADER.size + length:]
    if len(audio) > MAX_AUDIO_SECONDS * SAMPLE_RATE * 2:
        raise ProtocolError("Request audio is too long")
    return header, decode_audio(audio)


def certificate_fingerprint(der: bytes) -> str:
    """SHA-256 of a DER certificate, as uppercase hex."""
    return hashlib.sha256(der).hexdigest().upper()


def short_fingerprint(fingerprint: str, groups: int = 5) -> str:
    """The first ``groups`` blocks of four, for people to compare by eye.

    Five blocks are 80 bits: enough that a forged certificate cannot be made
    to match what the host's screen shows.
    """
    clean = "".join(ch for ch in (fingerprint or "").upper() if ch.isalnum())
    return "-".join(clean[i:i + 4] for i in range(0, min(len(clean), groups * 4), 4))


def token_digest(token: str) -> str:
    """What the host stores for a device token. The token itself never is."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> Tuple[str, int]:
    """Split ``host``, ``host:port`` or ``[v6]:port`` into host and port.

    Pasted URLs are tolerated: a scheme and any path are dropped.
    """
    raw = (text or "").strip()
    for scheme in ("https://", "http://", "wss://", "ws://"):
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme):]
            break
    raw = raw.split("/", 1)[0].strip()
    if not raw:
        raise ValueError("Enter the host's address, such as 192.168.1.20 or devbox.local")
    host: str
    port: Optional[int] = None
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise ValueError("The IPv6 address is missing its closing bracket")
        host = raw[1:end]
        rest = raw[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError("Put the port after the bracket, as in [fe80::1]:47821")
            port = _parse_port(rest[1:])
    elif raw.count(":") == 1:
        host, port_text = raw.split(":")
        port = _parse_port(port_text)
    else:
        # No colon, or an unbracketed IPv6 address with no port.
        host = raw
    host = host.strip()
    if not host or any(ch.isspace() for ch in host):
        raise ValueError("The host's address is not valid")
    return host, port if port is not None else default_port


def _parse_port(text: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise ValueError("The port must be a number") from exc
    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535")
    return port


def format_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
