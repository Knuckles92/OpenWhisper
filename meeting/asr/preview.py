"""Best-effort live previews sharing a model with durable meeting transcription."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import logging
import queue
import threading
import time

import numpy as np

from meeting.asr.audio import prepare_for_whisper

logger = logging.getLogger(__name__)

#: A block further than this from its channel's previous one, in media or
#: wall-clock time, starts a new preview stream.
_MEDIA_GAP_S = .15
_WALL_GAP_S = .25

#: Longest single preview stream, in seconds of audio.
_MAX_STREAM_S = 30

#: 16 kHz samples buffered before a chunk is streamed (0.75 s).
_MIN_STREAM_SAMPLES = 12000

#: While durable decoding is busy, drop a stream that buffered over 5 s.
_BUSY_CANCEL_SAMPLES = 5 * 16000


class MeetingSpeechPreview:
    """Capture never waits for inference; dropped previews leave the WAV spool intact."""

    def __init__(self, backend, callback, busy, language=None):
        self.backend, self.callback, self.busy, self.language = backend, callback, busy, language
        self._queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="meeting-speech-preview")
        self._thread.start()

    def feed(self, block, start_s):
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait((replace(block, frames=block.frames.copy()), start_s))
        except queue.Full:
            # Gaps are detected by the timestamp check in the consumer.
            pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=35)

    def _run(self):
        sessions = {}
        try:
            while not self._stop.is_set():
                try:
                    block, start_s = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                self._handle_block(sessions, block, start_s)
        except Exception:
            logger.exception("Live speech preview stopped; durable chunk transcription continues")
        finally:
            for channel, state in sessions.items():
                try:
                    if state["pending"]:
                        native = np.concatenate(state["pending"])
                        samples = prepare_for_whisper(native, state["sample_rate"])
                    else:
                        samples = np.empty(0, np.float32)
                    self.backend.stream_audio(channel, samples, self.language, finish=True)
                except Exception:
                    logger.debug("Preview final flush failed", exc_info=True)

    def _handle_block(self, sessions, block, start_s):
        channel = block.channel
        rate = max(1, int(block.sample_rate))
        end_s = start_s + len(block.frames) / rate
        state = self._channel_session(sessions, block, start_s, end_s)
        state["pending"].append(block.frames)
        state["native_samples"] += len(block.frames)
        state["end"] = end_s
        state["wall_end"] = block.t_mono + len(block.frames) / rate
        est_16k = int(state["native_samples"] * 16000 / rate)
        if est_16k < _MIN_STREAM_SAMPLES:
            return
        if self.busy():
            # Do not let speculative text delay a durable decode.
            if est_16k > _BUSY_CANCEL_SAMPLES:
                self.backend.cancel_stream(channel)
                sessions.pop(channel)
            return
        native = np.concatenate(state["pending"])
        state["pending"].clear()
        state["native_samples"] = 0
        samples = prepare_for_whisper(native, rate)
        events = self.backend.stream_audio(channel, samples, self.language)
        state["sent"] = True
        if events and not self._stop.is_set():
            self._emit_events(channel, state, events, end_s)

    def _channel_session(self, sessions, block, start_s, end_s):
        """The channel's stream session, restarted after a gap or at the length cap."""
        channel = block.channel
        rate = max(1, int(block.sample_rate))
        state = sessions.get(channel)
        if state and (_session_broken(state, block, start_s, end_s)
                      or state["sample_rate"] != rate):
            if state.get("sent"):
                self.backend.stream_audio(channel, np.empty(0, np.float32), self.language, finish=True)
            sessions.pop(channel)
            state = None
        if state is None:
            state = dict(start=start_s, end=start_s, wall_end=block.t_mono,
                         sample_rate=rate, pending=deque(), native_samples=0)
            sessions[channel] = state
        return state

    def _emit_events(self, channel, state, events, end_s):
        for event in events:
            start = state["start"] + event.get("start", 0)
            end = state["start"] + event.get("end", end_s - state["start"])
            self.callback(dict(channel=channel, text=event["text"],
                               start_s=start,
                               end_s=min(end_s, end),
                               final=event.get("final", False)))


def _session_broken(state, block, start_s, end_s):
    """True when this block cannot continue the channel's current stream."""
    return (abs(start_s - state["end"]) > _MEDIA_GAP_S or
            abs(block.t_mono - state["wall_end"]) > _WALL_GAP_S or
            end_s - state["start"] > _MAX_STREAM_S)


class WindowSpeechPreview:
    """One-second Parakeet checks on at most eight seconds of recent audio.

    Capture only copies into a bounded queue. The loaded model is shared, with
    durable work taking priority. Slow decodes reduce preview frequency rather
    than accumulating speculative inference or changing the recording cadence.
    """

    INTERVAL_S = 1.0
    WINDOW_S = 8.0

    def __init__(self, backend, callback, busy, language=None):
        self.backend, self.callback, self.busy, self.language = backend, callback, busy, language
        self._queue = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._sessions = {}
        self._next_decode = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="meeting-window-preview")
        self._thread.start()

    def feed(self, block, start_s):
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait((replace(block, frames=block.frames.copy()), start_s))
        except queue.Full:
            # Drop only preview data. The spool already owns every audio block.
            pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _accept(self, block, start_s):
        rate = max(1, int(block.sample_rate))
        end_s = start_s + len(block.frames) / rate
        state = self._sessions.get(block.channel)
        if (state is None or state["sample_rate"] != rate
                or abs(start_s - state["end"]) > _MEDIA_GAP_S
                or abs(block.t_mono - state["wall_end"]) > _WALL_GAP_S):
            state = dict(blocks=deque(), native_samples=0, sample_rate=rate,
                         end=start_s, decoded=start_s)
            self._sessions[block.channel] = state
        state["blocks"].append(block.frames)
        state["native_samples"] += len(block.frames)
        max_native = int(round(self.WINDOW_S * rate))
        while (len(state["blocks"]) > 1
               and state["native_samples"] - len(state["blocks"][0]) >= max_native):
            dropped = state["blocks"].popleft()
            state["native_samples"] -= len(dropped)
        state["end"] = end_s
        state["wall_end"] = block.t_mono + len(block.frames) / rate

    def _decode(self):
        if self.busy() or time.monotonic() < self._next_decode:
            return
        # Oldest due channel first: continuous microphone speech cannot starve loopback.
        for channel, state in sorted(self._sessions.items(), key=lambda pair: pair[1]["decoded"]):
            if state["end"] - state["decoded"] < self.INTERVAL_S:
                continue
            state["decoded"] = state["end"]
            if not state["blocks"]:
                continue
            rate = state["sample_rate"]
            max_native = int(round(self.WINDOW_S * rate))
            native = (np.concatenate(state["blocks"])
                      if len(state["blocks"]) > 1 else state["blocks"][0])
            if len(native) > max_native:
                native = native[-max_native:]
            samples = prepare_for_whisper(native, rate)[-int(self.WINDOW_S * 16000):]
            if not samples.size or np.max(np.abs(samples)) <= .00025:
                continue
            began = time.monotonic()
            result = self.backend.preview_audio(samples, self.language, busy=self.busy)
            if result is None:
                return
            # At most 25% sustained model time for previews, across both channels.
            self._next_decode = time.monotonic() + max(.1, 3 * (time.monotonic() - began))
            if not self._stop.is_set():
                self.callback(dict(channel=channel, text=result.get("text", ""),
                                   start_s=state["end"] - len(samples) / 16000,
                                   end_s=state["end"], final=False))
            return

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self._accept(*self._queue.get(timeout=.1))
                except queue.Empty:
                    continue
                # Catch up with capture before inference, instead of replaying stale windows.
                for _ in range(128):
                    try:
                        self._accept(*self._queue.get_nowait())
                    except queue.Empty:
                        break
                self._decode()
        except Exception:
            logger.exception("Window speech preview stopped; durable recording continues")
