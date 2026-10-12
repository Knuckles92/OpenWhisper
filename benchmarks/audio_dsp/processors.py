"""Experimental controls and a direct RNNoise C-ABI adapter, outside the app."""

from __future__ import annotations

import ctypes
import math
import time
from pathlib import Path

import numpy as np

from benchmarks.audio_dsp.audio import FRAME, RATE, db, rms
from benchmarks.provenance import file_identity

PROFILES = (
    "raw",
    "highpass",
    "gate_agc",
    "rnnoise",
    "rnnoise_conditioned",
    "audioforge_rnnoise",
    "audioforge_full",
)
CONTROL_SETTINGS = {
    "highpass_hz": 80,
    "gate_threshold_dbfs": -45,
    "gate_hold_ms": 100,
    "gate_attack_ms": 5,
    "gate_release_ms": 100,
    "agc_target_dbfs": -24,
    "agc_max_gain_db": 18,
    "agc_time_ms": 500,
    "agc_noise_floor_dbfs": -55,
}


class Control:
    """Simple causal baseline, not AudioForge's gate/compressor or a production preset."""

    latency_samples = 0

    def __init__(self, highpass=True, levels=False):
        self.highpass = highpass
        self.levels = levels
        omega = 2 * math.pi * CONTROL_SETTINGS["highpass_hz"] / RATE
        alpha = math.sin(omega) / (2 * math.sqrt(0.5))
        a0 = 1 + alpha
        self.b0 = (1 + math.cos(omega)) / (2 * a0)
        self.b1 = -(1 + math.cos(omega)) / a0
        self.b2 = self.b0
        self.a1 = -2 * math.cos(omega) / a0
        self.a2 = (1 - alpha) / a0
        self.z1 = self.z2 = 0.0
        self.gate_gain = self.agc_gain = 1.0
        self.hold = 0

    def process(self, frame):
        out = frame.copy()
        if self.highpass:
            for index, sample in enumerate(frame):
                value = self.b0 * float(sample) + self.z1
                self.z1 = self.b1 * float(sample) - self.a1 * value + self.z2
                self.z2 = self.b2 * float(sample) - self.a2 * value
                out[index] = value
        if self.levels:
            level = db(rms(out))
            if level >= CONTROL_SETTINGS["gate_threshold_dbfs"]:
                self.hold = 10
            else:
                self.hold = max(0, self.hold - 1)
            target = float(self.hold > 0)
            tau = (
                CONTROL_SETTINGS["gate_attack_ms"]
                if target > self.gate_gain
                else CONTROL_SETTINGS["gate_release_ms"]
            )
            gain = target + (self.gate_gain - target) * math.exp(-10 / tau)
            out *= np.linspace(self.gate_gain, gain, len(out), dtype=np.float32)
            self.gate_gain = gain
            # Freeze gain in pauses so AGC does not amplify a quiet room.
            if level > CONTROL_SETTINGS["agc_noise_floor_dbfs"]:
                target_db = np.clip(
                    CONTROL_SETTINGS["agc_target_dbfs"] - level,
                    -18,
                    CONTROL_SETTINGS["agc_max_gain_db"],
                )
                target_gain = 10 ** (float(target_db) / 20)
                gain = target_gain + (self.agc_gain - target_gain) * math.exp(
                    -10 / CONTROL_SETTINGS["agc_time_ms"]
                )
                out *= np.linspace(self.agc_gain, gain, len(out), dtype=np.float32)
                self.agc_gain = gain
            else:
                out *= self.agc_gain
            out = np.clip(out, -0.98, 0.98)
        return out

    def close(self):
        pass


class RNNoise:
    """One persistent state per capture; 48 kHz, 480 samples, PCM-scale float32."""

    # RNNoise's overlap/add analysis introduces one frame of signal delay.
    latency_samples = FRAME

    def __init__(self, library):
        if library is None:
            raise ValueError(
                "RNNoise requires --rnnoise-library pointing to a local DLL/.so/.dylib"
            )
        self.library = ctypes.CDLL(str(Path(library).resolve()))
        pointer = ctypes.POINTER(ctypes.c_float)
        self.library.rnnoise_get_frame_size.argtypes = []
        self.library.rnnoise_get_frame_size.restype = ctypes.c_int
        self.library.rnnoise_create.argtypes = [ctypes.c_void_p]
        self.library.rnnoise_create.restype = ctypes.c_void_p
        self.library.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.library.rnnoise_destroy.restype = None
        self.library.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            pointer,
            pointer,
        ]
        self.library.rnnoise_process_frame.restype = ctypes.c_float
        if self.library.rnnoise_get_frame_size() != FRAME:
            raise ValueError("Unsupported RNNoise frame size")
        self.state = self.library.rnnoise_create(None)
        if not self.state:
            raise RuntimeError("RNNoise state allocation failed")

    def process(self, frame):
        audio = np.ascontiguousarray(frame * 32768, dtype=np.float32)
        output = np.empty(FRAME, dtype=np.float32)
        pointer = ctypes.POINTER(ctypes.c_float)
        self.library.rnnoise_process_frame(
            self.state, output.ctypes.data_as(pointer), audio.ctypes.data_as(pointer)
        )
        return output / 32768

    def close(self):
        if self.state:
            self.library.rnnoise_destroy(self.state)
            self.state = None


class Chain:
    def __init__(self, processors):
        self.processors = processors
        self.latency_samples = sum(p.latency_samples for p in processors)

    def process(self, frame):
        for processor in self.processors:
            frame = processor.process(frame)
        return frame

    def close(self):
        for processor in self.processors:
            processor.close()


def make_processor(profile, library=None):
    if profile == "raw":
        return Control(highpass=False)
    if profile == "highpass":
        return Control()
    if profile == "gate_agc":
        return Control(levels=True)
    if profile == "rnnoise":
        return RNNoise(library)
    if profile == "rnnoise_conditioned":
        return Chain(
            [Control(), RNNoise(library), Control(highpass=False, levels=True)]
        )
    raise ValueError(f"Unknown frame processor: {profile}")


def render(audio, factory):
    """Include cold first frame; flush delayed tails and preserve capture length."""
    started = time.perf_counter()
    processor = factory()
    initialization_ms = (time.perf_counter() - started) * 1000
    frames, timings = [], []
    try:
        latency = processor.latency_samples
        padded = np.pad(
            audio, (0, (-len(audio)) % FRAME + math.ceil(latency / FRAME) * FRAME)
        )
        for start in range(0, len(padded), FRAME):
            frame = padded[start : start + FRAME]
            started = time.perf_counter()
            output = processor.process(frame)
            timings.append((time.perf_counter() - started) * 1000)
            if output.shape != frame.shape or not np.isfinite(output).all():
                raise ValueError("Invalid DSP frame")
            frames.append(output)
        result = np.concatenate(frames)[latency : latency + len(audio)]
        return result, {
            "initialization_ms": initialization_ms,
            "frame_ms": timings,
            "algorithmic_latency_ms": latency / RATE * 1000,
        }
    finally:
        processor.close()


def processor_identity(profile, library):
    result = {
        "profile": profile,
        "controls": CONTROL_SETTINGS
        if profile in ("highpass", "gate_agc", "rnnoise_conditioned")
        else None,
    }
    if profile.startswith("rnnoise") and library:
        result["library"] = str(Path(library).resolve())
        result["library_sha256"] = file_identity(library)
        result["implementation"] = (
            "RNNoise C ABI; not AudioForge's nnnoiseless Rust implementation"
        )
    return result
