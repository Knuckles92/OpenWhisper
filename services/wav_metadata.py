"""RIFF/WAVE origination tags for File Explorer and other media browsers.

Python's ``wave`` module writes bare PCM. Windows Details columns such as
Media created read LIST INFO ``ICRD`` and Broadcast Wave ``bext`` dates, so
Quick Record adds those to each recording as it saves it.
"""
from __future__ import annotations

import os
import struct
from datetime import datetime
from typing import Iterator, Optional, Tuple

ORIGINATOR = "OpenWhisper"
SOFTWARE = "OpenWhisper"
BEXT_SIZE = 602
# RIFF stores its size as an unsigned 32-bit field.
_MAX_RIFF_SIZE = 0xFFFFFFFF

_BEXT_DATE = slice(320, 330)
_BEXT_TIME = slice(330, 338)
_BEXT_ORIGINATOR = slice(256, 288)


class WavMetadataError(ValueError):
    """The file is not a RIFF WAVE we can tag."""


def append_origination(wav, when: datetime) -> None:
    """Tag the RIFF/WAVE just written to ``wav``, in place.

    ``wav`` is the open binary file a ``wave`` writer has finished with, so
    its last chunk ends where the file does. LIST INFO and bext go after
    that chunk and the RIFF size is set to match; nothing more may be
    written through ``wav`` afterwards. In place, because the tags are 700
    bytes and rewriting the file to add them means a second copy of every
    recording, with its own fsync and rename, before it can be transcribed.

    A failure is raised with the file put back as the untagged WAV it was.
    """
    wav.flush()
    # The descriptor, not the file object: a buffered writer keeps a failed
    # write and tries it again, so a partial tag could not be taken back.
    fd = wav.fileno()
    end = os.lseek(fd, 0, os.SEEK_END)
    # Python's wave writer can omit the final pad byte for odd-length 8-bit
    # PCM. Insert it before adding another RIFF chunk.
    padding = b"\x00" if end & 1 else b""
    metadata = padding + _list_info_chunk(when) + _bext_chunk(when)
    if end + len(metadata) - 8 > _MAX_RIFF_SIZE:
        raise WavMetadataError("Recording is too large for RIFF metadata")
    try:
        _write_all(fd, metadata)
        _write_riff_size(fd, end + len(metadata))
    except BaseException:
        os.ftruncate(fd, end)
        _write_riff_size(fd, end)
        raise


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if not written:
            raise OSError("Recording storage accepted no more data")
        view = view[written:]


def _write_riff_size(fd: int, file_size: int) -> None:
    """Record ``file_size`` in the RIFF header and leave the descriptor at the end."""
    os.lseek(fd, 4, os.SEEK_SET)
    _write_all(fd, struct.pack("<I", file_size - 8))
    os.lseek(fd, 0, os.SEEK_END)


#: Chunks read while looking for ``fmt``; it is nearly always the first.
_FORMAT_SEARCH_CHUNKS = 16
_WAVE_FORMAT_PCM = 1


def is_mono_pcm16(path) -> bool:
    """Whether ``path`` is a RIFF/WAVE of 16-bit integer PCM with one channel.

    What Quick Record saves. Reads only the chunk headers before ``data``;
    a file that can't be read, or isn't a WAV, is simply not one.
    """
    try:
        with open(path, "rb") as handle:
            if not _is_wave(handle.read(12)):
                return False
            for _ in range(_FORMAT_SEARCH_CHUNKS):
                header = handle.read(8)
                if len(header) != 8:
                    return False
                chunk_id, size = struct.unpack("<4sI", header)
                if chunk_id == b"fmt ":
                    fields = handle.read(16)
                    if len(fields) != 16:
                        return False
                    tag, channels, _rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", fields)
                    return (tag, channels, bits) == (_WAVE_FORMAT_PCM, 1, 16)
                if chunk_id == b"data":
                    return False
                handle.seek(size + (size & 1), os.SEEK_CUR)
    except OSError:
        return False
    return False


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
