#!/usr/bin/env python3
"""
Test audio generation script for testing chunking functionality.
Generates large audio files (>23MB) to test the audio splitting and chunked transcription.
"""

import os
import sys
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TestAudioGenerator:
    """Generates test audio files for chunking tests."""

    def __init__(self):
        """Initialize the audio generator."""
        self.temp_dir = tempfile.mkdtemp(prefix="test_audio_")
        logger.info(f"Created temp directory: {self.temp_dir}")

    def generate_long_speech_gtts(self, output_file: str = "test_long_speech.wav",
                                  target_size_mb: float = 30.0) -> Optional[str]:
        """
        Generate a long speech audio file using gTTS.
        Creates realistic speech that will test transcription accuracy.

        Args:
            output_file: Output filename
            target_size_mb: Target file size in MB (should be >23 to trigger chunking)

        Returns:
            Path to generated file, or None if failed
        """
        try:
            from gtts import gTTS
            import pydub
            from pydub import AudioSegment
        except ImportError:
            logger.error("Missing dependencies. Install with: pip install gtts pydub")
            return None

        # Long text content that will generate >23MB of audio
        long_text = """
        This is a comprehensive test of the audio chunking functionality in our transcription system.
        We need to generate a very long audio file that will exceed the twenty-three megabyte limit
        that triggers the automatic splitting mechanism. By creating this extended content, we can
        thoroughly test how the system handles large audio files, splits them into manageable chunks,
        processes each chunk individually, and then combines the transcriptions back together seamlessly.

        The chunking process involves several important steps. First, the system analyzes the audio file
        to determine if it exceeds the maximum allowed size. When it does, the audio processor begins
        the splitting workflow. It uses sophisticated silence detection algorithms to find natural
        break points in the audio where it can safely divide the content without cutting through words
        or sentences. This ensures that the resulting chunks maintain their semantic integrity.

        Once the split points are identified, the system creates individual audio chunks with small
        overlaps to prevent any loss of context at the boundaries. Each chunk is then processed by
        the transcription backend, whether it's the local Whisper model or one of the OpenAI API options.
        The backend transcribes each chunk and returns the text content, which is then carefully
        combined with the results from other chunks to create a complete transcription.

        This approach allows us to handle audio files of virtually any length while maintaining
        transcription quality and system performance. The overlap between chunks ensures that no
        content is lost during the splitting process, and the intelligent combination of results
        maintains the natural flow of the original speech. Testing this functionality with a long
        audio file like this one helps us verify that all components work together correctly.

        Let me continue with more content to ensure we reach our target file size. The development
        of this transcription system has involved careful consideration of many technical challenges.
        Audio processing requires handling different file formats, sample rates, and quality levels.
        The system must be robust enough to handle various audio sources while maintaining consistent
        transcription accuracy. This means implementing proper error handling, retry mechanisms, and
        user feedback throughout the transcription process.

        The user interface plays a crucial role in making this complex functionality accessible and
        easy to use. Status updates, progress indicators, and clear error messages help users
        understand what's happening during long transcription jobs. The waveform overlay provides
        visual feedback about the recording and processing states, making the system feel responsive
        and professional. Hotkey support allows for quick access to common functions without
        needing to interact with the graphical interface.

        Quality assurance is essential for a system like this. We've implemented comprehensive
        testing strategies that cover unit tests, integration tests, and user acceptance testing.
        The test suite includes specific tests for the chunking functionality, ensuring that
        large files are properly split, processed, and recombined. Mock objects and test fixtures
        help simulate various scenarios without requiring actual audio hardware or network calls.

        Documentation and maintainability are also key considerations. Clear code comments, type
        hints, and comprehensive docstrings make the codebase accessible to other developers.
        The modular architecture allows for easy extension and modification as new requirements
        emerge. Configuration management through dedicated settings files and environment variables
        ensures flexibility across different deployment scenarios.

        Security and privacy are important aspects of any application that handles audio data.
        The system implements proper data handling practices, secure API key management, and
        appropriate cleanup of temporary files. Users can be confident that their audio data
        is handled responsibly throughout the transcription process.

        Looking ahead, there are many opportunities for enhancing this system further. Integration
        with additional transcription services, support for more audio formats, and advanced
        audio processing features could expand the system's capabilities. Machine learning
        techniques could be applied to improve transcription accuracy and adapt to different
        speakers and accents. Real-time processing capabilities and streaming support could
        enable new use cases and workflows.

        The development process itself has been an interesting journey. Starting from a simple
        recording application, we've evolved it into a sophisticated transcription system with
        advanced features like automatic chunking, multiple backend support, and comprehensive
        user interface. Each feature has required careful design, implementation, and testing
        to ensure it integrates well with the existing codebase and provides value to users.

        This concludes our comprehensive test of the audio chunking functionality. The system
        should have generated a sufficiently long audio file to trigger the splitting mechanism
        and test all aspects of the chunked transcription process. If everything works correctly,
        you should see the file being split into multiple chunks, each chunk being processed
        individually, and the results being combined into a single coherent transcription.
        """

        # Repeat the text multiple times to reach target size
        repetitions = max(1, int(target_size_mb / 5))  # Estimate ~5MB per repetition
        full_text = long_text * repetitions

        logger.info(f"Generating speech from {len(full_text)} characters ({repetitions} repetitions)")

        try:
            # Generate speech
            tts = gTTS(text=full_text, lang='en', slow=False)
            mp3_file = os.path.join(self.temp_dir, "temp_speech.mp3")
            tts.save(mp3_file)

            # Convert to WAV format
            audio = AudioSegment.from_mp3(mp3_file)
            audio.export(output_file, format="wav")

            # Check file size
            file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            logger.info(f"Generated audio file: {output_file} ({file_size_mb:.1f} MB)")

            if file_size_mb < 23:
                logger.warning(f"File size ({file_size_mb:.1f} MB) is below chunking threshold (23 MB)")
                logger.info("Consider increasing target_size_mb or adding more text content")

            return output_file

        except Exception as e:
            logger.error(f"Failed to generate speech audio: {e}")
            return None

    def generate_synthetic_audio(self, output_file: str = "test_synthetic.wav",
                                duration_seconds: int = 300, sample_rate: int = 44100) -> Optional[str]:
        """
        Generate synthetic audio using numpy.
        Creates a simple test signal that can be used for basic chunking tests.

        Args:
            output_file: Output filename
            duration_seconds: Duration of the audio
            sample_rate: Sample rate

        Returns:
            Path to generated file, or None if failed
        """
        try:
            import numpy as np
            import wave

            logger.info(f"Generating synthetic audio: {duration_seconds}s at {sample_rate}Hz")

            # Generate a simple test signal (combination of tones and noise)
            t = np.linspace(0, duration_seconds, int(sample_rate * duration_seconds), False)

            # Create a mix of different frequencies to simulate speech-like content
            signal = (
                0.3 * np.sin(2 * np.pi * 440 * t) +  # A4 note
                0.2 * np.sin(2 * np.pi * 880 * t) +  # A5 note
                0.1 * np.random.normal(0, 1, len(t))  # Add some noise
            )

            # Normalize to 16-bit range
            signal = np.int16(signal / np.max(np.abs(signal)) * 32767)

            # Save as WAV
            with wave.open(output_file, 'wb') as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(signal.tobytes())

            file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            logger.info(f"Generated synthetic audio: {output_file} ({file_size_mb:.1f} MB)")

            return output_file

        except ImportError:
            logger.error("Missing numpy dependency. Install with: pip install numpy")
            return None
        except Exception as e:
            logger.error(f"Failed to generate synthetic audio: {e}")
            return None

    def create_large_file_from_existing(self, source_file: str = "speech.mp3",
                                       output_file: str = "test_large_from_existing.wav",
                                       target_size_mb: float = 30.0) -> Optional[str]:
        """
        Create a large test file by concatenating an existing audio file multiple times.

        Args:
            source_file: Path to existing audio file to replicate
            output_file: Output filename
            target_size_mb: Target file size in MB

        Returns:
            Path to generated file, or None if failed
        """
        if not os.path.exists(source_file):
            logger.error(f"Source file not found: {source_file}")
            return None

        try:
            import pydub
            from pydub import AudioSegment

            # Load source audio
            audio = AudioSegment.from_file(source_file)

            # Calculate how many copies we need
            source_size_mb = len(audio) / 1000 / 60  # Rough estimate: 1 minute ≈ 10MB for MP3
            repetitions = max(1, int(target_size_mb / source_size_mb))

            logger.info(f"Concatenating {source_file} {repetitions} times")

            # Concatenate
            combined = audio
            for i in range(repetitions - 1):
                combined += audio

            # Export as WAV
            combined.export(output_file, format="wav")

            file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
            logger.info(f"Generated large file: {output_file} ({file_size_mb:.1f} MB)")

            return output_file

        except ImportError:
            logger.error("Missing pydub dependency. Install with: pip install pydub")
            return None
        except Exception as e:
            logger.error(f"Failed to create large file: {e}")
            return None

    def cleanup(self):
        """Clean up temporary files."""
        try:
            import shutil
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.info("Cleaned up temporary files")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp files: {e}")


def main():
    """Main function to generate test audio."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate test audio files for chunking tests")
    parser.add_argument("--method", choices=["speech", "synthetic", "existing"],
                       default="speech", help="Generation method")
    parser.add_argument("--output", default="test_audio.wav", help="Output filename")
    parser.add_argument("--size", type=float, default=30.0,
                       help="Target file size in MB (should be >23 for chunking)")
    parser.add_argument("--duration", type=int, default=300,
                       help="Duration for synthetic audio (seconds)")

    args = parser.parse_args()

    generator = TestAudioGenerator()

    try:
        if args.method == "speech":
            logger.info("Generating long speech audio using gTTS...")
            result = generator.generate_long_speech_gtts(args.output, args.size)

        elif args.method == "synthetic":
            logger.info("Generating synthetic audio...")
            result = generator.generate_synthetic_audio(args.output, args.duration)

        elif args.method == "existing":
            logger.info("Creating large file from existing audio...")
            result = generator.create_large_file_from_existing("speech.mp3", args.output, args.size)

        if result:
            file_size_mb = os.path.getsize(result) / (1024 * 1024)
            print("2")
            print(f"Generated: {os.path.abspath(result)}")
            print(f"Size: {file_size_mb:.1f} MB")

            if file_size_mb >= 23:
                print("✅ File is large enough to trigger chunking (>23 MB)")
            else:
                print("⚠️  File size is below chunking threshold (23 MB)")
                print("   Consider increasing --size parameter or using different content")

        else:
            print("❌ Failed to generate test audio")
            sys.exit(1)

    finally:
        generator.cleanup()


if __name__ == "__main__":
    main()
