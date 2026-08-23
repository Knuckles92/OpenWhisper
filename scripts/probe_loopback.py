"""Meeting Mode Phase-1 gate: probe the two loopback capture paths.

Run on Windows with the project venv active (sounddevice >= 0.5.0):
    python scripts/probe_loopback.py

Section 1 prints every WASAPI device, flagging '[Loopback]' input devices and
the default render device. Section 2 probes the ``soundcard`` fallback, which
is the operative path on machines whose bundled PortAudio build exposes no
loopback inputs: it lists loopback-capable microphones and records ~1 s from
the default speaker so the printed RMS proves audio actually flows. Play
something audible while running it -- an RMS near zero means silence was
captured, not that the device is broken.
"""
import numpy as np
import sounddevice as sd

#: Seconds of audio captured by the soundcard probe.
PROBE_SECONDS = 1.0

#: Sample rate requested from the soundcard recorder.
PROBE_RATE = 48000


def probe_sounddevice() -> int:
    """Print WASAPI devices and return the number of '[Loopback]' inputs."""
    hostapis = list(sd.query_hostapis())
    devices = list(sd.query_devices())
    print(f"sounddevice {sd.__version__} | {sd.get_portaudio_version()[1]}")
    wasapi = next((i for i, api in enumerate(hostapis)
                   if "wasapi" in str(api["name"]).lower()), None)
    if wasapi is None:
        print("GATE FAILED: no WASAPI host API found.")
        return 0
    default_out = hostapis[wasapi].get("default_output_device", -1)
    print(f"WASAPI host API index {wasapi}; default render device: {default_out}\n")
    loopbacks = 0
    for idx, dev in enumerate(devices):
        if dev["hostapi"] != wasapi:
            continue
        tags = []
        if "[Loopback]" in dev["name"] and dev["max_input_channels"] > 0:
            tags.append("<-- LOOPBACK INPUT")
            loopbacks += 1
        if idx == default_out:
            tags.append("<== DEFAULT RENDER")
        print(f"[{idx:3d}] in={dev['max_input_channels']:2d} "
              f"out={dev['max_output_channels']:2d} "
              f"{int(dev['default_samplerate'])} Hz  {dev['name']}  {' '.join(tags)}")
    print(f"\n{loopbacks} '[Loopback]' input device(s) found. "
          + ("sounddevice loopback available."
             if loopbacks else "sounddevice loopback unavailable."))
    return loopbacks


def probe_soundcard() -> bool:
    """Enumerate soundcard loopback devices and record a short sample.

    Returns:
        True when audio was captured from the default speaker's loopback.
    """
    print("\n--- soundcard fallback ---")
    try:
        import soundcard as sc
    except Exception as exc:
        print(f"soundcard not importable: {exc}")
        return False

    try:
        mics = list(sc.all_microphones(include_loopback=True))
    except Exception as exc:
        print(f"soundcard device enumeration failed: {exc}")
        return False
    for mic in mics:
        tag = "<-- LOOPBACK" if getattr(mic, "isloopback", False) else ""
        print(f"  {mic.name}  (channels={mic.channels}) {tag}")

    try:
        speaker = sc.default_speaker()
        print(f"default speaker: {speaker.name}")
        loop_mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    except Exception as exc:
        print(f"No loopback microphone for the default speaker: {exc}")
        return False

    frames = int(PROBE_SECONDS * PROBE_RATE)
    try:
        with loop_mic.recorder(samplerate=PROBE_RATE) as recorder:
            data = recorder.record(numframes=frames)
    except Exception as exc:
        print(f"soundcard loopback record failed: {exc}")
        return False

    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2 and arr.shape[1] > 1:
        arr = arr.mean(axis=1)
    arr = arr.reshape(-1)
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2))) if arr.size else 0.0
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    print(f"captured {arr.size} frames @ {PROBE_RATE} Hz  "
          f"RMS={rms:.5f}  peak={peak:.5f}")
    if rms < 1e-5:
        print("  (near-silence: play audio through the default speaker and "
              "re-run to confirm the path)")
    return arr.size > 0


def main() -> None:
    loopbacks = probe_sounddevice()
    soundcard_ok = probe_soundcard()
    print("\n=== GATE ===")
    if loopbacks:
        print("PASSED: sounddevice WASAPI loopback is available (primary path).")
    elif soundcard_ok:
        print("PASSED: no sounddevice loopback; 'soundcard' fallback captures "
              "system audio (operative path).")
    else:
        print("FAILED: neither sounddevice loopback nor 'soundcard' can "
              "capture system audio; meetings will be mic-only.")


if __name__ == "__main__":
    main()
