"""Approximate previews during recording.

``StreamingTranscriber`` re-decodes overlapping windows. The backend only has
to expose ``model.transcribe(audio, ...)`` returning ``(segments, info)``:
faster-whisper's model and the optional engines' ``SpeechDecoder`` both do, so
the preview can share the dictation engine.

``NativeStreamingTranscriber`` has the same surface but follows an engine's
own streaming decoder through ``backend.stream_audio``, for engines whose
catalog entry advertises streaming (Nemotron today).
"""
import queue
import math
import re
import threading
import logging
import time
import numpy as np
from typing import Callable, List, Optional
from config import config

logger = logging.getLogger(__name__)

# Queued behind the last accepted block by stop and cancel, so a worker waiting
# on an empty queue wakes at once rather than at its next 0.1 s poll.
_WAKE = object()


def _wake_worker(audio_queue: queue.Queue) -> None:
    try:
        audio_queue.put_nowait(_WAKE)
    except queue.Full:
        # A full queue never blocks the worker's get, so it reaches its stop
        # check without the marker.
        pass


def fft_resample(samples: np.ndarray, num_samples: int) -> np.ndarray:
    """Resample mono audio without adding SciPy's 110 MB dependency."""
    n_in = len(samples)
    spectrum = np.fft.rfft(samples).copy()
    n_out_bins = num_samples // 2 + 1

    # The Nyquist bin needs care whenever the output length changes, because
    # irfft treats the final bin as its own conjugate twin (counted once) while
    # every other bin is counted twice. scipy.signal.resample makes the same
    # two adjustments.
    if num_samples < n_in:
        spectrum = spectrum[:n_out_bins]
        if num_samples % 2 == 0:
            # Truncation promoted an ordinary two-sided component into the
            # Nyquist slot; double it to preserve its energy.
            spectrum[-1] *= 2.0
    elif num_samples > n_in:
        if n_in % 2 == 0:
            # The input's real Nyquist component is about to become an ordinary
            # two-sided bin; halve it so the energy splits between +/- Nyquist.
            spectrum[-1] *= 0.5
        if n_out_bins > len(spectrum):
            spectrum = np.concatenate(
                [spectrum, np.zeros(n_out_bins - len(spectrum), dtype=spectrum.dtype)]
            )
        else:
            spectrum = spectrum[:n_out_bins]

    resampled = np.fft.irfft(spectrum, num_samples) * (num_samples / n_in)
    return resampled.astype(np.float32)


_PREVIEW_WORD = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)


def append_preview_text(existing: str, chunk_text: str, *, max_overlap_words: int = 0) -> str:
    """Join window text, removing only a bounded matching phrase at the seam.

    Single words and wholly repeated chunks are ambiguous without word times,
    so retain them. Only callers that actually prepend audio overlap opt in.
    """
    chunk_text = (chunk_text or "").strip()
    if not chunk_text:
        return existing or ""
    if not existing:
        return chunk_text
    if max_overlap_words >= 2:
        tail = list(_PREVIEW_WORD.finditer(existing[-512:]))
        head = list(_PREVIEW_WORD.finditer(chunk_text[:512]))
        normalize = lambda word: word.group().casefold().replace("’", "'")
        tail_words = [normalize(word) for word in tail]
        head_words = [normalize(word) for word in head]
        # Leave at least one newly recognized word; an entire repeated phrase
        # may be intentional speech rather than the overlap being decoded twice.
        maximum = min(max_overlap_words, len(tail_words), len(head_words) - 1)
        for count in range(maximum, 1, -1):
            if tail_words[-count:] == head_words[:count]:
                chunk_text = chunk_text[head[count].start():]
                break
    return f"{existing} {chunk_text}".strip()


def prepare_preview_audio(audio_array: np.ndarray, sample_rate: int) -> Optional[np.ndarray]:
    """Recorder PCM (int16 or float, mono or interleaved) to 16 kHz float32 mono."""
    if audio_array.dtype == np.int16:
        audio_array = audio_array.astype(np.float32) / 32768.0
    else:
        audio_array = audio_array.astype(np.float32)

    if len(audio_array.shape) > 1:
        audio_array = audio_array.mean(axis=1)

    if sample_rate != config.WHISPER_TARGET_SAMPLE_RATE:
        num_samples = int(len(audio_array) * config.WHISPER_TARGET_SAMPLE_RATE / sample_rate)
        if num_samples <= 0:
            return None
        audio_array = fft_resample(audio_array, num_samples)

    return audio_array


class NativePreviewLedger:
    """Assemble a native stream's events into one preview string.

    NeMo emits interim utterances that replace the current partial and final
    utterances that commit; Moonshine repeats and revises lines by stable id.
    Events are the worker's JSON dicts, never deltas.
    """

    def __init__(self):
        self.lines: dict = {}
        self.committed: List[str] = []
        self.partial: str = ""

    def apply(self, events) -> str:
        for event in events or ():
            text = (event.get("text") or "").strip()
            if "id" in event:
                self.lines[event["id"]] = dict(event, text=text)
            elif event.get("final"):
                if text:
                    self.committed.append(text)
                self.partial = ""
            else:
                self.partial = text
        return self.text

    @property
    def text(self) -> str:
        if self.lines:
            ordered = sorted(self.lines.values(), key=lambda e: e.get("start", 0.0))
            return " ".join(e["text"] for e in ordered if e["text"]).strip()
        return " ".join([*self.committed, self.partial]).strip()


class StreamingTranscriber:
    """Manages real-time streaming transcription using a worker thread.

    Stopping never decodes. The old stop drained the unfinished window first,
    and because Parakeet's preview shares the dictation engine that drain held
    the final decode back 88–100 ms on every dictation (September 2026 logs,
    RTX 2060), for text that is read only when the final transcript is empty.
    Stop now keeps that audio, and ``finalize_preview`` decodes it on demand.
    A paced replay of six saved dictations through the real Parakeet worker
    measured stop at 42–153 ms before (depending on where the worker's 0.1 s
    poll stood) and under 0.1 ms after, with the same finalized text.
    """

    # Queue poll while idle; stop and cancel wake the worker with _WAKE instead.
    _POLL_SEC = 0.1

    def __init__(
        self,
        backend,
        chunk_duration_sec: float = 3.0,
        overlap_sec: float = None,
    ):
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec
        self.overlap_sec = (
            overlap_sec
            if overlap_sec is not None
            else getattr(config, "STREAMING_OVERLAP_SEC", 0.75)
        )

        self.audio_queue: queue.Queue = queue.Queue(maxsize=config.STREAMING_QUEUE_SIZE)

        self.worker_thread: Optional[threading.Thread] = None
        self.is_streaming = False
        self._stop_requested = False
        self._discard_results = False
        self._state_lock = threading.RLock()

        self.preview_text: str = ""
        self._overlap_tail: Optional[np.ndarray] = None
        self._last_chunk_text = ""

        # What a stop left undecoded: the worker publishes its partial window
        # here as it exits, and blocks it never took stay in audio_queue.
        self._retained: List[np.ndarray] = []
        # From a stop until finalize_preview, the next start, cancel, or cleanup
        # takes that audio. _finalizing is True while finalize_preview decodes.
        self._finalize_pending = False
        self._finalizing = False

        self.sample_rate = 0
        self.callback: Optional[Callable[[str, bool], None]] = None

        self._chunk_count = 0
        self._slow_chunks = 0
        self._last_warning_time = 0

        logger.info(
            "StreamingTranscriber initialized "
            f"(chunk_duration={chunk_duration_sec}s, overlap={self.overlap_sec}s)"
        )

    def start_streaming(self, sample_rate: int, callback: Callable[[str, bool], None]):
        """Start previewing audio and report ``(text, is_final)`` to callback."""
        if (self.is_streaming or self._finalizing
                or (self.worker_thread and self.worker_thread.is_alive())):
            logger.warning("Streaming worker is still active")
            return

        # A decode can outlast ten recorder blocks (~0.23 s). Keep a bounded
        # audio-time buffer so a brief CPU/GPU stall does not drop speech.
        blocks = math.ceil(config.STREAMING_QUEUE_SEC * sample_rate / config.CHUNK_SIZE)
        # The fresh queue also drops blocks the last recording's stop retained.
        self.audio_queue = queue.Queue(maxsize=max(config.STREAMING_QUEUE_SIZE, blocks))
        self.sample_rate = sample_rate
        self.callback = callback
        self.is_streaming = True
        self._stop_requested = False
        self._discard_results = False
        self.preview_text = ""
        self._last_chunk_text = ""
        self._overlap_tail = None
        self._retained = []
        self._finalize_pending = False
        self._chunk_count = 0
        self._slow_chunks = 0

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        logger.info("Streaming transcription started")

    def feed_audio(self, audio_chunk: np.ndarray):
        """Queue an audio chunk without blocking the recorder callback."""
        with self._state_lock:
            if not self.is_streaming or self._stop_requested:
                return
            try:
                self.audio_queue.put_nowait(audio_chunk.copy())
            except queue.Full:
                logger.debug("Audio queue full, dropping preview chunk")

    def stop_streaming(self) -> str:
        """Stop without waiting or decoding, and return the text so far.

        The worker wakes and exits, leaving its partial window, blocks still
        queued, and the overlap tail for ``finalize_preview``. A window decode
        already in flight finishes in the background: its text still lands
        for the fallback, but no callback fires after stop.
        """
        with self._state_lock:
            if not self.is_streaming:
                return ""
            self._stop_requested = True
            self.is_streaming = False
            self._finalize_pending = True
            text = self.preview_text.strip()
        _wake_worker(self.audio_queue)
        return text

    def finalize_preview(self, timeout: float = 5.0) -> str:
        """Decode what stop left behind and return the complete preview text.

        Blocking, so call it from a worker thread, never Qt, and only when the
        text is needed. Waits up to ``timeout`` for the stopped worker to exit,
        then decodes the retained audio with the window boundaries the old stop
        drain used, so the text is the same. Past the deadline the late output
        is ignored and the text so far is returned, as that drain did.
        """
        with self._state_lock:
            if self._discard_results:
                return ""
            if not self._finalize_pending:
                return self.preview_text.strip()
            worker = self.worker_thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        with self._state_lock:
            if self._discard_results or not self._finalize_pending:
                # Cancel, cleanup, or a new recording took the audio meanwhile.
                return ""
            self._finalize_pending = False
            if worker and worker.is_alive():
                self._discard_results = True
                self._take_retained()
                logger.warning("Preview worker did not finish in time; ignoring late output")
                return self.preview_text.strip()
            blocks = self._take_retained()
            self._finalizing = True
        started = time.perf_counter()
        try:
            window: List[np.ndarray] = []
            duration = 0.0
            for block in blocks:
                window.append(block)
                duration += len(block) / self.sample_rate
                if duration >= self.chunk_duration_sec:
                    self._process_incremental_chunk(window)
                    window, duration = [], 0.0
            if window:
                self._process_incremental_chunk(window)
        finally:
            with self._state_lock:
                self._finalizing = False
        with self._state_lock:
            text = "" if self._discard_results else self.preview_text.strip()
        logger.info(
            f"Preview tail finalized in {(time.perf_counter() - started) * 1000:.0f} ms "
            f"({len(blocks)} blocks, {len(text)} chars)"
        )
        return text

    def cancel_streaming(self) -> None:
        """Discard preview work and retained audio without waiting for a decode."""
        with self._state_lock:
            self._discard_results = True
            self._stop_requested = True
            self.is_streaming = False
            self._finalize_pending = False
            self._take_retained()
            worker = self.worker_thread
        if worker and worker.is_alive():
            _wake_worker(self.audio_queue)

    def _take_retained(self) -> List[np.ndarray]:
        """Remove and return the stopped worker's partial window plus queued blocks."""
        blocks, self._retained = self._retained, []
        while True:
            try:
                block = self.audio_queue.get_nowait()
            except queue.Empty:
                return blocks
            if block is not _WAKE:
                blocks.append(block)

    def _worker_loop(self):
        accumulated_audio: List[np.ndarray] = []
        accumulated_duration = 0.0
        try:
            while not (self._discard_results or self._stop_requested):
                try:
                    audio_chunk = self.audio_queue.get(timeout=self._POLL_SEC)
                except queue.Empty:
                    continue
                if audio_chunk is _WAKE:
                    break
                accumulated_audio.append(audio_chunk)
                accumulated_duration += len(audio_chunk) / self.sample_rate
                # A window completed by a block taken just after stop is left
                # for finalize_preview rather than started here.
                if accumulated_duration >= self.chunk_duration_sec and not self._stop_requested:
                    self._process_incremental_chunk(accumulated_audio)
                    accumulated_audio = []
                    accumulated_duration = 0.0
        except Exception:
            logger.exception("Error in streaming worker loop")
        finally:
            with self._state_lock:
                if not self._discard_results:
                    self._retained = accumulated_audio
            logger.info("Streaming worker thread exiting")

    def _process_incremental_chunk(self, new_chunks: List[np.ndarray]):
        if not new_chunks or self._discard_results:
            return

        try:
            start_time = time.time()

            new_audio = np.concatenate(new_chunks)
            if self._overlap_tail is not None and len(self._overlap_tail) > 0:
                audio_array = np.concatenate([self._overlap_tail, new_audio])
            else:
                audio_array = new_audio

            total_duration = len(audio_array) / self.sample_rate
            new_duration = len(new_audio) / self.sample_rate

            model = getattr(self.backend, "model", None)
            if model is None:
                logger.debug("Preview window skipped: the engine is not loaded")
                return

            prepared = self._prepare_audio_for_whisper(audio_array)
            if prepared is None or len(prepared) == 0:
                return

            segments, _info = model.transcribe(
                prepared,
                beam_size=1,
                vad_filter=False,
            )

            text_parts = []
            for segment in segments:
                if self._discard_results:
                    return
                text_parts.append(segment.text)

            chunk_text = " ".join(text_parts).strip()
            overlap_words = min(12, math.ceil(self.overlap_sec * 8)) if (
                self._overlap_tail is not None and self._last_chunk_text
            ) else 0
            with self._state_lock:
                if self._discard_results:
                    return
                self.preview_text = append_preview_text(
                    self.preview_text, chunk_text, max_overlap_words=overlap_words)
                self._last_chunk_text = chunk_text

            overlap_samples = int(self.overlap_sec * self.sample_rate)
            if overlap_samples > 0 and len(new_audio) > 0:
                self._overlap_tail = new_audio[-overlap_samples:].copy()
            else:
                self._overlap_tail = None

            processing_time = time.time() - start_time
            self._chunk_count += 1

            logger.info(
                f"Incremental transcription #{self._chunk_count}: "
                f"{new_duration:.1f}s new (+{total_duration - new_duration:.1f}s overlap) "
                f"-> {processing_time:.2f}s processing ({len(chunk_text)} chars)"
            )

            if processing_time > 5.0:
                self._slow_chunks += 1
                if self._slow_chunks >= 3 and time.time() - self._last_warning_time > 30:
                    logger.warning("Incremental transcription falling behind (3+ slow chunks)")
                    self._last_warning_time = time.time()

            with self._state_lock:
                # After stop the text is kept for finalize_preview only; the
                # preview overlay is already gone.
                if (not self._discard_results and not self._stop_requested
                        and self.callback and self.preview_text):
                    # is_final=True means replace the full preview in the UI.
                    self.callback(self.preview_text, True)

        except Exception as e:
            logger.error(f"Error in incremental transcription: {e}", exc_info=True)

    def _prepare_audio_for_whisper(self, audio_array: np.ndarray) -> Optional[np.ndarray]:
        return prepare_preview_audio(audio_array, self.sample_rate)

    def cleanup(self):
        """Stop streaming and drop queued and retained audio without waiting."""
        self.cancel_streaming()
        logger.info("StreamingTranscriber cleaned up")


class NativeStreamingTranscriber:
    """Preview through an engine's own streaming decoder.

    Same surface as ``StreamingTranscriber`` so ``StreamingRuntime`` can hold
    either. The engine keeps decoder state across pushes, so new audio goes
    out every ``update_interval_sec`` with no overlap, and quiet audio is never
    skipped: the stream's endpointing is what turns a partial into a final.
    One worker session named ``SESSION`` is opened by the first push and
    closed by the ``finish`` push on stop, so the final utterance flushes.
    """

    SESSION = "dictation-preview"
    _POLL_SEC = StreamingTranscriber._POLL_SEC

    def __init__(self, backend, update_interval_sec: Optional[float] = None):
        self.backend = backend
        self.update_interval_sec = (
            update_interval_sec
            if update_interval_sec is not None
            else config.STREAMING_NATIVE_UPDATE_SEC
        )

        # Recorder blocks are CHUNK_SIZE frames; hold STREAMING_NATIVE_QUEUE_SEC
        # of them so a slow push never costs the stream audio.
        blocks = config.STREAMING_NATIVE_QUEUE_SEC * config.SAMPLE_RATE / config.CHUNK_SIZE
        self.audio_queue: queue.Queue = queue.Queue(maxsize=max(1, int(blocks)))

        self.worker_thread: Optional[threading.Thread] = None
        self.is_streaming = False
        self._stop_requested = False
        self._discard_results = False
        self._state_lock = threading.RLock()

        self.preview_text: str = ""
        self._ledger = NativePreviewLedger()

        self.sample_rate = 0
        self.callback: Optional[Callable[[str, bool], None]] = None

        self._update_count = 0
        # True while the worker holds a stream handle for SESSION.
        self._session_open = False
        # Set by the first failed push; later pushes are skipped so one broken
        # engine does not log once per update for the rest of the recording.
        self._failed = False

        logger.info(
            "NativeStreamingTranscriber initialized "
            f"(update_interval={self.update_interval_sec}s)"
        )

    def start_streaming(self, sample_rate: int, callback: Callable[[str, bool], None]):
        """Start following the engine stream and report ``(text, True)`` on each change."""
        if self.is_streaming or (self.worker_thread and self.worker_thread.is_alive()):
            logger.warning("Native streaming worker is still active")
            return

        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        self.sample_rate = sample_rate
        self.callback = callback
        self.is_streaming = True
        self._stop_requested = False
        self._discard_results = False
        self.preview_text = ""
        self._ledger = NativePreviewLedger()
        self._update_count = 0
        self._failed = False

        if self._session_open:
            # The previous session did not close; drop its handle before the
            # engine would otherwise append this recording to it.
            self._cancel_session()

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        logger.info("Native streaming preview started")

    def feed_audio(self, audio_chunk: np.ndarray):
        """Queue an audio chunk without blocking the recorder callback."""
        with self._state_lock:
            if not self.is_streaming or self._stop_requested:
                return
            try:
                self.audio_queue.put_nowait(audio_chunk.copy())
            except queue.Full:
                logger.debug("Audio queue full, dropping native preview chunk")

    def stop_streaming(self) -> str:
        """Finish accepted audio off the UI thread, bounded to five seconds."""
        with self._state_lock:
            if not self.is_streaming:
                return ""
            self._stop_requested = True
            self.is_streaming = False
        # Unlike the window preview this still waits: the finish push is what
        # flushes the engine's last utterance, and deferring it would hold the
        # engine session open across the final decode.
        _wake_worker(self.audio_queue)
        worker = self.worker_thread
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=5.0)
        with self._state_lock:
            if worker and worker.is_alive():
                self._discard_results = True
                logger.warning("Native preview worker did not finish in time; ignoring late output")
            else:
                self.worker_thread = None
            return self.preview_text.strip()

    def cancel_streaming(self) -> None:
        """Let the worker close its stream without publishing canceled output."""
        with self._state_lock:
            self._discard_results = True
            self._stop_requested = True
            self.is_streaming = False
            worker = self.worker_thread
        if worker and worker.is_alive():
            _wake_worker(self.audio_queue)

    def _worker_loop(self):
        logger.info("Native preview worker thread started")

        pending: List[np.ndarray] = []
        pending_samples = 0
        threshold = int(self.update_interval_sec * self.sample_rate)

        try:
            while not self._discard_results:
                try:
                    audio_chunk = self.audio_queue.get(timeout=self._POLL_SEC)
                    if audio_chunk is _WAKE:
                        # Stop queues this behind every accepted block, so the
                        # finish push below still carries all of them.
                        break
                    pending.append(audio_chunk)
                    pending_samples += len(audio_chunk)
                    if pending_samples >= threshold:
                        self._push(pending, finish=False)
                        pending = []
                        pending_samples = 0
                except queue.Empty:
                    if self._stop_requested and self.audio_queue.empty():
                        break
        except Exception as e:
            logger.error(f"Error in native preview worker loop: {e}", exc_info=True)
        finally:
            # Remaining audio plus the finish that emits the last utterance.
            if self._discard_results:
                if self._session_open:
                    self._cancel_session()
            else:
                self._push(pending, finish=True)
            logger.info("Native preview worker thread exiting")

    def _push(self, chunks: List[np.ndarray], *, finish: bool):
        if self._failed:
            if finish and self._session_open:
                self._cancel_session()
            return
        if not chunks and not (finish and self._session_open):
            # Nothing new, and no engine stream to close.
            return

        try:
            start_time = time.time()
            if chunks:
                prepared = prepare_preview_audio(np.concatenate(chunks), self.sample_rate)
            else:
                prepared = None
            if prepared is None:
                prepared = np.empty(0, dtype=np.float32)

            # "auto" matches the window preview's language handling.
            events = self.backend.stream_audio(
                self.SESSION, prepared, "auto", finish=finish
            )
            self._session_open = not finish
            self._update_count += 1

            with self._state_lock:
                if self._discard_results:
                    return
                text = self._ledger.apply(events)
                if text != self.preview_text:
                    self.preview_text = text
                    if self.callback and text:
                        self.callback(text, True)
            logger.debug("Native preview update #%d took %.3fs%s",
                         self._update_count, time.time() - start_time,
                         " (finish)" if finish else "")
        except Exception as e:
            self._failed = True
            logger.error(
                "Native preview stream failed; the final transcription is unaffected: %s",
                e,
                exc_info=True,
            )
            self._cancel_session()

    def _cancel_session(self):
        try:
            self.backend.cancel_stream(self.SESSION)
        except Exception as e:
            logger.debug(f"Native preview session cancel failed: {e}")
        self._session_open = False

    def cleanup(self):
        """Clean up resources and stop streaming."""
        if self.is_streaming:
            self.stop_streaming()

        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        logger.info("NativeStreamingTranscriber cleaned up")
