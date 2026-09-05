from types import SimpleNamespace

import numpy as np
import pytest

from benchmarks import live_preview as bench
from config import config


def test_callback_quantization_and_short_clip():
    assert bench.block_ends(7*44100, 44100, 3.) == [133120, 266240]
    assert bench.block_ends(44100, 44100, 3.) == []


def test_native_revisions_replace_and_finals_commit_once():
    transcript = bench.NativeTranscript()
    assert transcript.apply([dict(text='hello', final=False)]) == 'hello'
    assert transcript.apply([dict(text='hello world', final=False)]) == 'hello world'
    assert transcript.apply([dict(text='hello world.', final=True)]) == 'hello world.'
    assert transcript.apply([dict(text='next', final=False)]) == 'hello world. next'


def test_moonshine_line_snapshots_do_not_duplicate_completed_lines():
    transcript = bench.NativeTranscript()
    transcript.apply([dict(id='a', text='one', start=0., final=False)])
    events = [dict(id='a', text='one two', start=0., final=True),
              dict(id='b', text='three', start=1., final=False)]
    assert transcript.apply(events) == 'one two three'
    assert transcript.apply(events) == 'one two three'


def test_silence_counts_hallucinations_without_inventing_wer():
    result = bench.score('', 'Thank you')
    assert result['insertions'] == 2
    assert result['wer'] is None


def test_window_replay_includes_overlap_and_excludes_post_stop_text(monkeypatch):
    seen = []
    texts = iter(['one', 'two', 'three'])
    def transcribe(audio, **options):
        seen.append((len(audio), options))
        return iter([SimpleNamespace(text=next(texts))]), None
    backend = SimpleNamespace(model=SimpleNamespace(transcribe=transcribe))
    ticks = iter([0., .1, 1., 1.2, 2., 2.3])
    monkeypatch.setattr(bench.time, 'perf_counter', lambda: next(ticks))
    result = bench.replay(backend, np.zeros(7*44100, np.int16), 'one two three',
                          mode='window', cadence=3., overlap=.75)
    assert seen[0][0] == int(133120*16000/44100)
    assert seen[1][0] == int((133120+int(.75*44100))*16000/44100)
    assert all(options == dict(beam_size=1, vad_filter=False) for _, options in seen)
    assert result['live_text'] == 'one two'
    assert result['drained_text'] == 'one two three'
    assert result['live']['deletions'] == 1
    assert result['drained']['errors'] == 0
    assert result['drain_after_stop_s'] == pytest.approx(.3)


def test_slow_decode_models_backlog_and_visibility(monkeypatch):
    backend = SimpleNamespace(model=SimpleNamespace(transcribe=lambda a, **k:
                              (iter([SimpleNamespace(text='word')]), None)))
    ticks = iter([0., 4., 5., 9., 10., 14.])
    monkeypatch.setattr(bench.time, 'perf_counter', lambda: next(ticks))
    result = bench.replay(backend, np.zeros(7*44100, np.int16), 'word word word',
                          mode='window', cadence=3., overlap=.75)
    assert result['live_text'] == ''  # First completion is after audio EOF.
    assert result['calls'][1]['visible_s'] == pytest.approx(133120/44100+8)
    assert bench.aggregate([result], 133120/44100)['update_deadline_misses'] == 2


def test_native_short_clip_only_flushes_after_stop(monkeypatch):
    calls = []
    class Backend:
        def stream_audio(self, session, audio, language, finish=False):
            calls.append((len(audio), finish))
            return [dict(text='hello', final=True)] if finish else []
        def cancel_stream(self, session):
            calls.append('closed')
    ticks = iter([0., .1, 1., 1.2])
    monkeypatch.setattr(bench.time, 'perf_counter', lambda: next(ticks))
    result = bench.replay(Backend(), np.zeros(4410, np.int16), 'hello',
                          mode='native', cadence=.75, overlap=.75)
    assert calls == [(1600, False), (0, True), 'closed']
    assert result['live_text'] == ''
    assert result['drained_text'] == 'hello'
    assert result['first_text_before_stop_s'] is None


def test_swallowed_production_exception_fails_benchmark():
    def fail(audio, **options):
        raise RuntimeError('inference failed')
    backend = SimpleNamespace(model=SimpleNamespace(transcribe=fail))
    with pytest.raises(RuntimeError, match='Preview decode failed'):
        bench.replay(backend, np.zeros(4410, np.int16), 'hello',
                     mode='window', cadence=3., overlap=.75)


def test_aggregate_uses_word_weighted_errors():
    clips = []
    for ref, hyp in [('one', ''), ('one two three', 'one two three')]:
        clips.append(dict(duration_s=1., calls=[], first_text_before_stop_s=None,
                          live_text=hyp, live=bench.score(ref, hyp), drained=bench.score(ref, hyp)))
    assert bench.aggregate(clips, 3.)['live']['wer'] == .25


@pytest.mark.parametrize('options', [['--chunk', '0'], ['--chunk', 'nan'],
                                   ['--repeats', '0'], ['--overlap', '3']])
def test_rejects_invalid_benchmark_settings(options):
    with pytest.raises(SystemExit):
        bench.parse_args(['manifest.json', *options])
