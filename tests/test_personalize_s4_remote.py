"""Remote engine vocabulary hints: the capability, the header and the host's checks."""
from types import SimpleNamespace

import numpy as np
import pytest

from services.recognition_context import RecognitionContext
from services.remote_asr import protocol
from services.remote_asr.engines import SpeechWorkerEngine, UnavailableEngine, WhisperEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from tests.test_remote_engine import (  # noqa: F401  (fixtures)
    FakeEngine,
    ListStore,
    _connect,
    _pair,
    _tone,
    identity,
    no_real_tailscale,
)

PHRASES = ["Ksenia", "Kubernetes"]


class HintEngine(FakeEngine):
    """A host engine that uses vocabulary hints, like Whisper's."""

    accepts_phrases = True

    def transcribe(self, audio, language, *, phrases=()):
        self.calls.append(("transcribe", len(audio), language, tuple(phrases)))
        return {"text": ",".join(phrases) or "plain", "segments": []}


def start_host(engine, identity):  # noqa: F811
    store = ListStore()
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=identity,
        host_name="devbox",
    )
    speech_host.start(port=0, bind="127.0.0.1")
    return speech_host


@pytest.fixture
def hint_host(identity):  # noqa: F811
    engine = HintEngine()
    speech_host = start_host(engine, identity)
    yield speech_host, engine
    speech_host.stop()


@pytest.fixture
def plain_host(identity):  # noqa: F811
    engine = FakeEngine()
    speech_host = start_host(engine, identity)
    yield speech_host, engine
    speech_host.stop()


@pytest.fixture
def tone_wav(tmp_path):
    import wave

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes((_tone(16000) * 32767).astype("<i2").tobytes())
    return str(path)


def paired(speech_host):
    from services.remote_asr import settings as remote_settings
    from transcriber.remote_backend import RemoteSpeechBackend

    remote_settings.save_client_pairing("127.0.0.1", speech_host.port, _pair(speech_host))
    backend = RemoteSpeechBackend()
    backend.reload_model()
    assert backend.is_available(), backend.last_error
    return backend


class TestHeaderPhrases:
    def test_valid_phrases_pass(self):
        assert protocol.header_phrases({"phrases": ["Ksenia", "  Kuber   netes "]}) == ("Ksenia", "Kuber netes")

    @pytest.mark.parametrize("value", [None, "Ksenia", {"a": 1}, 5, [], [None, 3, "", "  "]])
    def test_malformed_values_give_nothing(self, value):
        assert protocol.header_phrases({"phrases": value}) == ()
        assert protocol.header_phrases({}) == ()

    def test_limits(self):
        phrases = protocol.header_phrases({"phrases": ["x" * 65] + [f"w{i}" for i in range(60)]})
        assert len(phrases) == protocol.MAX_HINT_PHRASES and phrases[0] == "w0"

    def test_a_full_header_still_fits_the_frame_limit(self):
        header = {"id": 1, "op": "transcribe", "language": "en",
                  "phrases": ["é" * protocol.MAX_HINT_CHARS] * protocol.MAX_HINT_PHRASES}
        frame = protocol.pack_request(header, np.zeros(16, np.float32))
        assert protocol.unpack_frame(frame)[0]["phrases"] == header["phrases"]


def test_round_trip_reaches_an_engine_that_uses_hints(hint_host):
    speech_host, engine = hint_host
    connection = _connect(speech_host, _pair(speech_host))
    try:
        assert connection.ready["capabilities"]["recognition_hints"] is True
        result = connection.request("transcribe", audio=_tone(1600), language="en",
                                    phrases=PHRASES + ["x" * 65, 7])
        assert result["text"] == "Ksenia,Kubernetes"
        assert engine.calls[-1] == ("transcribe", 1600, "en", ("Ksenia", "Kubernetes"))
        connection.request("transcribe", audio=_tone(1600), language="en")
        assert engine.calls[-1][3] == ()
    finally:
        connection.close()


def test_a_host_without_the_capability_ignores_phrases(plain_host):
    speech_host, engine = plain_host
    connection = _connect(speech_host, _pair(speech_host))
    try:
        assert connection.ready["capabilities"]["recognition_hints"] is False
        result = connection.request("transcribe", audio=_tone(1600), language="en", phrases=PHRASES)
        assert result["text"] == "heard 1600"
        assert engine.calls[-1][0] == "transcribe" and engine.calls[-1][2] == "en"
    finally:
        connection.close()


def test_phrases_never_change_the_engine_identity(hint_host):
    speech_host, engine = hint_host
    before = engine.identity
    connection = _connect(speech_host, _pair(speech_host))
    try:
        connection.request("transcribe", audio=_tone(1600), language="en", phrases=PHRASES)
        assert engine.identity == before
        assert connection.request("describe")["family"] == "parakeet"
    finally:
        connection.close()


def test_client_sends_phrases_only_to_a_host_that_uses_them(hint_host, tone_wav):
    speech_host, engine = hint_host
    backend = paired(speech_host)
    try:
        assert backend.recognition_support == "model"
        text = backend.transcribe(tone_wav, recognition=RecognitionContext(language="fr", phrases=tuple(PHRASES)))
        assert text == "Ksenia,Kubernetes"
        assert engine.calls[-1][2:] == ("fr", ("Ksenia", "Kubernetes"))
        backend.transcribe(tone_wav)
        assert engine.calls[-1][3] == ()
    finally:
        backend.cleanup()
    assert backend.recognition_support == "after"


def test_client_keeps_phrases_from_a_host_without_the_capability(plain_host, monkeypatch, tone_wav):
    speech_host, engine = plain_host
    backend = paired(speech_host)
    sent = []
    process = backend._process
    original = process.request_timed

    def spy(op, **fields):
        sent.append(fields)
        return original(op, **fields)

    monkeypatch.setattr(process, "request_timed", spy)
    try:
        assert backend.recognition_support == "after"
        backend.transcribe(tone_wav, recognition=RecognitionContext(phrases=tuple(PHRASES)))
        assert sent and all("phrases" not in fields for fields in sent)
    finally:
        backend.cleanup()


def test_host_engines_declare_whether_they_use_hints(monkeypatch):
    from services.local_asr import nvidia
    from transcriber.optional_backend import LocalSpeechBackend

    assert WhisperEngine(SimpleNamespace()).accepts_phrases is True
    assert UnavailableEngine("off").accepts_phrases is False
    parakeet = SpeechWorkerEngine(LocalSpeechBackend("parakeet"))
    nemotron = SpeechWorkerEngine(LocalSpeechBackend("nemotron"))
    assert not parakeet.accepts_phrases and not nemotron.accepts_phrases
    monkeypatch.setattr(nvidia, "WORD_BOOSTING", True)
    assert nemotron.accepts_phrases and not parakeet.accepts_phrases


def test_speech_worker_engine_passes_phrases_to_recognize():
    calls = []
    backend = SimpleNamespace(recognize=lambda audio, language, **kw: calls.append((language, kw)) or {"text": ""})
    engine = SpeechWorkerEngine(backend)
    engine.transcribe(np.zeros(4), "en")
    engine.transcribe(np.zeros(4), "en", phrases=("Ksenia",))
    assert calls == [("en", {}), ("en", {"phrases": ("Ksenia",)})]


def test_whisper_engine_turns_phrases_into_hotwords():
    calls = []

    class Model:
        def transcribe(self, audio, **options):
            calls.append(options)
            return iter([]), SimpleNamespace(language="en")

    engine = WhisperEngine(SimpleNamespace(model=Model()))
    engine.transcribe(np.zeros(16000, np.float32), "en")
    engine.transcribe(np.zeros(16000, np.float32), "en", phrases=("Ksenia", "Kubernetes"))
    assert "hotwords" not in calls[0]
    assert calls[1]["hotwords"] == "Ksenia Kubernetes"

