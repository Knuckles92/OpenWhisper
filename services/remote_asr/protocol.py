"""Wire format for the remote engine.

One TLS WebSocket per client. The first message is a JSON text frame, either
``hello`` (a paired device's token) or ``pair`` (a pairing code from the
host's screen). After the host answers ``ready``, every request is a single
binary frame and every reply a JSON text frame. The operations are those the
local speech worker takes over stdin (services/local_asr/worker.py):
``transcribe``, ``stream`` and ``cancel_stream``, plus ``describe`` and
``select_model``. ``ready`` lists the models the host can switch to as
``models``, and ``select_model`` (``family``, ``model``) switches it and
answers once the model has loaded; the client then reconnects. Hosts from
before model switching send no ``models`` and answer ``select_model`` with
an unknown-operation error. A successful reply carries ``host_ms``, the time
the host spent on the request, which older hosts leave out.

Opt-in model management is advertised only to authenticated clients as
``ready.capabilities.model_management`` (missing means unsupported).
``model_catalog`` returns bundled models, cache/runtime readiness, the active
engine and the last remote download's progress. ``download_model`` takes
``family`` and ``model`` from that catalog and acknowledges a host-owned
background download; poll ``model_catalog`` for completion. Neither operation
changes download policy or selects a model. Authorization
is checked on every request. Accepted downloads continue after disconnect or
permission changes. These additive operations keep protocol version 1.

Hosts with ``capabilities.runtime_installation`` also accept ``install_runtime``
with a bundled ``family``, ``model`` and explicit ``device`` (cpu/cuda;
Parakeet MLX uses auto for the Apple GPU or cpu). The host
resolves a platform-compatible, pinned component and installs it in the background
using its shared component coordinator. Clients cannot supply URLs, paths or
commands. ``model_catalog`` includes per-device dependency readiness and reasons,
download sizes, plus ``installation`` progress, failure and restart status.
The same host model-management opt-in is rechecked for every install request.
Accepted installations survive disconnect and permission changes. On these hosts,
``select_model`` may include ``device`` to load a cached model with a ready runtime
in one change; this extension also requires model-management permission.

Runtime controls are advertised to paired clients as
``ready.capabilities.engine_controls``. ``ready.runtime`` describes the host's
saved device, supported devices and precisions, optional language choices, and
GPU name/memory; ``ready.engine`` describes what is actually running.
``configure_runtime`` takes the current ``family`` and ``model`` plus a bounded
``settings`` object (device, compute_type, or language). It follows the same
paired-client authorization as model selection, rejects stale engine choices,
and answers after the host reloads. The client reconnects to authoritative
state, including CPU fallback. Older hosts have no editable runtime controls.

Record storage is advertised as ``ready.capabilities.records``: true when the
host keeps records (dictation history, meetings) for paired computers, false
when its owner hasn't turned that on, and missing on older hosts.
``ready.records`` then counts what this device has stored there. Every
``records_*`` request is scoped to the authenticated device, which can only
see, fetch or delete its own records, and the permission is rechecked per
request. A record is a set of files, one of them ``record.json``; uploads are
resumable and verified:

* ``records_stat`` (``kind``, ``record_id``) lists the files an earlier copy
  left on the host, so an edited meeting is sent without its audio again
  (record.json then says ``update``).
* ``records_begin`` (``kind``, ``record_id``, ``files``, ``bytes``) opens or
  resumes an upload and answers with what the host already has of each file.
* ``records_put`` (``record_id``, ``name``, ``size``, ``sha256``, ``offset``)
  carries up to ``RECORD_CHUNK_BYTES`` of one file as the frame's payload;
  offsets are append-only and a finished file is checked against its hash.
* ``records_commit`` (``record_id``) imports the complete record into the
  host's own history or meetings, badged with the device it came from.
* ``records_list`` (``kind``, ``query``, ``limit``), ``records_open`` and
  ``records_fetch`` (``name``, ``offset``) read records back, the latter as
  base64 in the JSON reply; ``records_delete`` removes one, ``records_clear``
  every one of a kind, and ``records_abort`` drops an unfinished upload.

MCP control is advertised as ``ready.capabilities.mcp_control``: true when the
host's owner allows paired computers to manage its MCP server (off by
default), false when they haven't, and missing on older hosts. The permission
is rechecked on every request. ``mcp_state`` returns whether MCP is on, its
status, port, Tailscale access, the connection URLs, the access token while
it is running, and which agent permissions and preferences are granted.
``mcp_configure`` takes a bounded ``settings`` object (``enabled``, ``port``,
``tailscale``, ``retitle_transcriptions``, ``retitle_meetings``,
``settings_access`` and ``writable``, a map of preference key to bool),
applies it all or none of it, and answers with the new state. Port and
Tailscale can change only while MCP is off, as on the host's own page.

A request frame is a 4-byte big-endian header length, the JSON header, then
a payload. For decoding operations the payload is the audio as 16 kHz mono
signed 16-bit little-endian PCM. Dictation audio is recorded as 16-bit and
resampled to 16-bit before it is decoded, so the conversion loses nothing
and halves what float32 would send. For ``records_put`` it is file bytes.

Direct history uses a separate paired connection: ``hello`` includes
``purpose: "history"`` and the client's ``history_enabled`` opt-in. A supporting
host replies with ``ready.capabilities.client_history: true``, then sends JSON
``history_query`` messages (id, operation, params). The client returns JSON
``history_result`` with that id and a public History API result or sanitized
error code. This channel carries no audio, accepts only read operations, and
the client rechecks its permission for every query. Older hosts omit the
capability, so clients close the dedicated connection without sending history.

Pairing by approval, for a host found on the network (``probe`` answers
``approval: true``): the first message is ``pair_request`` (``device_name``).
The host answers ``pair_commit`` with the SHA-256 commitment to a random
nonce, the client sends its own nonce in ``pair_nonce``, and the host reveals
its nonce in ``pair_reveal``. Both sides then show ``pairing_sas`` of the two
nonces and the certificate fingerprint, and the host's owner allows the
request only if the numbers match; the host answers ``paired`` exactly as for
a code, or an error (``pair_denied``, ``pair_timeout``). The host committed to
its nonce before seeing the client's, so a computer in the middle can't steer
the two numbers to match, and the fingerprint is in the hash, so relaying the
nonces unchanged gives two different numbers. The client may send
``pair_cancel`` while it waits. Older hosts close the connection.
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
#: File bytes per ``records_put`` frame and per ``records_fetch`` reply.
#: Well inside both frame limits, base64 included.
RECORD_CHUNK_BYTES = 1024 * 1024

#: WebSocket close codes the host uses (4000-4999 is the application range).
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_TIMEOUT = 4408
CLOSE_ENGINE_CHANGED = 4409
CLOSE_BUSY = 4429

#: Longest name one computer keeps for another, given or chosen.
MAX_NAME = 60

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


def pack_request(header: dict, audio=None, *, payload: Optional[bytes] = None) -> bytes:
    """A request frame; ``payload`` is raw bytes, sent instead of ``audio``."""
    body = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_HEADER_BYTES:
        raise ProtocolError("Request header is too large")
    if payload is not None:
        data = bytes(payload)
    else:
        data = b"" if audio is None else encode_audio(audio)
    return _HEADER.pack(len(body)) + body + data


def unpack_request(frame: bytes) -> Tuple[dict, np.ndarray]:
    header, payload = unpack_frame(frame)
    return header, payload_audio(payload)


def payload_audio(payload: bytes) -> np.ndarray:
    """A decoding request's payload as audio, within the length limit."""
    if len(payload) > MAX_AUDIO_SECONDS * SAMPLE_RATE * 2:
        raise ProtocolError("Request audio is too long")
    return decode_audio(payload)


def unpack_frame(frame: bytes) -> Tuple[dict, bytes]:
    """The header and the payload as bytes, without reading it as audio."""
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
    return header, frame[_HEADER.size + length:]


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


def clean_name(name) -> str:
    """A computer's name with only printable characters, trimmed; "" if none are left."""
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return text[:MAX_NAME]


#: Bytes in each side's pairing nonce.
PAIRING_NONCE_BYTES = 32
SAS_DIGITS = 6


def pairing_commitment(nonce: bytes) -> str:
    """What the host sends before it has seen the client's nonce."""
    return hashlib.sha256(b"openwhisper-pair-commit-v1" + nonce).hexdigest()


def pairing_sas(host_nonce: bytes, client_nonce: bytes, fingerprint: str) -> str:
    """The six digits both screens show while a pairing waits for approval.

    A computer in the middle has its own certificate, so the client hashes a
    different fingerprint than the host does; to make the two numbers match
    it would have to choose nonces after seeing the other side's, which the
    commitment rules out. One try in a million is left to luck.
    """
    digest = hashlib.sha256(
        b"openwhisper-pair-sas-v1" + host_nonce + client_nonce
        + (fingerprint or "").upper().encode("ascii", "replace")
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 10 ** SAS_DIGITS:0{SAS_DIGITS}d}"


def format_sas(sas: str) -> str:
    """``123 456``, easier to compare across a room than ``123456``."""
    return f"{sas[:3]} {sas[3:]}" if len(sas) == SAS_DIGITS else sas


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
