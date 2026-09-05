#!/usr/bin/env python3
"""
Transcription model speed benchmark.

Tests all transcription models against known-length audio files (10s, 30s, 2min)
and measures transcription time to compare performance.

Usage:
    From project root:
        python benchmarks/model_benchmark.py

    Or from benchmarks folder:
        python model_benchmark.py
"""

import os
import sys
import time
import logging
import warnings
import subprocess
import platform
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")


def _patch_subprocess_for_windows():
    """Patch subprocess.Popen to hide console windows on Windows.

    This prevents the console flash when running with pythonw.exe,
    especially when whisper calls ffmpeg internally via subprocess.
    """
    if platform.system() != "Windows":
        return

    _original_popen = subprocess.Popen

    class _NoConsolePopen(_original_popen):
        def __init__(self, *args, **kwargs):
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW):
                kwargs['creationflags'] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoConsolePopen


if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from services.settings import settings_manager

from transcriber.local_backend import LocalWhisperBackend
from transcriber.openai_backend import OpenAIBackend
from config import config

logging.basicConfig(
    level=logging.CRITICAL,  # Only show critical errors
    format='%(levelname)s - %(message)s',
    handlers=[logging.NullHandler()]  # Suppress all logging output
)
logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    model_name: str
    audio_duration: float
    transcription_time: float
    success: bool
    error: Optional[str] = None
    transcribed_text_length: int = 0
    accuracy_percentage: float = 0.0
    transcribed_text: str = ""
    expected_text: str = ""


def calculate_word_accuracy(expected: str, transcribed: str) -> float:
    """
    Calculate word-level accuracy between expected and transcribed text.

    Uses a simple word matching approach:
    - Normalizes both texts (lowercase, remove punctuation)
    - Counts matching words
    - Returns percentage of expected words that were correctly transcribed

    Args:
        expected: The reference text that was spoken
        transcribed: The text output from transcription

    Returns:
        Accuracy percentage (0.0 to 100.0)
    """
    import re

    def normalize_text(text: str) -> List[str]:
        text = text.lower()
        text = re.sub(r'[^\w\s]', '', text)
        words = [w.strip() for w in text.split() if w.strip()]
        return words

    expected_words = normalize_text(expected)
    transcribed_words = normalize_text(transcribed)

    if not expected_words:
        return 100.0 if not transcribed_words else 0.0

    matches = 0
    transcribed_set = set(transcribed_words)
    expected_counts = {}
    transcribed_counts = {}

    for word in expected_words:
        expected_counts[word] = expected_counts.get(word, 0) + 1

    for word in transcribed_words:
        transcribed_counts[word] = transcribed_counts.get(word, 0) + 1

    for word, count in expected_counts.items():
        transcribed_count = transcribed_counts.get(word, 0)
        matches += min(count, transcribed_count)

    accuracy = (matches / len(expected_words)) * 100.0
    return min(accuracy, 100.0)  # Cap at 100%


class AudioGenerator:
    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = script_dir
        self._tts_available = None
        logger.info(f"Using audio directory: {self.temp_dir}")

    def _check_tts_available(self) -> bool:
        if self._tts_available is None:
            try:
                from gtts import gTTS
                from pydub import AudioSegment
                self._tts_available = True
            except ImportError:
                self._tts_available = False
        return self._tts_available

    def _find_ami_audio(self) -> Optional[str]:
        ami_dir = os.path.join(project_root, "benchmarks", "meeting_mode", "data", "ami", "audio")
        if os.path.isdir(ami_dir):
            for fname in ["IN1009.Mix-Headset.wav", "IN1001.Mix-Headset.wav"]:
                candidate = os.path.join(ami_dir, fname)
                if os.path.exists(candidate):
                    return candidate
        return None

    def _check_audio_available(self) -> bool:
        return self._check_tts_available() or (self._find_ami_audio() is not None)

    def generate_tts_audio(self, duration_seconds: float, output_filename: str) -> Optional[Tuple[str, str]]:
        """
        Generate speech audio using gTTS or slice from the local speech corpus.

        Args:
            duration_seconds: Target duration in seconds
            output_filename: Name for the output file

        Returns:
            Tuple of (path to generated audio file, expected text description), or None if failed
        """
        output_path = os.path.join(self.temp_dir, output_filename)

        if self._check_tts_available():
            try:
                from gtts import gTTS  # type: ignore
                from pydub import AudioSegment  # type: ignore

                base_text = (
                    "This is a test audio file for benchmarking transcription models. "
                    "We are measuring how long it takes each model to transcribe audio of different lengths. "
                    "The transcription system needs to accurately convert speech to text while maintaining "
                    "good performance. This test will help us understand which model works best for different "
                    "use cases and audio durations. The audio contains natural speech patterns and common words "
                    "that transcription systems should be able to handle effectively."
                )

                repetitions = max(1, int(duration_seconds / 20))
                full_text = (base_text + " ") * repetitions

                print(f"  Text to be spoken: {len(full_text.split())} words (~{duration_seconds}s)")
                print(f"  Generating {duration_seconds}s audio with gTTS...")
                tts = gTTS(text=full_text, lang='en', slow=False)

                temp_mp3 = os.path.join(self.temp_dir, "temp.mp3")
                tts.save(temp_mp3)

                audio = AudioSegment.from_mp3(temp_mp3)
                target_ms = int(duration_seconds * 1000)
                if len(audio) < target_ms:
                    silence = AudioSegment.silent(duration=target_ms - len(audio))
                    audio = audio + silence
                else:
                    audio = audio[:target_ms]

                audio.export(output_path, format="wav")

                try:
                    os.remove(temp_mp3)
                except Exception:
                    pass

                actual_duration = len(audio) / 1000.0
                print(f"  ✅ Generated TTS audio: {output_path} ({actual_duration:.1f}s)")
                return (output_path, full_text.strip())
            except Exception as e:
                logger.debug(f"gTTS generation failed, attempting corpus slice fallback: {e}")

        ami_audio = self._find_ami_audio()
        if ami_audio:
            try:
                import wave
                print(f"  Extracting {duration_seconds}s speech slice from {os.path.basename(ami_audio)}...")
                with wave.open(ami_audio, "rb") as src:
                    rate = src.getframerate()
                    channels = src.getnchannels()
                    sampwidth = src.getsampwidth()
                    total_frames = src.getnframes()
                    # Start at 30 seconds into the recording to skip intro silence
                    start_frame = min(total_frames - 1, int(30.0 * rate))
                    src.setpos(start_frame)
                    frames_to_read = min(total_frames - start_frame, int(duration_seconds * rate))
                    frames = src.readframes(frames_to_read)

                with wave.open(output_path, "wb") as dst:
                    dst.setnchannels(channels)
                    dst.setsampwidth(sampwidth)
                    dst.setframerate(rate)
                    dst.writeframes(frames)

                actual_duration = len(frames) / (channels * sampwidth * rate)
                print(f"  ✅ Extracted speech sample: {output_path} ({actual_duration:.1f}s)")
                return (output_path, f"AMI speech slice ({duration_seconds}s)")
            except Exception as e:
                logger.error(f"Failed to slice corpus audio: {e}")

        return None


    def cleanup(self):
        try:
            for filename in os.listdir(self.temp_dir):
                if filename.startswith("test_") and filename.endswith(".wav"):
                    file_path = os.path.join(self.temp_dir, filename)
                    try:
                        os.remove(file_path)
                        logger.info(f"Removed test audio file: {filename}")
                    except Exception as e:
                        logger.warning(f"Failed to remove {filename}: {e}")
        except Exception as e:
            logger.warning(f"Failed to cleanup audio files: {e}")


class ModelBenchmark:
    def __init__(
        self,
        local_models: Optional[List[str]] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        skip_api: bool = False,
    ):
        self.audio_generator = AudioGenerator()
        self.results: List[TestResult] = []
        self.device = None if device == "auto" else device
        self.compute_type = None if compute_type == "auto" else compute_type

        if local_models is not None:
            models = list(local_models)
        else:
            models = ['base', 'base.en', 'tiny', 'tiny.en', 'turbo']

        # Ensure 'turbo' is tested last if present to prevent destructor issues
        if 'turbo' in models:
            models.remove('turbo')
            models.append('turbo')
        self.local_models_to_test = models

        self.backends: Dict[str, any] = {}

        if not skip_api:
            print("Initializing OpenAI backends...")
            for backend_name in config.API_MODEL_CHOICES:
                try:
                    backend = OpenAIBackend(backend_name)
                    if backend.is_available():
                        self.backends[backend_name] = backend
                        print(f"✅ {backend_name} backend initialized")
                    else:
                        print(f"⚠️  {backend_name} backend not available (missing API key?)")
                except Exception as e:
                    error_msg = str(e)
                    if len(error_msg) > 150:
                        error_msg = error_msg[:147] + "..."
                    print(f"⚠️  Failed to initialize {backend_name}: {error_msg}")

    def test_model(self, backend_name: str, backend: any, audio_file: str, duration: float) -> TestResult:
        print(f"\n  Testing {backend_name}...")

        try:
            if not backend.is_available():
                return TestResult(
                    model_name=backend_name,
                    audio_duration=duration,
                    transcription_time=0.0,
                    success=False,
                    error="Backend not available"
                )
        except Exception as e:
            return TestResult(
                model_name=backend_name,
                audio_duration=duration,
                transcription_time=0.0,
                success=False,
                error=f"Backend check failed: {str(e)}"
            )

        try:
            start_time = time.time()
            transcribed_text = backend.transcribe(audio_file)
            end_time = time.time()

            transcription_time = end_time - start_time

            return TestResult(
                model_name=backend_name,
                audio_duration=duration,
                transcription_time=transcription_time,
                success=True,
                transcribed_text_length=len(transcribed_text)
            )

        except KeyboardInterrupt:
            raise
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 100:
                error_msg = error_msg[:97] + "..."
            logger.error(f"Transcription failed for {backend_name}: {error_msg}")
            return TestResult(
                model_name=backend_name,
                audio_duration=duration,
                transcription_time=0.0,
                success=False,
                error=error_msg
            )

    def _print_local_whisper_config(self, backend):
        print("\n" + "=" * 80)
        print("Local Whisper Configuration")
        print("=" * 80)

        print(f"\n  Model:        {backend.model_name}")
        print(f"  Device:       {backend._device}")
        print(f"  Compute Type: {backend._compute_type}")

        if backend._device == "cuda":
            try:
                import torch
                if torch.cuda.is_available():
                    gpu_name = torch.cuda.get_device_name(0)
                    gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                    print(f"  GPU:          {gpu_name}")
                    print(f"  GPU Memory:   {gpu_memory:.1f} GB")

                    cuda_version = torch.version.cuda
                    print(f"  CUDA Version: {cuda_version}")
            except Exception as e:
                print(f"  GPU Info:     (could not retrieve: {e})")

        try:
            print(f"\n  VAD Enabled:  {config.FASTER_WHISPER_VAD_ENABLED}")
            print(f"  Beam Size:    {config.FASTER_WHISPER_BEAM_SIZE}")
            if config.FASTER_WHISPER_VAD_ENABLED:
                print(f"  VAD Min Silence: {config.FASTER_WHISPER_VAD_MIN_SILENCE_MS}ms")
        except AttributeError:
            pass  # Config attributes not available

        print("")

    def run_benchmark(self, durations: List[float] = [10.0, 30.0, 120.0]):
        print("=" * 80)
        print("Model Benchmark Test")
        print("=" * 80)
        total_models = len(self.local_models_to_test) + len(self.backends)
        print(f"\nTesting {total_models} models with {len(durations)} audio durations")
        print(f"Local models: {', '.join([f'local_whisper_{m}' for m in self.local_models_to_test])}")
        print(f"API models: {', '.join(self.backends.keys())}")
        print(f"Durations: {', '.join([f'{d}s' for d in durations])}")
        print("\n" + "=" * 80)

        print("\n📁 Generating test audio files...")
        audio_files = {}

        if not self.audio_generator._check_audio_available():
            print("\n❌ ERROR: No speech audio generator available")
            print("   Please either install gTTS (pip install gtts pydub) or ensure")
            print("   AMI benchmark audio exists under benchmarks/meeting_mode/data/ami/audio/")
            return

        for duration in durations:
            filename = f"test_{int(duration)}s.wav"
            audio_result = self.audio_generator.generate_tts_audio(duration, filename)

            if audio_result:
                audio_files[duration] = audio_result[0]
            else:
                print(f"❌ Failed to generate {duration}s audio file")
                return

        if not audio_files:
            print("❌ Failed to generate any test audio files. Exiting.")
            return

        print("\n" + "=" * 80)
        print("Running benchmark tests for local models...")
        print("=" * 80)
        print("\n⚠️  Note: Local models will be loaded one at a time to avoid memory issues")
        print("   Each model will be tested with all durations, then unloaded before the next.\n")

        config_printed = False

        for model_idx, model_name in enumerate(self.local_models_to_test):
            is_last_model = (model_idx == len(self.local_models_to_test) - 1)
            backend_key = f'local_whisper_{model_name}'
            backend = None

            try:
                print(f"\n{'='*80}")
                print(f"Loading {backend_key}...")
                print('='*80)
                backend = LocalWhisperBackend(
                    model_name=model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                )

                if not backend.is_available():
                    print(f"⚠️  {backend_key} backend not available - skipping")
                    continue

                print(f"\n  Model Configuration:")
                print(f"    Model Name:    {backend.model_name}")
                print(f"    Device:        {backend._device}")
                print(f"    Compute Type:  {backend._compute_type}")

                if not config_printed:
                    print(f"\n  Hardware Configuration:")
                    self._print_local_whisper_config(backend)
                    config_printed = True
                else:
                    print()  # Just add a blank line for subsequent models

                print(f"✅ {backend_key} loaded successfully")

                for duration, audio_file in audio_files.items():
                    print(f"\n🎵 Testing {backend_key} with {duration}s audio file...")
                    try:
                        result = self.test_model(backend_key, backend, audio_file, duration)
                        self.results.append(result)

                        if result.success:
                            print(f"  ✅ {backend_key}: {result.transcription_time:.2f}s")
                        else:
                            print(f"  ❌ {backend_key}: {result.error}")
                    except KeyboardInterrupt:
                        print("\n⚠️  Benchmark interrupted by user")
                        if backend:
                            backend.cleanup()
                        raise
                    except Exception as e:
                        error_msg = str(e)
                        if len(error_msg) > 100:
                            error_msg = error_msg[:97] + "..."
                        print(f"  ❌ {backend_key}: Unexpected error - {error_msg}")
                        self.results.append(TestResult(
                            model_name=backend_key,
                            audio_duration=duration,
                            transcription_time=0.0,
                            success=False,
                            error=f"Unexpected error: {error_msg}"
                        ))

                # Skip cleanup for turbo model - ctranslate2 destructor crashes with large models
                if model_name == 'turbo':
                    print(f"\n⏭️  Skipping cleanup for {backend_key} (turbo model - known destructor issue)")
                    # Keep reference alive to prevent GC from destroying it (causes segfault)
                    self._turbo_backend_ref = backend
                    backend = None
                elif is_last_model:
                    print(f"\n⏭️  Skipping cleanup for {backend_key} (last local model)")
                    backend = None
                else:
                    print(f"\n🧹 Unloading {backend_key}...")
                    sys.stdout.flush()
                    backend.cleanup()
                    backend = None
                    time.sleep(0.5)
                    print(f"✅ {backend_key} unloaded")
                    sys.stdout.flush()

            except KeyboardInterrupt:
                print("\n⚠️  Benchmark interrupted by user")
                if backend:
                    backend.cleanup()
                raise
            except Exception as e:
                error_msg = str(e)
                if len(error_msg) > 150:
                    error_msg = error_msg[:147] + "..."
                print(f"⚠️  Failed to initialize {backend_key}: {error_msg}")
                print("   (This may be due to CUDA/cuDNN issues - will skip this model)")
                if backend:
                    try:
                        backend.cleanup()
                    except:
                        pass

        if self.backends:
            print("\n" + "=" * 80)
            print("Running benchmark tests for API models...")
            print("=" * 80)

            for duration, audio_file in audio_files.items():
                print(f"\n🎵 Testing with {duration}s audio file...")

                for backend_name, backend in self.backends.items():
                    try:
                        result = self.test_model(backend_name, backend, audio_file, duration)
                        self.results.append(result)

                        if result.success:
                            print(f"  ✅ {backend_name}: {result.transcription_time:.2f}s")
                        else:
                            print(f"  ❌ {backend_name}: {result.error}")
                    except KeyboardInterrupt:
                        print("\n⚠️  Benchmark interrupted by user")
                        raise
                    except Exception as e:
                        error_msg = str(e)
                        if len(error_msg) > 100:
                            error_msg = error_msg[:97] + "..."
                        print(f"  ❌ {backend_name}: Unexpected error - {error_msg}")
                        self.results.append(TestResult(
                            model_name=backend_name,
                            audio_duration=duration,
                            transcription_time=0.0,
                            success=False,
                            error=f"Unexpected error: {error_msg}"
                        ))

        self.print_results()

    def print_results(self):
        print("\n" + "=" * 80)
        print("Benchmark Results Summary")
        print("=" * 80)

        if not self.results:
            print("No results to display.")
            return

        durations = sorted(set(r.audio_duration for r in self.results))
        models = sorted(set(r.model_name for r in self.results))

        print(f"\n{'Model':<25} {'Duration':<12} {'Time (s)':<12} {'Speed (x)':<12} {'Status':<10}")
        print("-" * 80)

        for duration in durations:
            print(f"\n📊 Audio Duration: {duration:.0f} seconds")
            print("-" * 80)

            duration_results = [r for r in self.results if r.audio_duration == duration]

            successful_results = [r for r in duration_results if r.success]
            if successful_results:
                fastest_time = min(r.transcription_time for r in successful_results)
            else:
                fastest_time = None

            for model in models:
                result = next((r for r in duration_results if r.model_name == model), None)
                if result:
                    if result.success:
                        speed_multiplier = fastest_time / result.transcription_time if fastest_time else 1.0
                        print(f"{result.model_name:<25} {result.audio_duration:<12.0f} "
                              f"{result.transcription_time:<12.2f} {speed_multiplier:<12.2f}x ✅")
                    else:
                        print(f"{result.model_name:<25} {result.audio_duration:<12.0f} "
                              f"{'N/A':<12} {'N/A':<12} ❌ {result.error}")

        print("\n" + "=" * 80)
        print("Fastest Model by Duration")
        print("=" * 80)

        for duration in durations:
            duration_results = [r for r in self.results
                               if r.audio_duration == duration and r.success]
            if duration_results:
                fastest = min(duration_results, key=lambda r: r.transcription_time)
                print(f"{duration:.0f}s: {fastest.model_name} ({fastest.transcription_time:.2f}s)")

        print("\n" + "=" * 80)
        print("Overall Statistics")
        print("=" * 80)

        successful_results = [r for r in self.results if r.success]
        if successful_results:
            total_tests = len(self.results)
            successful_tests = len(successful_results)
            avg_time = sum(r.transcription_time for r in successful_results) / len(successful_results)

            print(f"Total tests: {total_tests}")
            print(f"Successful: {successful_tests} ({successful_tests/total_tests*100:.1f}%)")
            print(f"Failed: {total_tests - successful_tests}")
            print(f"Average transcription time: {avg_time:.2f}s")

        print("\n" + "=" * 80)

    def cleanup(self):
        self.audio_generator.cleanup()

        for backend_name, backend in self.backends.items():
            try:
                backend.cleanup()
            except Exception as e:
                logger.debug(f"Error cleaning up {backend_name}: {e}")


def main(argv: Optional[List[str]] = None):
    import argparse

    parser = argparse.ArgumentParser(description="Model Speed & Throughput Benchmark")
    parser.add_argument(
        "--durations",
        type=str,
        default="10,30,120",
        help="Comma-separated audio durations in seconds to test (default: 10,30,120)",
    )
    parser.add_argument(
        "--models",
        type=str,
        default="base,base.en,tiny,tiny.en,turbo",
        help="Comma-separated local model names to test (default: base,base.en,tiny,tiny.en,turbo)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="Hardware device to use (default: auto)",
    )
    parser.add_argument(
        "--compute-type",
        type=str,
        default="auto",
        help="Compute type (float16, int8, float32, auto)",
    )
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="Skip testing OpenAI API models",
    )
    args = parser.parse_args(argv)

    try:
        durations = [float(d.strip()) for d in args.durations.split(",") if d.strip()]
    except ValueError:
        print("❌ Invalid durations specified. Must be comma-separated numbers.")
        return 2

    local_models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else []

    benchmark = None

    try:
        benchmark = ModelBenchmark(
            local_models=local_models,
            device=args.device,
            compute_type=args.compute_type,
            skip_api=args.skip_api,
        )

        if not benchmark.backends and not benchmark.local_models_to_test:
            print("\n❌ No transcription models or backends configured!")
            return 2

        benchmark.run_benchmark(durations=durations)

    except KeyboardInterrupt:
        print("\n\n⚠️  Benchmark interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Benchmark failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if benchmark:
            benchmark.cleanup()


if __name__ == "__main__":
    main()

