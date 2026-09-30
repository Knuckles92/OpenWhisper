"""RIFF/WAVE origination tags used by File Explorer."""
import struct
import wave
from datetime import datetime

import numpy as np
import pytest

from services.wav_metadata import (
    read_bext_origination,
    read_info_icrd,
    read_info_isft,
    stamp_wav_origination,
    WavMetadataError,
)
from tests.helpers import write_wav

WHEN = datetime(2026, 9, 19, 13, 5, 7)


def test_stamp_round_trip(tmp_path):
    path = tmp_path / "clip.wav"
    write_wav(path)

    assert stamp_wav_origination(str(path), WHEN)
    assert read_info_icrd(str(path)) == "2026-09-19"
    assert read_info_isft(str(path)) == "OpenWhisper"
    assert read_bext_origination(str(path)) == ("2026-09-19", "13:05:07")


def test_stamp_preserves_pcm_and_even_alignment(tmp_path):
    path = tmp_path / "clip.wav"
    frames = np.arange(64, dtype=np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames.tobytes())

    assert stamp_wav_origination(str(path), WHEN)

    with wave.open(str(path), "rb") as handle:
        assert handle.readframes(len(frames)) == frames.tobytes()

    data = path.read_bytes()
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8
    _assert_even_aligned_info_items(data)


def test_non_wav_is_left_alone(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")

    assert stamp_wav_origination(str(path), WHEN) is False
    assert path.read_bytes() == b"hello"
    assert read_info_icrd(str(path)) is None
    assert read_bext_origination(str(path)) is None


def test_already_tagged_file_is_left_alone(tmp_path):
    path = tmp_path / "clip.wav"
    write_wav(path)
    assert stamp_wav_origination(str(path), WHEN)
    original = path.read_bytes()

    assert stamp_wav_origination(str(path), datetime(2026, 6, 6, 6, 6, 6)) is False
    assert path.read_bytes() == original
    assert read_info_icrd(str(path)) == "2026-09-19"


def test_list_adtl_does_not_block_info_stamp(tmp_path):
    path = tmp_path / "clip.wav"
    write_wav(path)
    data = path.read_bytes()
    adtl = b"LIST" + struct.pack("<I", 4) + b"adtl"
    tagged = data + adtl
    path.write_bytes(tagged[:4] + struct.pack("<I", len(tagged) - 8) + tagged[8:])

    assert stamp_wav_origination(str(path), WHEN)
    assert read_info_icrd(str(path)) == "2026-09-19"


def test_unknown_chunks_and_their_padding_are_preserved_exactly(tmp_path):
    path = tmp_path / "unknown.wav"
    write_wav(path)
    original = path.read_bytes()
    # Odd-length unknown payload with a non-zero (but valid) pad byte.
    junk = b"JUNK" + struct.pack("<I", 3) + b"abc" + b"\x7f"
    extra = b"XTRA" + struct.pack("<I", 4) + b"keep"
    original = original[:12] + junk + original[12:] + extra
    original = original[:4] + struct.pack("<I", len(original) - 8) + original[8:]
    path.write_bytes(original)

    assert stamp_wav_origination(str(path), WHEN)
    stamped = path.read_bytes()
    assert stamped[:4] == original[:4]
    assert stamped[8:len(original)] == original[8:]
    assert read_info_icrd(str(path)) == "2026-09-19"
    assert struct.unpack_from("<I", stamped, 4)[0] == len(stamped) - 8


def test_existing_bext_without_info_is_left_unchanged(tmp_path):
    path = tmp_path / "bext.wav"
    write_wav(path)
    original = path.read_bytes() + b"bext" + struct.pack("<I", 602) + bytes(602)
    original = original[:4] + struct.pack("<I", len(original) - 8) + original[8:]
    path.write_bytes(original)

    assert stamp_wav_origination(str(path), WHEN) is False
    assert path.read_bytes() == original


def test_odd_length_pcm_gets_padding_before_metadata(tmp_path):
    path = tmp_path / "odd.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 1, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\x80\x81\x7f")
    assert stamp_wav_origination(str(path), WHEN)
    with wave.open(str(path), "rb") as handle:
        assert handle.readframes(3) == b"\x80\x81\x7f"
    assert read_info_icrd(str(path)) == "2026-09-19"
    assert read_bext_origination(str(path)) == ("2026-09-19", "13:05:07")


def test_stamping_never_reads_the_entire_audio_payload(tmp_path, monkeypatch):
    import services.wav_metadata as module

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for _ in range(6):
            handle.writeframesraw(bytes(module.COPY_BLOCK_BYTES))
    native_open = open
    reads = []

    class BoundedReader:
        def __init__(self, *args, **kwargs):
            self.handle = native_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            assert 0 <= size <= module.COPY_BLOCK_BYTES
            reads.append(size)
            return self.handle.read(size)

    with monkeypatch.context() as patch:
        patch.setattr(module, "open", BoundedReader, raising=False)
        assert stamp_wav_origination(str(path), WHEN)
    assert reads.count(module.COPY_BLOCK_BYTES) == 6
    assert read_info_icrd(str(path)) == "2026-09-19"


@pytest.mark.parametrize("failure", ["read", "fsync", "replace"])
def test_stamping_failure_preserves_original_and_removes_temp(tmp_path, monkeypatch, failure):
    import services.wav_metadata as module

    path = tmp_path / "original.wav"
    write_wav(path)
    original = path.read_bytes()

    def fail(*_):
        raise OSError("injected storage failure")

    native_open = open

    class FailingReader:
        def __init__(self, *args, **kwargs):
            self.handle = native_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            if size > 12:
                fail()
            return self.handle.read(size)

    with monkeypatch.context() as patch:
        if failure == "read":
            patch.setattr(module, "open", FailingReader, raising=False)
        else:
            patch.setattr(module.os, failure, fail)
        with pytest.raises(OSError, match="injected storage failure"):
            stamp_wav_origination(str(path), WHEN)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_truncated_chunk_is_not_modified(tmp_path):
    path = tmp_path / "truncated.wav"
    original = b"RIFF" + struct.pack("<I", 100) + b"WAVEdata" + struct.pack("<I", 88) + b"short"
    path.write_bytes(original)
    with pytest.raises(WavMetadataError, match="Truncated RIFF chunk payload"):
        stamp_wav_origination(str(path), WHEN)
    assert path.read_bytes() == original


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
