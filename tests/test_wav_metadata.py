"""RIFF/WAVE origination tags used by File Explorer."""
import os
import struct
import wave
from datetime import datetime

import numpy as np
import pytest

import services.wav_metadata as wav_metadata
from services.wav_metadata import (
    WavMetadataError,
    append_origination,
    is_mono_pcm16,
    read_bext_origination,
    read_info_icrd,
    read_info_isft,
)

WHEN = datetime(2026, 9, 19, 13, 5, 7)


def _write_tagged(path, frames: bytes, *, width: int = 2, when: datetime = WHEN) -> None:
    """Write ``frames`` as mono PCM and tag the file before it is closed, as the recorder does."""
    with open(path, "wb") as handle:
        with wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(width)
            wav.setframerate(16000)
            wav.writeframesraw(frames)
        append_origination(handle, when)


def _untagged(frames: bytes, *, width: int = 2) -> bytes:
    """The bytes Python's wave writer alone produces for ``frames``."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE", b"fmt ", 16, 1, 1,
        16000, 16000 * width, width, 8 * width, b"data", len(frames),
    )
    return header + frames


def test_tags_round_trip(tmp_path):
    path = tmp_path / "clip.wav"
    _write_tagged(path, np.full(8000, 1000, dtype=np.int16).tobytes())

    assert read_info_icrd(str(path)) == "2026-09-19"
    assert read_info_isft(str(path)) == "OpenWhisper"
    assert read_bext_origination(str(path)) == ("2026-09-19", "13:05:07")


def test_tagging_preserves_pcm_and_even_alignment(tmp_path):
    path = tmp_path / "clip.wav"
    frames = np.arange(64, dtype=np.int16).tobytes()
    _write_tagged(path, frames)

    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() == 64
        assert handle.readframes(64) == frames

    data = path.read_bytes()
    assert data[8:len(_untagged(frames))] == _untagged(frames)[8:]
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8
    _assert_even_aligned_info_items(data)


def test_odd_length_pcm_gets_padding_before_the_tags(tmp_path):
    path = tmp_path / "odd.wav"
    _write_tagged(path, b"\x80\x81\x7f", width=1)

    with wave.open(str(path), "rb") as handle:
        assert handle.readframes(3) == b"\x80\x81\x7f"
    data = path.read_bytes()
    assert data[47:52] == b"\x00LIST"
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8
    assert read_info_icrd(str(path)) == "2026-09-19"
    assert read_bext_origination(str(path)) == ("2026-09-19", "13:05:07")


def test_tags_follow_audio_still_in_the_writer_buffer(tmp_path):
    path = tmp_path / "buffered.wav"
    frames = np.arange(2000, dtype=np.int16).tobytes()
    # A buffer larger than the file: nothing reaches the disk before tagging.
    with open(path, "wb", buffering=1024 * 1024) as handle:
        with wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframesraw(frames)
        append_origination(handle, WHEN)

    with wave.open(str(path), "rb") as handle:
        assert handle.readframes(2000) == frames
    assert read_info_icrd(str(path)) == "2026-09-19"


@pytest.mark.parametrize("failing_write", [1, 2])
def test_a_failed_tag_leaves_the_untagged_wav(tmp_path, monkeypatch, failing_write):
    """The tags' own write, or the header update after it, fails once."""
    path = tmp_path / "clip.wav"
    frames = np.arange(64, dtype=np.int16).tobytes()
    native_write = os.write
    writes = []

    def flaky_write(fd, data):
        writes.append(len(data))
        if len(writes) == failing_write:
            if failing_write == 1:
                # Part of the tags lands before the disk fills.
                native_write(fd, bytes(data[:100]))
            raise OSError(28, "No space left on device")
        return native_write(fd, data)

    with open(path, "wb") as handle:
        with wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframesraw(frames)
        handle.flush()
        monkeypatch.setattr(wav_metadata.os, "write", flaky_write)
        with pytest.raises(OSError, match="No space left"):
            append_origination(handle, WHEN)
        monkeypatch.undo()

    assert path.read_bytes() == _untagged(frames)
    assert read_info_icrd(str(path)) is None


def test_a_recording_too_large_for_riff_is_left_untagged(tmp_path, monkeypatch):
    path = tmp_path / "clip.wav"
    frames = np.arange(64, dtype=np.int16).tobytes()
    monkeypatch.setattr(wav_metadata, "_MAX_RIFF_SIZE", len(_untagged(frames)))
    with pytest.raises(WavMetadataError, match="too large"):
        _write_tagged(path, frames)
    assert path.read_bytes() == _untagged(frames)


def _riff(format_fields: bytes, *, before_format: bytes = b"", data: bytes = b"\x00\x00" * 8) -> bytes:
    body = (b"WAVE" + before_format + b"fmt " + struct.pack("<I", len(format_fields)) + format_fields
            + b"data" + struct.pack("<I", len(data)) + data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _format(tag: int, channels: int, bits: int) -> bytes:
    block = channels * bits // 8
    return struct.pack("<HHIIHH", tag, channels, 44100, 44100 * block, block, bits)


@pytest.mark.parametrize("content, expected", [
    (_riff(_format(1, 1, 16)), True),
    # An odd-sized chunk ahead of fmt, with its pad byte.
    (_riff(_format(1, 1, 16), before_format=b"JUNK" + struct.pack("<I", 3) + b"abc\x00"), True),
    (_riff(_format(1, 2, 16)), False),
    (_riff(_format(1, 1, 8)), False),
    (_riff(_format(1, 1, 24)), False),
    (_riff(_format(3, 1, 32)), False),
    # WAVE_FORMAT_EXTENSIBLE and a compressed codec (MP3) in a WAV.
    (_riff(_format(0xFFFE, 1, 16) + bytes(24)), False),
    (_riff(_format(0x55, 1, 16)), False),
    (_riff(_format(1, 1, 16))[:30], False),
    (b"RIFF" + struct.pack("<I", 4) + b"WAVE", False),
    (b"OggS" + bytes(60), False),
    (b"", False),
])
def test_only_mono_16_bit_pcm_is_recognized(tmp_path, content, expected):
    path = tmp_path / "audio.bin"
    path.write_bytes(content)
    assert is_mono_pcm16(str(path)) is expected


def test_a_recording_is_recognized_and_a_missing_file_is_not(tmp_path):
    path = tmp_path / "clip.wav"
    _write_tagged(path, np.arange(64, dtype=np.int16).tobytes())
    assert is_mono_pcm16(str(path))
    assert not is_mono_pcm16(str(tmp_path / "missing.wav"))
    assert not is_mono_pcm16(str(tmp_path))


def _assert_even_aligned_info_items(data: bytes) -> None:
    offset = 12
    found_info = False
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        payload = data[offset + 8:offset + 8 + size]
        if chunk_id == b"LIST" and payload[:4] == b"INFO":
            found_info = True
            pos = 4
            while pos + 8 <= len(payload):
                item_size = struct.unpack_from("<I", payload, pos + 4)[0]
                item_end = pos + 8 + item_size
                assert item_end <= len(payload)
                if payload[pos:pos + 4] == b"ICRD":
                    assert item_size % 2 == 1
                pos = item_end + (item_size & 1)
            assert pos == len(payload)
        offset = offset + 8 + size + (size & 1)
    assert found_info
