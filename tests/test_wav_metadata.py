"""RIFF/WAVE origination tags used by File Explorer."""
import struct
import wave
from datetime import datetime

import numpy as np

from services.wav_metadata import (
    read_bext_origination,
    read_info_icrd,
    read_info_isft,
    stamp_wav_origination,
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
