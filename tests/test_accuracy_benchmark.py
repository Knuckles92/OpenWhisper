#!/usr/bin/env python3
"""
Model Accuracy Benchmark Test Script

Tests all transcription models against generated speech audio with known text
and measures transcription accuracy using word-level comparison.

Usage:
    From project root:
        python tests/test_accuracy_benchmark.py

    Or from tests folder:
        python test_accuracy_benchmark.py
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
from dataclasses import dataclass, field

# Suppress warnings that might clutter output
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", message=".*pkg_resources.*")


def _patch_subprocess_for_windows():
    """Patch subprocess.Popen to hide console windows on Windows."""
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


_patch_subprocess_for_windows()

# Add project root to path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from settings import settings_manager
from transcriber.local_backend import LocalWhisperBackend
from transcriber.openai_backend import OpenAIBackend
from config import config

# Setup logging - minimal output
logging.basicConfig(
    level=logging.CRITICAL,
    format='%(levelname)s - %(message)s',
    handlers=[logging.NullHandler()]
)
logger = logging.getLogger(__name__)


# Test text samples - varied content types for comprehensive accuracy testing
TEST_SAMPLES = {
    "conversational": {
        "name": "Conversational Speech",
        "text": (
            "Hey, how's it going? I was thinking we could grab some coffee later "
            "and talk about the project. You know, the one we've been working on "
            "for the past few weeks. I think we're making good progress, but there "
            "are still a few things we need to figure out before the deadline."
        ),
        "description": "Casual conversational speech with contractions"
    },
    "technical": {
        "name": "Technical Content",
        "text": (
            "The application uses a microservices architecture with Docker containers "
            "orchestrated by Kubernetes. Each service communicates via REST APIs and "
            "message queues. The database layer consists of PostgreSQL for relational "
            "data and Redis for caching. Authentication is handled through OAuth 2.0 "
            "with JSON Web Tokens for session management."
        ),
        "description": "Technical jargon with acronyms and specific terminology"
    },
    "numbers_dates": {
        "name": "Numbers and Dates",
        "text": (
            "The meeting is scheduled for January 15th, 2025, at 3:30 PM. "
            "We expect approximately 250 attendees. The budget allocation is "
            "$45,000 for phase one and $127,500 for phase two. Please confirm "
            "your attendance by December 28th. The room capacity is 300 people."
        ),
        "description": "Content with numbers, dates, times, and currency"
    },
    "names_places": {
        "name": "Proper Nouns",
        "text": (
            "Dr. Elizabeth Thompson from Stanford University will present her research "
            "on artificial intelligence at the conference in San Francisco. She previously "
            "worked at Google and Microsoft before joining the academic sector. "
            "Her colleague, Professor James Chen from MIT, will co-present the findings."
        ),
        "description": "Proper nouns including names, institutions, and locations"
    }
}


@dataclass
class AccuracyResult:
    """Result of a single accuracy test."""
    model_name: str
    sample_id: str
    sample_name: str
    expected_text: str
    transcribed_text: str
    word_accuracy: float
    character_accuracy: float
    transcription_time: float
    success: bool
    error: Optional[str] = None


@dataclass
class ModelSummary:
    """Summary statistics for a model across all samples."""
    model_name: str
    avg_word_accuracy: float = 0.0
    avg_char_accuracy: float = 0.0
    total_tests: int = 0
    successful_tests: int = 0
    results_by_sample: Dict[str, AccuracyResult] = field(default_factory=dict)


def calculate_word_accuracy(expected: str, transcribed: str) -> float:
    """
    Calculate word-level accuracy between expected and transcribed text.

    Returns percentage of expected words correctly transcribed (0.0 to 100.0).
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

    expected_counts = {}
    transcribed_counts = {}

    for word in expected_words:
        expected_counts[word] = expected_counts.get(word, 0) + 1

    for word in transcribed_words:
        transcribed_counts[word] = transcribed_counts.get(word, 0) + 1

    matches = 0
    for word, count in expected_counts.items():
        transcribed_count = transcribed_counts.get(word, 0)
        matches += min(count, transcribed_count)

    accuracy = (matches / len(expected_words)) * 100.0
    return min(accuracy, 100.0)


def calculate_character_accuracy(expected: str, transcribed: str) -> float:
    """
    Calculate character-level accuracy using Levenshtein distance.

    Returns percentage similarity (0.0 to 100.0).
    """
    import re

    def normalize(text: str) -> str:
        text = text.lower()
        text = re.sub(r'[^\w\s]', '', text)
        text = ' '.join(text.split())
        return text

    s1 = normalize(expected)
    s2 = normalize(transcribed)

    if not s1 and not s2:
        return 100.0
    if not s1 or not s2:
        return 0.0

    # Levenshtein distance calculation
    m, n = len(s1), len(s2)

    # Use two rows for memory efficiency
    prev_row = list(range(n + 1))
    curr_row = [0] * (n + 1)

    for i in range(1, m + 1):
        curr_row[0] = i
        for j in range(1, n + 1):
            if s1[i-1] == s2[j-1]:
                curr_row[j] = prev_row[j-1]
            else:
                curr_row[j] = 1 + min(prev_row[j], curr_row[j-1], prev_row[j-1])
        prev_row, curr_row = curr_row, prev_row

    distance = prev_row[n]
    max_len = max(m, n)
    similarity = ((max_len - distance) / max_len) * 100.0
    return max(0.0, similarity)


class AudioGenerator:
    """Generate test audio files from text using TTS."""

    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = script_dir
        self._tts_available = None

    def _check_tts_available(self) -> bool:
        if self._tts_available is None:
            try:
                from gtts import gTTS
                from pydub import AudioSegment
                self._tts_available = True
            except ImportError:
                self._tts_available = False
        return self._tts_available

    def generate_audio(self, text: str, filename: str) -> Optional[str]:
        """
        Generate speech audio from text.

        Returns path to generated WAV file, or None if failed.
        """
        if not self._check_tts_available():
            return None

        try:
            from gtts import gTTS
            from pydub import AudioSegment
        except ImportError:
            return None

        output_path = os.path.join(self.temp_dir, filename)

        try:
            tts = gTTS(text=text, lang='en', slow=False)
            temp_mp3 = os.path.join(self.temp_dir, f"temp_{filename}.mp3")
            tts.save(temp_mp3)

            audio = AudioSegment.from_mp3(temp_mp3)
            audio.export(output_path, format="wav")

            try:
                os.remove(temp_mp3)
            except Exception:
                pass

            return output_path

        except Exception as e:
            logger.error(f"Failed to generate audio: {e}")
            return None

    def cleanup(self, filenames: List[str]):
        """Clean up generated audio files."""
        for filename in filenames:
            try:
                filepath = os.path.join(self.temp_dir, filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass


class AccuracyBenchmark:
    """Benchmark transcription accuracy across models."""

    def __init__(self):
        self.audio_generator = AudioGenerator()
        self.results: List[AccuracyResult] = []
        self.backends: Dict[str, any] = {}
        self.generated_files: List[str] = []

        # Initialize backends
        print("Initializing transcription backends...")
        print("-" * 60)

        # Local Whisper
        try:
            self.backends['local_whisper'] = LocalWhisperBackend()
            if self.backends['local_whisper'].is_available():
                print("  ✅ local_whisper")
            else:
                print("  ⚠️  local_whisper (not available)")
                del self.backends['local_whisper']
        except Exception as e:
            print(f"  ❌ local_whisper: {str(e)[:60]}...")

        # OpenAI backends
        for backend_name in ['api_whisper', 'api_gpt4o', 'api_gpt4o_mini']:
            try:
                backend = OpenAIBackend(backend_name)
                if backend.is_available():
                    self.backends[backend_name] = backend
                    print(f"  ✅ {backend_name}")
                else:
                    print(f"  ⚠️  {backend_name} (missing API key?)")
            except Exception as e:
                print(f"  ❌ {backend_name}: {str(e)[:60]}...")

        print("-" * 60)
        print(f"Initialized {len(self.backends)} backend(s)\n")

    def test_sample(self, backend_name: str, backend, sample_id: str,
                    sample: dict, audio_file: str) -> AccuracyResult:
        """Test a single sample with a single model."""
        expected_text = sample["text"]

        try:
            if not backend.is_available():
                return AccuracyResult(
                    model_name=backend_name,
                    sample_id=sample_id,
                    sample_name=sample["name"],
                    expected_text=expected_text,
                    transcribed_text="",
                    word_accuracy=0.0,
                    character_accuracy=0.0,
                    transcription_time=0.0,
                    success=False,
                    error="Backend not available"
                )
        except Exception as e:
            return AccuracyResult(
                model_name=backend_name,
                sample_id=sample_id,
                sample_name=sample["name"],
                expected_text=expected_text,
                transcribed_text="",
                word_accuracy=0.0,
                character_accuracy=0.0,
                transcription_time=0.0,
                success=False,
                error=str(e)
            )

        try:
            start_time = time.time()
            transcribed_text = backend.transcribe(audio_file)
            transcription_time = time.time() - start_time

            word_acc = calculate_word_accuracy(expected_text, transcribed_text)
            char_acc = calculate_character_accuracy(expected_text, transcribed_text)

            return AccuracyResult(
                model_name=backend_name,
                sample_id=sample_id,
                sample_name=sample["name"],
                expected_text=expected_text,
                transcribed_text=transcribed_text,
                word_accuracy=word_acc,
                character_accuracy=char_acc,
                transcription_time=transcription_time,
                success=True
            )

        except KeyboardInterrupt:
            raise
        except Exception as e:
            return AccuracyResult(
                model_name=backend_name,
                sample_id=sample_id,
                sample_name=sample["name"],
                expected_text=expected_text,
                transcribed_text="",
                word_accuracy=0.0,
                character_accuracy=0.0,
                transcription_time=0.0,
                success=False,
                error=str(e)[:100]
            )

    def run_benchmark(self):
        """Run accuracy benchmark on all models with all samples."""
        print("=" * 80)
        print("Model Accuracy Benchmark")
        print("=" * 80)
        print(f"\nTesting {len(self.backends)} model(s) with {len(TEST_SAMPLES)} sample(s)")
        print(f"Models: {', '.join(self.backends.keys())}")
        print(f"Samples: {', '.join(TEST_SAMPLES.keys())}")

        # Check TTS availability
        if not self.audio_generator._check_tts_available():
            print("\n❌ ERROR: gTTS and pydub are required for accuracy benchmarking")
            print("   Install with: pip install gtts pydub")
            return

        # Generate audio files for all samples
        print("\n" + "=" * 80)
        print("Generating test audio files...")
        print("=" * 80)

        audio_files: Dict[str, str] = {}

        for sample_id, sample in TEST_SAMPLES.items():
            print(f"\n📝 {sample['name']}: {sample['description']}")
            filename = f"accuracy_test_{sample_id}.wav"

            audio_path = self.audio_generator.generate_audio(sample["text"], filename)

            if audio_path:
                audio_files[sample_id] = audio_path
                self.generated_files.append(filename)
                print(f"   ✅ Generated: {filename}")
            else:
                print(f"   ❌ Failed to generate audio")

        if not audio_files:
            print("\n❌ Failed to generate any audio files. Exiting.")
            return

        # Run tests
        print("\n" + "=" * 80)
        print("Running accuracy tests...")
        print("=" * 80)

        for sample_id, audio_file in audio_files.items():
            sample = TEST_SAMPLES[sample_id]
            print(f"\n🎤 Testing: {sample['name']}")
            print(f"   {sample['description']}")
            print("-" * 60)

            for backend_name, backend in self.backends.items():
                print(f"   Testing {backend_name}...", end=" ", flush=True)

                try:
                    result = self.test_sample(
                        backend_name, backend, sample_id, sample, audio_file
                    )
                    self.results.append(result)

                    if result.success:
                        print(f"Word: {result.word_accuracy:.1f}% | "
                              f"Char: {result.character_accuracy:.1f}%")
                    else:
                        print(f"❌ {result.error}")

                except KeyboardInterrupt:
                    print("\n⚠️  Interrupted by user")
                    raise
                except Exception as e:
                    print(f"❌ Error: {str(e)[:50]}")

        # Print results
        self.print_results()

    def print_results(self):
        """Print detailed benchmark results."""
        print("\n" + "=" * 80)
        print("ACCURACY BENCHMARK RESULTS")
        print("=" * 80)

        if not self.results:
            print("No results to display.")
            return

        # Calculate model summaries
        model_summaries: Dict[str, ModelSummary] = {}

        for result in self.results:
            if result.model_name not in model_summaries:
                model_summaries[result.model_name] = ModelSummary(
                    model_name=result.model_name
                )

            summary = model_summaries[result.model_name]
            summary.total_tests += 1
            summary.results_by_sample[result.sample_id] = result

            if result.success:
                summary.successful_tests += 1

        # Calculate averages
        for summary in model_summaries.values():
            successful = [r for r in summary.results_by_sample.values() if r.success]
            if successful:
                summary.avg_word_accuracy = sum(r.word_accuracy for r in successful) / len(successful)
                summary.avg_char_accuracy = sum(r.character_accuracy for r in successful) / len(successful)

        # Print detailed results by sample
        print("\n📊 Results by Sample Type")
        print("-" * 80)

        for sample_id, sample in TEST_SAMPLES.items():
            print(f"\n{sample['name']} ({sample_id})")
            print(f"   \"{sample['text'][:60]}...\"")
            print()

            sample_results = [r for r in self.results if r.sample_id == sample_id]

            print(f"   {'Model':<20} {'Word Acc':>10} {'Char Acc':>10} {'Status':>10}")
            print(f"   {'-'*20} {'-'*10} {'-'*10} {'-'*10}")

            for result in sorted(sample_results, key=lambda r: r.word_accuracy, reverse=True):
                if result.success:
                    print(f"   {result.model_name:<20} "
                          f"{result.word_accuracy:>9.1f}% "
                          f"{result.character_accuracy:>9.1f}% "
                          f"{'✅':>10}")
                else:
                    print(f"   {result.model_name:<20} "
                          f"{'N/A':>10} "
                          f"{'N/A':>10} "
                          f"{'❌':>10}")

        # Print model rankings
        print("\n" + "=" * 80)
        print("📈 MODEL RANKINGS (by Average Word Accuracy)")
        print("=" * 80)

        ranked = sorted(
            model_summaries.values(),
            key=lambda s: s.avg_word_accuracy,
            reverse=True
        )

        print(f"\n{'Rank':<6} {'Model':<20} {'Avg Word':>12} {'Avg Char':>12} {'Tests':>10}")
        print(f"{'-'*6} {'-'*20} {'-'*12} {'-'*12} {'-'*10}")

        for i, summary in enumerate(ranked, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "  "
            print(f"{medal} {i:<3} {summary.model_name:<20} "
                  f"{summary.avg_word_accuracy:>11.1f}% "
                  f"{summary.avg_char_accuracy:>11.1f}% "
                  f"{summary.successful_tests}/{summary.total_tests:>7}")

        # Print winner
        if ranked and ranked[0].avg_word_accuracy > 0:
            print("\n" + "=" * 80)
            winner = ranked[0]
            print(f"🏆 WINNER: {winner.model_name}")
            print(f"   Average Word Accuracy: {winner.avg_word_accuracy:.1f}%")
            print(f"   Average Character Accuracy: {winner.avg_char_accuracy:.1f}%")

        # Print sample transcription comparison for best model
        if ranked and ranked[0].successful_tests > 0:
            print("\n" + "=" * 80)
            print("📝 SAMPLE TRANSCRIPTION COMPARISON (Best Model)")
            print("=" * 80)

            best_model = ranked[0]
            # Show first successful sample
            for sample_id, result in best_model.results_by_sample.items():
                if result.success:
                    print(f"\nSample: {result.sample_name}")
                    print(f"\nExpected ({len(result.expected_text)} chars):")
                    print(f"  \"{result.expected_text[:200]}{'...' if len(result.expected_text) > 200 else ''}\"")
                    print(f"\nTranscribed ({len(result.transcribed_text)} chars):")
                    print(f"  \"{result.transcribed_text[:200]}{'...' if len(result.transcribed_text) > 200 else ''}\"")
                    print(f"\nAccuracy: Word={result.word_accuracy:.1f}%, Char={result.character_accuracy:.1f}%")
                    break

        print("\n" + "=" * 80)

    def cleanup(self):
        """Clean up resources."""
        self.audio_generator.cleanup(self.generated_files)

        for backend_name, backend in self.backends.items():
            try:
                backend.cleanup()
            except Exception:
                pass


def main():
    """Main entry point."""
    benchmark = None

    try:
        benchmark = AccuracyBenchmark()

        if not benchmark.backends:
            print("\n❌ No transcription backends available!")
            print("   Please check:")
            print("   - Local Whisper: Ensure faster-whisper is installed")
            print("   - API models: Set OPENAI_API_KEY environment variable")
            return

        benchmark.run_benchmark()

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
