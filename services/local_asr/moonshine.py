"""Native Moonshine sessions; only the isolated worker imports its SDK."""
from __future__ import annotations


class MoonshineRecognizer:
    def __init__(self, model_path, model):
        from moonshine_voice import Transcriber, ModelArch
        arch = ModelArch.SMALL_STREAMING if model == "moonshine-small" else ModelArch.MEDIUM_STREAMING
        self.engine = Transcriber(model_path, arch)
        self.streams = {}

    def stream(self, key, samples, language=None, finish=False):
        if language and language not in ("auto", "en", "en-US", "English"):
            raise ValueError("This Moonshine model supports English only.")
        if key not in self.streams:
            # The caller controls cadence and explicitly asks for each update.
            stream = self.engine.create_stream(update_interval=3600)
            stream.start()
            self.streams[key] = stream
        stream = self.streams[key]
        if samples:
            stream.add_audio(samples, 16000)
        transcript = stream.stop() if finish else stream.update_transcription()
        if transcript is None:
            raise RuntimeError("Moonshine could not finalize this stream")
        events = [
            dict(id=str(line.line_id), text=line.text, start=line.start_time,
                 end=line.start_time + line.duration, final=bool(line.is_complete or finish))
            for line in transcript.lines
        ]
        if finish:
            self.cancel_stream(key)
        return events

    def transcribe(self, samples, language=None):
        key = "file"
        try:
            events = self.stream(key, samples, language, finish=True)
            segments = [dict(text=e["text"], start=e["start"], end=e["end"])
                        for e in events if e["text"].strip()]
            return dict(text=" ".join(s["text"] for s in segments), segments=segments)
        finally:
            self.cancel_stream(key)

    def cancel_stream(self, key):
        stream = self.streams.pop(key, None)
        if stream is not None:
            stream.close()

    def close(self):
        for key in list(self.streams):
            self.cancel_stream(key)
        self.engine.close()

