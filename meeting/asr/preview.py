"""Best-effort live previews sharing a model with durable meeting transcription."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import logging
import queue
import threading

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
                    latest = events[-1]
                    self.callback(dict(channel=channel, text=latest["text"],
                                       start_s=state["start"] + latest.get("start", 0),
                                       end_s=end_s, final=latest.get("final", False)))
        except Exception:
            logger.exception("Live speech preview stopped; durable chunk transcription continues")
        finally:
            for channel, state in sessions.items():
                try:
                    samples = np.concatenate(state["pending"]) if state["pending"] else np.empty(0, np.float32)
                    self.backend.stream_audio(channel, samples, self.language, finish=True)
                except Exception:
                    logger.debug("Preview final flush failed", exc_info=True)

