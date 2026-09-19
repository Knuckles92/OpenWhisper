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
                channel = block.channel
                state = sessions.get(channel)
                end_s = start_s + len(block.frames) / block.sample_rate
                if state and (abs(start_s-state["end"]) > .15 or
                              abs(block.t_mono-state["wall_end"]) > .25 or
                              end_s-state["start"] > 30):
                    if state.get("sent"):
                        self.backend.stream_audio(channel, np.empty(0, np.float32), self.language, finish=True)
                    sessions.pop(channel)
                    state = None
                if state is None:
                    state = dict(start=start_s, end=start_s, wall_end=block.t_mono,
                                 pending=deque(), samples=0)
                    sessions[channel] = state
                audio = prepare_for_whisper(block.frames, block.sample_rate)
                state["pending"].append(audio)
                state["samples"] += len(audio)
                state["end"] = end_s
                state["wall_end"] = block.t_mono + len(block.frames) / block.sample_rate
                if state["samples"] < 12000:
                    continue
                if self.busy():
                    # Do not let speculative text delay a durable decode.
                    if state["samples"] > 5*16000:
                        self.backend.cancel_stream(channel)
                        sessions.pop(channel)
                    continue
                samples = np.concatenate(state["pending"])
                state["pending"].clear()
                state["samples"] = 0
                events = self.backend.stream_audio(channel, samples, self.language)
                state["sent"] = True
                if events and not self._stop.is_set():
                    for event in events:
                        self.callback(dict(channel=channel, text=event["text"],
                                           start_s=state["start"] + event.get("start", 0),
                                           end_s=min(end_s, state["start"] + event.get("end", end_s - state["start"])),
                                           final=event.get("final", False)))
        except Exception:
            logger.exception("Live speech preview stopped; durable chunk transcription continues")
        finally:
            for channel, state in sessions.items():
                try:
                    samples = np.concatenate(state["pending"]) if state["pending"] else np.empty(0, np.float32)
                    self.backend.stream_audio(channel, samples, self.language, finish=True)
                except Exception:
                    logger.debug("Preview final flush failed", exc_info=True)



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
        end_s = start_s + len(block.frames) / block.sample_rate
        state = self._sessions.get(block.channel)
        if state is None or abs(start_s - state["end"]) > .15 or abs(block.t_mono - state["wall_end"]) > .25:
            state = dict(audio=np.empty(0, np.float32), end=start_s, decoded=start_s)
            self._sessions[block.channel] = state
        audio = prepare_for_whisper(block.frames, block.sample_rate)
        state["audio"] = np.concatenate((state["audio"], audio))[-int(self.WINDOW_S * 16000):]
        state["end"] = end_s
        state["wall_end"] = block.t_mono + len(block.frames) / block.sample_rate

    def _decode(self):
        if self.busy() or time.monotonic() < self._next_decode:
            return
        # Oldest due channel first: continuous microphone speech cannot starve loopback.
        for channel, state in sorted(self._sessions.items(), key=lambda pair: pair[1]["decoded"]):
            if state["end"] - state["decoded"] < self.INTERVAL_S:
                continue
            samples = state["audio"]
            state["decoded"] = state["end"]
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
