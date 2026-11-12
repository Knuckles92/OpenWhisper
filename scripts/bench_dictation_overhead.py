"""Measure what OpenWhisper itself adds to a dictation, with no speech engine.

Three parts, each against the real code with the slow, variable work (the
microphone, the speech model, the network) taken out, so the numbers are the
app's own overhead on this computer:

``stop``    From the microphone stream closing to 16 kHz audio ready to
            decode: the recording journal's last write, the WAV save, and
            the conversion every optional engine and the Remote engine use.
``remote``  Round trips between a host and a paired client over TLS on
            127.0.0.1, the host's engine answering at once.
``api``     One upload by the API engine to a server on this computer that
            answers at once, with and without the worker readied ahead.

    python scripts/bench_dictation_overhead.py
    python scripts/bench_dictation_overhead.py --only stop --seconds 30 --runs 50
    python scripts/bench_dictation_overhead.py --dictionary-terms 1000 --output results.json

``--seconds`` is the recording's length, ``--runs`` the samples per number,
``--dictionary-terms`` how long a dictionary the host's settings file holds
(a host reads settings for every request), ``--only`` one part, and
``--output`` a JSON file for the medians. Everything is written under a
temporary data folder; no settings, recordings or pairings of yours are read.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RECORDER_RATE = 44100
RECORDER_BLOCK = 1024


def _report(results: dict, part: str, label: str, samples: list) -> None:
    median = statistics.median(samples)
    print(f"  {label:<52} median {median:8.2f} ms   min {min(samples):8.2f}   max {max(samples):8.2f}")
    results.setdefault(part, {})[label] = round(median, 3)


def _timed(call, runs: int) -> list:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def bench_stop(results: dict, data: str, seconds: float, runs: int) -> None:
    import numpy as np

    from services.local_asr.audio import windows
    from services.recorder import AudioRecorder
    from services.recording_journal import RecordingJournal

    print(f"stop: a {seconds:g} s recording, stream closed -> audio ready to decode")
    rng = np.random.default_rng(0)
    blocks = [(rng.standard_normal(RECORDER_BLOCK) * 3000).astype(np.int16).tobytes()
              for _ in range(64)]
    count = int(seconds * RECORDER_RATE / RECORDER_BLOCK)
    output = os.path.join(data, "recorded.wav")
    finish, save, convert, total = [], [], [], []
    random.seed(1)
    for _ in range(runs):
        recorder = AudioRecorder(output_file=output)
        journal = RecordingJournal(output, RECORDER_RATE, 1, 2, lambda _message: None)
        # What a capture leaves behind, without a microphone to make it.
        recorder._audio_spool = journal
        for index in range(count):
            journal.append(blocks[index % len(blocks)])
            if index % 256 == 255:
                # Far faster than a microphone; let the writer keep up.
                journal._queue.join()
        recorder._recorded_bytes = count * RECORDER_BLOCK * 2
        recorder._recorded_sample_frames = count * RECORDER_BLOCK
        journal._queue.join()
        # The writer idles through a post-roll before the stream closes.
        time.sleep(random.uniform(0.06, 0.16))
        started = time.perf_counter()
        journal.finish()
        finished = time.perf_counter()
        if not recorder.save_recording(allow_incomplete=True):
            raise SystemExit("The benchmark recording could not be saved")
        saved = time.perf_counter()
        for _window in windows(output):
            pass
        converted = time.perf_counter()
        finish.append((finished - started) * 1000)
        save.append((saved - finished) * 1000)
        convert.append((converted - saved) * 1000)
        total.append((converted - started) * 1000)
        recorder.clear_recording_data(discard=True)
    _report(results, "stop", "journal's last write", finish)
    _report(results, "stop", "WAV save", save)
    _report(results, "stop", "conversion to 16 kHz windows", convert)
    _report(results, "stop", "total", total)


def _settings_with_dictionary(terms: int) -> dict:
    settings = {f"setting_{index}": f"value-{index}" * 3 for index in range(120)}
    settings["dictation_dictionary"] = [
        {"id": f"term-{index}", "term": f"Kubernetes{index}", "sounds_like": [f"cooper netties {index}"],
         "starred": index % 7 == 0, "uses": index, "added_at": "2026-10-01T10:00:00+00:00"}
        for index in range(terms)
    ]
    return settings


def bench_remote(results: dict, data: str, seconds: float, runs: int, terms: int) -> None:
    import numpy as np

    from services.remote_asr import settings as remote_settings
    from services.remote_asr.client import RemoteConnection, pair_with_host
    from services.remote_asr.engines import SpeechWorkerEngine
    from services.remote_asr.host import DeviceRegistry, SpeechHost
    from services.remote_asr.tls import ensure_host_identity
    from services.settings import settings_manager

    class InstantBackend:
        """What SpeechWorkerEngine reads of a backend; every decode returns at once."""

        backend_id, model_name, device = "parakeet", "parakeet-v3", "cuda"
        name, device_info, recognition_support = "Parakeet", "Parakeet | cuda", "after"

        def is_available(self):
            return True

        def recognize(self, audio, language=None, phrases=()):
            return {"text": "hello world", "segments": [{"text": "hello world", "start": 0.0, "end": 1.0}]}

        def stream_audio(self, session, audio, language=None, *, finish=False):
            return [{"text": "hello", "final": finish, "end": len(audio) / 16000}]

        def cancel_stream(self, session):
            return None

    settings_manager.save_all_settings(_settings_with_dictionary(terms))
    size_kb = os.path.getsize(settings_manager.settings_file) / 1024
    print(f"remote: host and client on 127.0.0.1, host settings {size_kb:.0f} KB ({terms} dictionary terms)")
    engine = SpeechWorkerEngine(InstantBackend())
    host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(remote_settings.load_host_devices, remote_settings.save_host_devices),
        identity=ensure_host_identity(os.path.join(data, "identity")),
        host_name="bench-host",
    )
    host.start(port=0, bind="127.0.0.1")
    try:
        paired = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "bench-client")
        connection = RemoteConnection("127.0.0.1", host.port, paired.token, paired.fingerprint)
        connection.connect()
        rng = np.random.default_rng(0)
        preview = (rng.standard_normal(12000) * 0.1).astype(np.float32)
        recording = (rng.standard_normal(int(seconds * 16000)) * 0.1).astype(np.float32)
        for _ in range(20):
            connection.request("describe")
        _report(results, "remote", "request without audio", _timed(
            lambda: connection.request("describe"), runs))
        _report(results, "remote", "preview push, 0.75 s of audio", _timed(
            lambda: connection.request("stream", audio=preview, session="s", finish=False, language="en"),
            runs))
        _report(results, "remote", f"transcribe, {seconds:g} s of audio", _timed(
            lambda: connection.request("transcribe", audio=recording, language="en"), runs))
        _report(results, "remote", "host: check the device's pairing", _timed(
            lambda: host.registry.authenticate(paired.token), runs))
        _report(results, "remote", "host: engine identity", _timed(lambda: engine.identity, runs))
        _report(results, "remote", "a copy of every setting", _timed(
            settings_manager.load_all_settings, runs))
        connection.close()
    finally:
        host.stop()


class _InstantTranscription(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"text": "hello world"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def bench_api(results: dict, data: str, seconds: float, runs: int) -> None:
    import wave

    from services.isolated import IsolatedOpenAIClient

    print(f"api: upload of a {seconds:g} s recording to a server on this computer")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _InstantTranscription)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = os.path.join(data, "upload.wav")
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RECORDER_RATE)
        wav.writeframes(bytes(int(seconds * RECORDER_RATE) * 2))
    client = IsolatedOpenAIClient(
        api_key="sk-benchmark", base_url=f"http://127.0.0.1:{server.server_port}/v1", read_timeout=30.)

    def upload():
        with open(path, "rb") as audio:
            client.audio.transcriptions.create(model="whisper-1", file=audio, response_format="json")

    try:
        runs = max(3, min(runs, 8))  # each cold run starts a process
        _report(results, "api", "upload, worker started for it", _timed(upload, runs))
        ready = []
        for _ in range(runs):
            client.warm()  # what a recording's start does
            ready.extend(_timed(upload, 1))
        _report(results, "api", "upload, worker readied while recording", ready)
    finally:
        client.close()
        server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=("stop", "remote", "api"))
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--dictionary-terms", type=int, default=150)
    parser.add_argument("--output")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="openwhisper-bench-") as data:
        # Before any import below resolves the data folder.
        os.environ["OPENWHISPER_DATA_DIR"] = data
        import logging

        logging.disable(logging.WARNING)
        results: dict = {}
        if args.only in (None, "stop"):
            bench_stop(results, data, args.seconds, args.runs)
        if args.only in (None, "remote"):
            bench_remote(results, data, args.seconds, max(args.runs, 50), args.dictionary_terms)
        if args.only in (None, "api"):
            bench_api(results, data, args.seconds, args.runs)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump({"seconds": args.seconds, "medians_ms": results}, handle, indent=2)
        from services.local_asr.process import close_all_workers

        close_all_workers()


if __name__ == "__main__":
    main()
