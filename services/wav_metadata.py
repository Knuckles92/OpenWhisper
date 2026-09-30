"""RIFF/WAVE origination tags for File Explorer and other media browsers.

Python's ``wave`` module writes bare PCM. Windows Details columns such as
Media created read LIST INFO ``ICRD`` and Broadcast Wave ``bext`` dates, so
Quick Record stamps those after the PCM file is in place.
"""
from __future__ import annotations

import os
import struct
import tempfile
from datetime import datetime
from typing import Iterator, Optional, Tuple

ORIGINATOR = "OpenWhisper"
SOFTWARE = "OpenWhisper"
BEXT_SIZE = 602
COPY_BLOCK_BYTES = 1024 * 1024

_BEXT_DATE = slice(320, 330)
_BEXT_TIME = slice(330, 338)
_BEXT_ORIGINATOR = slice(256, 288)


class WavMetadataError(ValueError):
    """The file is not a RIFF WAVE we can tag."""


def stamp_wav_origination(path: str, when: datetime) -> bool:
    """Append LIST INFO and bext origination. Return True if the file changed.

    Files that are not RIFF/WAVE, or that already have ``bext`` or LIST INFO,
    are left untouched. Scan chunk headers and stream-copy the original audio
    into an atomic replacement, keeping memory bounded for long recordings.
    """
    temp_path = None
    fd = None
    try:
        with open(path, "rb") as source:
            header = source.read(12)
            if not _is_wave(header):
                return False
            source_size = os.fstat(source.fileno()).st_size
            aligned_end = 12
            for chunk_id, offset, size in _scan_chunk_headers(source, source_size):
                if chunk_id == b"bext":
                    return False
                if chunk_id == b"LIST" and size >= 4:
                    source.seek(offset)
                    if source.read(4) == b"INFO":
                        return False
                aligned_end = offset + size + (size & 1)
            # Python's wave writer can omit the final pad byte for odd-length
            # 8-bit PCM. Insert it before adding another RIFF chunk.
            padding = b"\x00" if aligned_end > source_size else b""
            metadata = padding + _list_info_chunk(when) + _bext_chunk(when)
            riff_size = source_size + len(metadata) - 8
            if riff_size > 0xFFFFFFFF:
                raise WavMetadataError("Recording is too large for RIFF metadata")
            directory = os.path.dirname(os.path.abspath(path)) or os.curdir
            fd, temp_path = tempfile.mkstemp(suffix=".wav", dir=directory)
            destination = os.fdopen(fd, "wb")
            fd = None  # ownership transferred to destination
            with destination:
                destination.write(header[:4] + struct.pack("<I", riff_size) + header[8:])
                source.seek(12)
                remaining = source_size - 12
                while remaining:
                    block = source.read(min(COPY_BLOCK_BYTES, remaining))
                    if not block:
                        raise WavMetadataError("Recording was truncated during metadata copy")
                    destination.write(block)
                    remaining -= len(block)
                destination.write(metadata)
                destination.flush()
                os.fsync(destination.fileno())
        # Close the source before replacing it, including on Windows.
        os.replace(temp_path, path)
    except Exception:
        if fd is not None:
            os.close(fd)
        if temp_path is not None and os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    return True


def _scan_chunk_headers(handle, length: int) -> Iterator[Tuple[bytes, int, int]]:
    """Yield chunk ID, payload offset and size without reading audio payloads."""
    offset = 12
    while offset < length:
        if offset + 8 > length:
            raise WavMetadataError("Truncated RIFF chunk header")
        handle.seek(offset)
        header = handle.read(8)
        if len(header) != 8:
            raise WavMetadataError("Truncated RIFF chunk header")
        chunk_id, size = struct.unpack("<4sI", header)
        start = offset + 8
        end = start + size
        if end > length:
            raise WavMetadataError("Truncated RIFF chunk payload")
        yield chunk_id, start, size
        offset = end + (size & 1)


def read_info_icrd(path: str) -> Optional[str]:
    """Return the LIST INFO creation date, or None."""
    return _read_info_field(path, b"ICRD")


def read_info_isft(path: str) -> Optional[str]:
    """Return the LIST INFO software tag, or None."""
    return _read_info_field(path, b"ISFT")


def read_bext_origination(path: str) -> Optional[Tuple[str, str]]:
    """Return ``(YYYY-MM-DD, HH:MM:SS)`` from bext, or None."""
    with open(path, "rb") as handle:
        data = handle.read()
    if not _is_wave(data):
        return None
    for chunk_id, payload in _iter_chunks(data):
        if chunk_id != b"bext" or len(payload) < _BEXT_TIME.stop:
            continue
        date = payload[_BEXT_DATE].decode("ascii")
        time = payload[_BEXT_TIME].decode("ascii")
        return date, time
    return None


def _is_wave(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _has_bext_or_info(data: bytes) -> bool:
    for chunk_id, payload in _iter_chunks(data):
        if chunk_id == b"bext":
            return True
        if chunk_id == b"LIST" and payload[:4] == b"INFO":
            return True
    return False


def _iter_chunks(data: bytes) -> Iterator[Tuple[bytes, bytes]]:
    if not _is_wave(data):
        raise WavMetadataError("Not a RIFF WAVE file")
    offset = 12
    length = len(data)
    while offset + 8 <= length:
        chunk_id = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        end = start + size
        if end > length:
            break
        yield chunk_id, data[start:end]
        offset = end + (size & 1)


def _with_riff_size(data: bytes) -> bytes:
    return data[:4] + struct.pack("<I", len(data) - 8) + data[8:]


def _riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    chunk = chunk_id + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        chunk += b"\x00"
    return chunk


def _list_info_chunk(when: datetime) -> bytes:
    items = _riff_chunk(b"ICRD", _zstring(when.strftime("%Y-%m-%d")))
    items += _riff_chunk(b"ISFT", _zstring(SOFTWARE))
    return _riff_chunk(b"LIST", b"INFO" + items)


def _bext_chunk(when: datetime) -> bytes:
    payload = bytearray(BEXT_SIZE)
    originator = ORIGINATOR.encode("ascii")[:32]
    payload[_BEXT_ORIGINATOR] = originator.ljust(32, b"\x00")
    payload[_BEXT_DATE] = when.strftime("%Y-%m-%d").encode("ascii")
    payload[_BEXT_TIME] = when.strftime("%H:%M:%S").encode("ascii")
    return _riff_chunk(b"bext", bytes(payload))


def _zstring(value: str) -> bytes:
    return value.encode("ascii") + b"\x00"


def _read_info_field(path: str, tag: bytes) -> Optional[str]:
    with open(path, "rb") as handle:
        data = handle.read()
    if not _is_wave(data):
        return None
    for chunk_id, payload in _iter_chunks(data):
        if chunk_id != b"LIST" or payload[:4] != b"INFO":
            continue
        return _info_item_text(payload[4:], tag)
    return None


def _info_item_text(items: bytes, tag: bytes) -> Optional[str]:
    offset = 0
    length = len(items)
    while offset + 8 <= length:
        item_id = items[offset:offset + 4]
        size = struct.unpack_from("<I", items, offset + 4)[0]
        start = offset + 8
        end = start + size
        if end > length:
            break
        if item_id == tag:
            return items[start:end].split(b"\x00", 1)[0].decode("ascii")
        offset = end + (size & 1)
    return None
