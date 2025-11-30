#!/usr/bin/env python3
"""
Model Benchmark Test Script

Tests all transcription models against known-length audio files (10s, 30s, 2min)
and measures transcription time to compare performance.

Usage:
    From project root:
        python tests/test_model_benchmark.py
    
    Or from tests folder:
        python test_model_benchmark.py
"""

import os
import sys
import time
import logging
import warnings
import subprocess
import platform
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

# Suppress warnings that might clutter output
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")


def _patch_subprocess_for_windows():
    """Patch subprocess.Popen to hide console windows on Windows.
    
    This prevents the console flash when running with pythonw.exe,
    especially when whisper calls ffmpeg internally via subprocess.
    """
    if platform.system() != "Windows":
        return
    
    # Store the original Popen
    _original_popen = subprocess.Popen
    
    class _NoConsolePopen(_original_popen):
        """Popen wrapper that adds CREATE_NO_WINDOW flag on Windows."""
        
        def __init__(self, *args, **kwargs):
            # Add CREATE_NO_WINDOW to creationflags if not already set
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW):
                kwargs['creationflags'] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    
    # Replace subprocess.Popen globally
    subprocess.Popen = _NoConsolePopen


# Apply the subprocess patch immediately on import (before whisper is loaded)
_patch_subprocess_for_windows()

# Add project root to path (go up one level from tests folder)
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)  # Go up one level from tests/ to project root
sys.path.insert(0, project_root)

# Import settings_manager early to ensure it's initialized
# (LocalWhisperBackend uses it internally, so we want it ready)
from settings import settings_manager

from transcriber.local_backend import LocalWhisperBackend
from transcriber.openai_backend import OpenAIBackend
from config import config

# Setup logging - redirect to null to reduce noise
logging.basicConfig(
    level=logging.CRITICAL,  # Only show critical errors
    format='%(levelname)s - %(message)s',
    handlers=[logging.NullHandler()]  # Suppress all logging output
)
logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result of a single transcription test."""
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
        """Normalize text to lowercase words only."""
        # Convert to lowercase
        text = text.lower()
        # Remove punctuation and extra whitespace
        text = re.sub(r'[^\w\s]', '', text)
        # Split into words and filter empty
        words = [w.strip() for w in text.split() if w.strip()]
        return words

    expected_words = normalize_text(expected)
    transcribed_words = normalize_text(transcribed)

    if not expected_words:
        return 100.0 if not transcribed_words else 0.0

    # Count matches using a simple sequential comparison
    # This is a basic approach - for more sophisticated comparison,
    # consider using Levenshtein distance or WER (Word Error Rate)
    matches = 0
    transcribed_set = set(transcribed_words)
    expected_counts = {}
    transcribed_counts = {}

    for word in expected_words:
        expected_counts[word] = expected_counts.get(word, 0) + 1

    for word in transcribed_words:
        transcribed_counts[word] = transcribed_counts.get(word, 0) + 1

    # Count how many expected words appear in transcription
    for word, count in expected_counts.items():
        transcribed_count = transcribed_counts.get(word, 0)
        matches += min(count, transcribed_count)

    accuracy = (matches / len(expected_words)) * 100.0
    return min(accuracy, 100.0)  # Cap at 100%


class AudioGenerator:
    """Generate test audio files of specified durations."""
    
    def __init__(self):
        """Initialize the audio generator."""
        # Use the tests folder (same folder as this script) for audio files
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = script_dir
        self._tts_available = None  # Cache TTS availability check
        logger.info(f"Using audio directory: {self.temp_dir}")
    
    def _check_tts_available(self) -> bool:
        """Check if TTS dependencies are available."""
        if self._tts_available is None:
            try:
                from gtts import gTTS
                from pydub import AudioSegment
                self._tts_available = True
            except ImportError:
                self._tts_available = False
        return self._tts_available
    
    def generate_tts_audio(self, duration_seconds: float, output_filename: str) -> Optional[Tuple[str, str]]:
        """
        Generate speech audio using gTTS.

        Args:
            duration_seconds: Target duration in seconds
            output_filename: Name for the output file

        Returns:
            Tuple of (path to generated audio file, expected text), or None if failed
        """
        if not self._check_tts_available():
            return None  # Silent return - fallback will be used

        try:
            from gtts import gTTS  # type: ignore
            from pydub import AudioSegment  # type: ignore
        except ImportError:
            return None

        # Text content for TTS - this is what will be spoken and transcribed
        # We use a standard paragraph that contains natural speech patterns
        base_text = (
            "This is a test audio file for benchmarking transcription models. "
            "We are measuring how long it takes each model to transcribe audio of different lengths. "
            "The transcription system needs to accurately convert speech to text while maintaining "
            "good performance. This test will help us understand which model works best for different "
            "use cases and audio durations. The audio contains natural speech patterns and common words "
            "that transcription systems should be able to handle effectively."
        )

        # Repeat the text to reach the target duration
        # Average speech rate is ~150 words per minute, base_text is ~50 words (~20 seconds)
        # We repeat it to fill the required duration
        repetitions = max(1, int(duration_seconds / 20))
        full_text = (base_text + " ") * repetitions

        print(f"  Text to be spoken: {len(full_text.split())} words (~{duration_seconds}s)")

        output_path = os.path.join(self.temp_dir, output_filename)

        try:
            # Generate TTS audio
            print(f"  Generating {duration_seconds}s audio with gTTS...")
            tts = gTTS(text=full_text, lang='en', slow=False)

            # Save to temp MP3 first
            temp_mp3 = os.path.join(self.temp_dir, "temp.mp3")
            tts.save(temp_mp3)

            # Convert to WAV
            audio = AudioSegment.from_mp3(temp_mp3)

            # Trim or pad to exact duration
            target_ms = int(duration_seconds * 1000)
            if len(audio) < target_ms:
                # Pad with silence
                silence = AudioSegment.silent(duration=target_ms - len(audio))
                audio = audio + silence
            else:
                # Trim to exact duration
                audio = audio[:target_ms]

            audio.export(output_path, format="wav")

            # Cleanup temp MP3
            try:
                os.remove(temp_mp3)
            except Exception:
                pass

            actual_duration = len(audio) / 1000.0
            print(f"  ✅ Generated: {output_path} ({actual_duration:.1f}s)")
            return (output_path, full_text.strip())

        except Exception as e:
            logger.error(f"Failed to generate TTS audio: {e}")
            return None
    
    
    def cleanup(self):
        """Clean up generated audio files."""
        try:
            # Remove only the generated test audio files, not the entire directory
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
    """Benchmark all transcription models."""
    
    def __init__(self):
        """Initialize the benchmark."""
        self.audio_generator = AudioGenerator()
        self.results: List[TestResult] = []
        
        # Initialize all backends
        self.backends: Dict[str, any] = {}
        
        # Local Whisper backend
        try:
            print("Initializing Local Whisper backend...")
            self.backends['local_whisper'] = LocalWhisperBackend()
            if not self.backends['local_whisper'].is_available():
                print("⚠️  Local Whisper backend not available")
            else:
                print("✅ Local Whisper backend initialized")
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 150:
                error_msg = error_msg[:147] + "..."
            print(f"⚠️  Failed to initialize Local Whisper: {error_msg}")
            print("   (This may be due to CUDA/cuDNN issues - will skip this model)")
        
        # OpenAI backends
        print("\nInitializing OpenAI backends...")
        for backend_name in ['api_whisper', 'api_gpt4o', 'api_gpt4o_mini']:
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
        """
        Test a single model with a single audio file.
        
        Args:
            backend_name: Name of the backend
            backend: Backend instance
            audio_file: Path to audio file
            duration: Duration of audio in seconds
            
        Returns:
            TestResult object
        """
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
            # Time the transcription
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
            # Re-raise keyboard interrupts
            raise
        except Exception as e:
            error_msg = str(e)
            # Truncate very long error messages
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
    
    def _print_local_whisper_config(self):
        """Print local whisper configuration details."""
        if 'local_whisper' not in self.backends:
            return

        backend = self.backends['local_whisper']

        print("\n" + "=" * 80)
        print("Local Whisper Configuration")
        print("=" * 80)

        # Model info
        print(f"\n  Model:        {backend.model_name}")
        print(f"  Device:       {backend._device}")
        print(f"  Compute Type: {backend._compute_type}")

        # Additional CUDA info if using GPU
        if backend._device == "cuda":
            try:
                import torch
                if torch.cuda.is_available():
                    gpu_name = torch.cuda.get_device_name(0)
                    gpu_memory = torch.cuda.get_device_properties(0).total_memory / (1024**3)
                    print(f"  GPU:          {gpu_name}")
                    print(f"  GPU Memory:   {gpu_memory:.1f} GB")

                    # CUDA version
                    cuda_version = torch.version.cuda
                    print(f"  CUDA Version: {cuda_version}")
            except Exception as e:
                print(f"  GPU Info:     (could not retrieve: {e})")

        # VAD settings from config
        try:
            print(f"\n  VAD Enabled:  {config.FASTER_WHISPER_VAD_ENABLED}")
            print(f"  Beam Size:    {config.FASTER_WHISPER_BEAM_SIZE}")
            if config.FASTER_WHISPER_VAD_ENABLED:
                print(f"  VAD Min Silence: {config.FASTER_WHISPER_VAD_MIN_SILENCE_MS}ms")
        except AttributeError:
            pass  # Config attributes not available

        print("")

    def run_benchmark(self, durations: List[float] = [10.0, 30.0, 120.0]):
        """
        Run benchmark tests for all models and durations.

        Args:
            durations: List of audio durations in seconds to test
        """
        print("=" * 80)
        print("Model Benchmark Test")
        print("=" * 80)
        print(f"\nTesting {len(self.backends)} models with {len(durations)} audio durations")
        print(f"Models: {', '.join(self.backends.keys())}")
        print(f"Durations: {', '.join([f'{d}s' for d in durations])}")

        # Print local whisper configuration if available
        self._print_local_whisper_config()
        print("\n" + "=" * 80)
        
        # Generate test audio files
        print("\n📁 Generating test audio files...")
        audio_files = {}
        
        # Check if TTS is available - required for meaningful transcription tests
        if not self.audio_generator._check_tts_available():
            print("\n❌ ERROR: gTTS and pydub are required for transcription benchmarking")
            print("   Synthetic audio (sine waves) cannot be meaningfully transcribed.")
            print("   Install with: pip install gtts pydub")
            print("\n   These dependencies are needed to generate actual speech audio")
            print("   that transcription models can transcribe to real words.")
            return
        
        for duration in durations:
            filename = f"test_{int(duration)}s.wav"
            audio_file = self.audio_generator.generate_tts_audio(duration, filename)
            
            if audio_file:
                audio_files[duration] = audio_file
            else:
                print(f"❌ Failed to generate {duration}s audio file")
                return
        
        if not audio_files:
            print("❌ Failed to generate any test audio files. Exiting.")
            return
        
        # Run tests
        print("\n" + "=" * 80)
        print("Running benchmark tests...")
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
                    # Catch any unexpected errors and continue
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
        
        # Print results summary
        self.print_results()
    
    def print_results(self):
        """Print formatted benchmark results."""
        print("\n" + "=" * 80)
        print("Benchmark Results Summary")
        print("=" * 80)
        
        if not self.results:
            print("No results to display.")
            return
        
        # Group results by duration
        durations = sorted(set(r.audio_duration for r in self.results))
        models = sorted(set(r.model_name for r in self.results))
        
        # Print table header
        print(f"\n{'Model':<25} {'Duration':<12} {'Time (s)':<12} {'Speed (x)':<12} {'Status':<10}")
        print("-" * 80)
        
        for duration in durations:
            print(f"\n📊 Audio Duration: {duration:.0f} seconds")
            print("-" * 80)
            
            # Get results for this duration
            duration_results = [r for r in self.results if r.audio_duration == duration]
            
            # Find fastest successful result for speed comparison
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
        
        # Print fastest model summary
        print("\n" + "=" * 80)
        print("Fastest Model by Duration")
        print("=" * 80)
        
        for duration in durations:
            duration_results = [r for r in self.results 
                               if r.audio_duration == duration and r.success]
            if duration_results:
                fastest = min(duration_results, key=lambda r: r.transcription_time)
                print(f"{duration:.0f}s: {fastest.model_name} ({fastest.transcription_time:.2f}s)")
        
        # Print overall statistics
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
        """Clean up resources."""
        self.audio_generator.cleanup()
        
        # Cleanup backends
        for backend_name, backend in self.backends.items():
            try:
                backend.cleanup()
            except Exception as e:
                logger.debug(f"Error cleaning up {backend_name}: {e}")


def main():
    """Main entry point."""
    benchmark = None
    
    try:
        benchmark = ModelBenchmark()
        
        if not benchmark.backends:
            print("\n❌ No transcription backends available!")
            print("   Please check:")
            print("   - Local Whisper: Ensure faster-whisper is installed")
            print("   - API models: Set OPENAI_API_KEY environment variable")
            return
        
        # Run benchmark with default durations: 10s, 30s, 2min
        benchmark.run_benchmark(durations=[10.0, 30.0, 120.0])
        
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

