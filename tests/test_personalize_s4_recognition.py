"""RecognitionContext language and phrases reach local engines, and only when set."""
import array
import ctypes
import gc
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services import recognition_context
from services.local_asr import nvidia
from services.recognition_context import RecognitionContext
from tests.test_local_asr_languages import run_worker
from transcriber.local_backend import LocalWhisperBackend, whisper_hints
from transcriber.optional_backend import LocalSpeechBackend

PHRASES = ("Ksenia", "Kubernetes")


class TestTranscribeSeam:
    def test_empty_or_unsupported_calls_as_before(self):
        incremental = Mock()
        supported = SimpleNamespace(supports_recognition=True)
        for backend, recognition in [
            (supported, None), (supported, RecognitionContext()),
            (SimpleNamespace(), RecognitionContext(phrases=PHRASES)),
            (Mock(), RecognitionContext(language="de")),
        ]:
            incremental.reset_mock()
            recognition_context.transcribe(incremental, backend, "a.wav", recognition)
            incremental.transcribe.assert_called_once_with(backend, "a.wav")

    def test_a_context_reaches_a_backend_that_takes_one(self):
        incremental = Mock()
        backend = SimpleNamespace(supports_recognition=True)
        recognition = RecognitionContext(language="fr", phrases=PHRASES)
        recognition_context.transcribe(incremental, backend, "a.wav", recognition)
        incremental.transcribe.assert_called_once_with(backend, "a.wav", recognition=recognition)


class TestHelpers:
    @pytest.mark.parametrize("language,code", [
        ("", ""), (None, ""), ("auto", ""), ("AUTO", ""), ("en", "en"), ("pt-BR", "pt"), ("zh_CN", "zh"),
    ])
    def test_language_code(self, language, code):
        assert recognition_context.language_code(language) == code

    def test_hotwords_keep_the_first_phrases_that_fit(self):
        assert recognition_context.hotwords(["Alpha", "Bravo", "Charlie"], max_chars=11) == "Alpha Bravo"
        assert recognition_context.hotwords(["x" * 65, "Bravo"]) == "Bravo"
        assert recognition_context.hotwords([]) == ""
        many = [f"Word{i:03d}" for i in range(100)]
        assert len(recognition_context.hotwords(many)) <= recognition_context.WHISPER_HOTWORDS_MAX_CHARS

    def test_keywords_and_prompt(self):
        assert recognition_context.keywords([f"w{i}" for i in range(60)]) == [f"w{i}" for i in range(50)]
        assert recognition_context.keywords(["  spaced   out ", "x" * 65]) == ["spaced out"]
        assert recognition_context.vocabulary_prompt(["Ksenia", "Kubernetes"]) == "Ksenia, Kubernetes."
        assert recognition_context.vocabulary_prompt([]) == ""
        long = recognition_context.vocabulary_prompt([f"Word{i:03d}" for i in range(200)])
        assert len(long) <= recognition_context.OPENAI_PROMPT_MAX_CHARS and long.endswith(".")


class TestLocalWhisper:
    @pytest.fixture
    def backend(self):
        backend = LocalWhisperBackend(model_name="base", load=False)
        backend.model = Mock()
        backend.model.transcribe.return_value = (
            iter([SimpleNamespace(text="hi")]), SimpleNamespace(language="en", language_probability=1.0),
        )
        return backend

    def test_hotwords_and_language_are_added(self, backend):
        assert backend.supports_recognition is True and backend.recognition_support == "model"
        backend.transcribe("a.wav", recognition=RecognitionContext(language="de-AT", phrases=PHRASES))
        kwargs = backend.model.transcribe.call_args.kwargs
        assert kwargs["hotwords"] == "Ksenia Kubernetes" and kwargs["language"] == "de"

    @pytest.mark.parametrize("recognition", [None, RecognitionContext(), RecognitionContext(language="auto")])
    def test_nothing_is_added_without_hints(self, backend, recognition):
        backend.transcribe("a.wav", recognition=recognition)
        assert set(backend.model.transcribe.call_args.kwargs) == {"beam_size", "vad_filter", "vad_parameters"}

    def test_options_stay_json_for_the_isolated_worker(self):
        import json

        hints = whisper_hints(RecognitionContext(language="fr", phrases=PHRASES))
        assert json.loads(json.dumps(hints)) == hints


def test_a_dictation_reaches_whisper_with_hints_and_replacements(tmp_path):
    """Saved dictionary → job context → hotwords, then "sounds like" fixes the text."""
    from services.dictation_pipeline import begin_job
    from services.runtime.transcription import TranscriptionRuntime
    from services.settings import SettingsKey, settings_manager

    settings_manager.update_settings({SettingsKey.DICTATION_DICTIONARY: [
        {"id": "a", "term": "Ksenia", "heard": ["Sonia"], "starred": True},
        {"id": "b", "term": "Kubernetes"},
    ]})
    backend = LocalWhisperBackend(model_name="base", load=False)
    backend.model = Mock()
    backend.model.transcribe.return_value = (
        iter([SimpleNamespace(text="Ask Sonia about the cluster")]),
        SimpleNamespace(language="en", language_probability=1.0),
    )
    controller = Mock(current_backend=backend, _pending_file_size=None, _pending_audio_path=None)
    runtime = TranscriptionRuntime(controller)
    job = begin_job("dictation", settings_manager.load_all_settings())
    runtime._active_job = job
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF")

    runtime.transcribe_audio_file(str(path))

    assert job.recognition.phrases == ("Ksenia", "Kubernetes")
    assert backend.model.transcribe.call_args.kwargs["hotwords"] == "Ksenia Kubernetes"
    controller.transcription_completed.emit.assert_called_once_with(
        "Ask Ksenia about the cluster", None, None
    )


class Recognize:
    """Fake worker call recording what each window was asked with."""

    def __init__(self):
        self.calls = []

    def __call__(self, audio, language=None, **options):
        self.calls.append((language, options))
        return {"text": "word", "segments": []}


@pytest.fixture
def speech(monkeypatch, tmp_path):
    from tests.helpers import write_wav

    def make(family):
        backend = LocalSpeechBackend(family)
        backend.is_available = lambda: True
        backend._recognize = Recognize()
        return backend

    path = tmp_path / "speech.wav"
    write_wav(path, value=8000, duration_s=2.0)
    return make, str(path)


class TestLocalSpeech:
    def test_language_comes_from_the_context_per_request(self, speech):
        make, path = speech
        backend = make("parakeet")
        assert backend.supports_recognition and backend.recognition_support == "after"
        backend.transcribe(path, recognition=RecognitionContext(language="fr", phrases=PHRASES))
        backend.transcribe(path, recognition=RecognitionContext(language="de"))
        backend.transcribe(path)
        assert [call[0] for call in backend._recognize.calls] == ["fr", "auto", None]
        assert all(options == {} for _language, options in backend._recognize.calls)

    def test_an_engine_without_languages_keeps_the_code(self, speech):
        make, _path = speech
        backend = make("parakeet")
        backend.backend_id = "local_whisper"  # a remote host's Whisper
        assert backend.recognition_inputs(RecognitionContext(language="de")) == ("de", ())

    def test_no_language_leaves_the_engine_setting(self, speech):
        from services.settings import settings_manager

        make, _path = speech
        settings_manager.update_settings({"local_asr_language": "es"})
        assert make("parakeet").recognition_inputs(RecognitionContext(phrases=PHRASES)) == ("es", ())

    def test_nemotron_phrases_only_once_boosting_is_verified(self, speech, monkeypatch):
        make, path = speech
        backend = make("nemotron")
        recognition = RecognitionContext(phrases=PHRASES)
        backend.transcribe(path, recognition=recognition)
        assert backend.recognition_support == "after"
        assert backend._recognize.calls[-1][1] == {}
        monkeypatch.setattr(nvidia, "WORD_BOOSTING", True)
        assert backend.recognition_support == "model"
        backend.transcribe(path, recognition=recognition)
        assert backend._recognize.calls[-1][1] == {"phrases": PHRASES}

    def test_phrases_reach_the_worker_request_only_when_set(self, monkeypatch):
        backend = LocalSpeechBackend("nemotron")
        process = Mock()
        process.request.return_value = {"text": "", "segments": []}
        backend._process = process
        backend._recognize(np.zeros(160, dtype=np.float32), "en")
        assert "phrases" not in process.request.call_args.kwargs
        backend._recognize(np.zeros(160, dtype=np.float32), "en", phrases=PHRASES)
        assert process.request.call_args.kwargs["phrases"] == list(PHRASES)

    def test_a_host_drops_phrases_its_engine_does_not_boost(self, speech, monkeypatch):
        make, _path = speech
        backend = make("nemotron")
        backend.recognize(np.ones(160) * 0.1, "en", phrases=PHRASES)
        assert backend._recognize.calls[-1] == ("en", {})
        monkeypatch.setattr(nvidia, "WORD_BOOSTING", True)
        backend.recognize(np.ones(160) * 0.1, "en", phrases=PHRASES)
        assert backend._recognize.calls[-1] == ("en", {"phrases": PHRASES})


class FakeLib:
    """Stands in for the NeMo C library; reads the options as native code would."""

    def __init__(self):
        self.seen = []
        self.closed = []

    def __getattr__(self, name):
        raise AttributeError(name)

    def nemo_speech_asr_recognition_options_default(self):
        options = nvidia.Options()
        options.size = ctypes.sizeof(nvidia.Options)
        return options

    def _read(self, options_ref):
        gc.collect()
        options = options_ref._obj
        if not options.speech_contexts:
            self.seen.append(None)
            return
        context = ctypes.cast(options.speech_contexts, ctypes.POINTER(nvidia.SpeechContext)).contents
        self.seen.append((
            [context.phrases[i].decode("utf-8") for i in range(context.phrase_count)],
            round(context.boost, 2), options.speech_context_count, context.size,
        ))

    def nemo_speech_asr_recognize_f32(self, handle, options_ref, buf, count, rate, result_ref):
        self._read(options_ref)
        return 0

    def nemo_speech_asr_streaming_recognize(self, handle, options_ref, stream_ref):
        self._read(options_ref)
        stream_ref._obj.value = 7
        return 0

    def nemo_speech_asr_stream_next(self, handle, result_ref):
        return 0

    def nemo_speech_asr_stream_finish(self, handle):
        return 0

    def nemo_speech_asr_stream_close(self, handle):
        self.closed.append(handle)

    def nemo_speech_asr_result_transcript(self, result, index):
        return b"text"

    def nemo_speech_asr_result_word_count(self, result, index):
        return 0

    def nemo_speech_asr_result_is_final(self, result):
        return True

    def nemo_speech_asr_result_audio_processed(self, result):
        return 1.0

    def nemo_speech_asr_result_destroy(self, result):
        pass


class TestNvidiaSpeechContext:
    @pytest.fixture
    def recognizer(self):
        recognizer = nvidia.NvidiaRecognizer.__new__(nvidia.NvidiaRecognizer)
        recognizer.lib = FakeLib()
        recognizer.handle = ctypes.c_void_p(1)
        recognizer.streams = {}
        recognizer._stream_contexts = {}
        return recognizer

    def test_struct_matches_the_c_abi(self):
        fields = [name for name, _type in nvidia.SpeechContext._fields_]
        assert fields == ["size", "phrases", "phrase_count", "boost"]
        assert ctypes.sizeof(nvidia.SpeechContext) == 4 * ctypes.sizeof(ctypes.c_void_p)

    def test_phrases_stay_alive_through_the_native_call(self, recognizer):
        recognizer.transcribe(array.array("f", [0.0] * 160), "en", phrases=["Ksenia", "Kübernetes"])
        recognizer.transcribe(array.array("f", [0.0] * 160), "en")
        assert recognizer.lib.seen == [
            (["Ksenia", "Kübernetes"], nvidia.BOOST_SCORE, 1, ctypes.sizeof(nvidia.SpeechContext)),
            None,
        ]

    def test_options_point_at_the_keepalive(self):
        context, keepalive = nvidia.speech_context(["Ksenia", " ", 5, "OpenWhisper"])
        assert keepalive[0] is context and keepalive[2] == [b"Ksenia", b"OpenWhisper"]
        assert ctypes.cast(context.phrases, ctypes.c_void_p).value == ctypes.addressof(keepalive[1])
        assert nvidia.speech_context([]) == (None, None)
        assert nvidia.speech_context([f"w{i}" for i in range(80)])[0].phrase_count == nvidia.MAX_BOOST_PHRASES

    def test_a_stream_keeps_its_context_until_it_closes(self, recognizer):
        recognizer.stream("mic", array.array("f"), "en", phrases=["Ksenia"])
        assert "mic" in recognizer._stream_contexts
        recognizer.stream("mic", array.array("f"), "en", finish=True)
        assert "mic" not in recognizer._stream_contexts and recognizer.lib.seen[0][0] == ["Ksenia"]
        recognizer.stream("other", array.array("f"), "en", phrases=["Ksenia"])
        recognizer.cancel_stream("other")
        assert recognizer._stream_contexts == {}
        recognizer.stream("plain", array.array("f"), "en")
        assert recognizer._stream_contexts == {}


_NEMO_BOOTSTRAP = """
import runpy, sys, types
class Nvidia:
    def __init__(self, *a): pass
    def transcribe(self, audio, language, phrases=()):
        if 'Fail' in phrases:
            raise RuntimeError('cannot boost Fail')
        return {'text': ','.join(phrases) or 'plain'}
    def stream(self, session, audio, language, finish, phrases=()):
        return [{'text': ','.join(phrases) or 'plain'}]
sys.modules['services.local_asr.nvidia'] = types.SimpleNamespace(NvidiaRecognizer=Nvidia)
runpy.run_path('services/local_asr/worker.py', run_name='__main__')
"""


@pytest.mark.parametrize("family,expected", [("nemotron", "Ksenia,Kubernetes"), ("parakeet", "plain")])
def test_worker_passes_phrases_only_to_nemotron(family, expected):
    responses = run_worker(_NEMO_BOOTSTRAP, [
        dict(id=0, op="load", backend=family, device="cpu", runtime="fixture", model_path="fixture"),
        dict(id=1, op="transcribe", language="en", phrases=list(PHRASES)),
        dict(id=2, op="transcribe", language="en"),
        dict(id=3, op="transcribe", language="en", phrases=["Fail"]),
        dict(id=4, op="stream", language="en", session="mic", finish=True, phrases=list(PHRASES)),
        dict(id=5, op="shutdown"),
    ])
    assert responses[1]["result"]["text"] == expected
    assert responses[2]["result"]["text"] == "plain"
    assert responses[3]["result"]["text"] == "plain"
    assert responses[4]["result"]["events"] == [{"text": expected}]
