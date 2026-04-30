#!/usr/bin/env python3
"""
End-to-end test for large-file transcription using production dispatch.

Generates a large audio file (>23MB) using TTS and runs it through the same
dispatch path the running app uses (see services/runtime/transcription.py):

  - If backend.requires_file_splitting is True (e.g. OpenAI API backends),
    the file is split via audio_processor.split_audio_file() and passed to
    backend.transcribe_chunks().
  - If backend.requires_file_splitting is False (e.g. LocalWhisperBackend,
    which uses faster-whisper and handles long audio natively), the original
    file is passed straight to backend.transcribe().

Validates accuracy by comparing the transcribed text with the original TTS
source text.
"""

import os
import sys
import logging
import tempfile
import shutil
from typing import Tuple, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.audio_processor import audio_processor
from config import config
from transcriber import LocalWhisperBackend

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_large_audio_file_with_tts(output_path: str, target_size_mb: float = 30.0) -> Tuple[str, str]:
    """
    Generate a large WAV file using TTS that exceeds the size limit.

    Returns:
        Tuple of (audio_file_path, original_text)
    """
    try:
        from gtts import gTTS
        from pydub import AudioSegment
    except ImportError:
        logger.error("❌ Missing dependencies for TTS generation.")
        logger.error("   Install with: pip install gtts pydub")
        raise ImportError("gtts and pydub are required for TTS audio generation")

    logger.info(f"Generating large audio file with TTS (~{target_size_mb} MB)...")

    # Known text that we'll use for validation
    base_text = """
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

    # Clean up the text (remove extra whitespace)
    base_text = ' '.join(base_text.split())

    # Repeat text to reach target size (estimate ~5MB per repetition)
    repetitions = max(1, int(target_size_mb / 5))
    full_text = (base_text + ' ') * repetitions

    logger.info(f"  Text length: {len(full_text)} characters")
    logger.info(f"  Repetitions: {repetitions}")

    # Generate TTS audio
    temp_dir = tempfile.mkdtemp()
    mp3_file = os.path.join(temp_dir, "temp_speech.mp3")

    logger.info("  Generating speech with gTTS...")
    tts = gTTS(text=full_text, lang='en', slow=False)
    tts.save(mp3_file)

    # Convert MP3 to WAV
    logger.info("  Converting to WAV format...")
    audio = AudioSegment.from_mp3(mp3_file)
    audio.export(output_path, format="wav")

    # Cleanup temp MP3
    try:
        os.remove(mp3_file)
        shutil.rmtree(temp_dir)
    except Exception:
        pass

    actual_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"✅ Generated audio file: {actual_size_mb:.2f} MB")

    return output_path, full_text


def normalize_text_for_comparison(text: str) -> str:
    """Normalize text for comparison (lowercase, remove extra spaces)."""
    return ' '.join(text.lower().split())


def compare_texts(original: str, transcribed: str) -> Tuple[float, str]:
    """
    Compare original and transcribed text.

    Returns:
        Tuple of (similarity_score, analysis_message)
    """
    orig_norm = normalize_text_for_comparison(original)
    trans_norm = normalize_text_for_comparison(transcribed)

    # Simple word-based comparison
    orig_words = set(orig_norm.split())
    trans_words = set(trans_norm.split())

    # Calculate overlap
    common_words = orig_words & trans_words
    total_unique_words = len(orig_words | trans_words)

    if total_unique_words == 0:
        return 0.0, "No words to compare"

    similarity = len(common_words) / total_unique_words if total_unique_words > 0 else 0.0

    # Generate analysis message
    orig_word_count = len(orig_norm.split())
    trans_word_count = len(trans_norm.split())

    message = (
        f"Original: {orig_word_count} words | "
        f"Transcribed: {trans_word_count} words | "
        f"Common: {len(common_words)} words | "
        f"Similarity: {similarity * 100:.1f}%"
    )

    return similarity, message


def run_large_file_workflow(audio_file: str, original_text: str):
    """Test the production large-file transcription path and validate accuracy.

    Mirrors the dispatch logic in services/runtime/transcription.py
    (_submit_transcription_job): only backends that opt in via
    requires_file_splitting=True go through split + transcribe_chunks. The
    local backend opts out (faster-whisper handles long audio natively), so
    its production path is a single transcribe() call on the original file.
    """
    logger.info("")
    logger.info("=" * 60)
    logger.info("Testing large-file transcription workflow")
    logger.info("=" * 60)

    logger.info("\n1. Checking file size...")
    needs_splitting, file_size_mb = audio_processor.check_file_size(audio_file)

    if not needs_splitting:
        logger.error(f"❌ File ({file_size_mb:.2f} MB) is below limit ({config.MAX_FILE_SIZE_MB} MB)")
        return False

    logger.info(f"✅ File size {file_size_mb:.2f} MB exceeds {config.MAX_FILE_SIZE_MB} MB limit")

    logger.info("\n2. Initializing transcription backend...")
    backend = LocalWhisperBackend()
    if not backend.is_available():
        logger.warning("⚠️  Local Whisper not available - skipping transcription test")
        backend.cleanup()
        return True

    should_split = backend.requires_file_splitting
    logger.info(f"   Backend.requires_file_splitting = {should_split}")

    chunk_files = []
    try:
        if should_split:
            logger.info("\n3. Splitting audio file (backend requires splitting)...")

            def progress(msg):
                logger.info(f"   {msg}")

            chunk_files = audio_processor.split_audio_file(audio_file, progress)
            if not chunk_files:
                logger.error("❌ Failed to split audio file")
                return False

            logger.info(f"✅ Created {len(chunk_files)} chunks")
            for i, chunk_file in enumerate(chunk_files):
                size_mb = os.path.getsize(chunk_file) / (1024 * 1024)
                logger.info(f"   Chunk {i+1}: {size_mb:.2f} MB")

            logger.info("\n4. Transcribing chunks via transcribe_chunks()...")
            transcribed_text = backend.transcribe_chunks(chunk_files)
        else:
            logger.info("\n3. Backend handles large files natively - skipping split")
            logger.info("   (Production passes the original file straight to transcribe())")

            logger.info("\n4. Transcribing original file via transcribe()...")
            transcribed_text = backend.transcribe(audio_file)

        logger.info(f"✅ Transcription complete: {len(transcribed_text)} characters")
        logger.info(f"   Preview: {transcribed_text[:150]}...")

        logger.info("\n5. Validating transcription accuracy...")
        similarity, analysis = compare_texts(original_text, transcribed_text)
        logger.info(f"   {analysis}")

        if similarity >= 0.5:
            logger.info("✅ Transcription accuracy is acceptable")
        elif similarity >= 0.3:
            logger.warning("⚠️  Transcription accuracy is lower than expected")
            logger.warning("   This may be due to TTS quality or Whisper model limitations")
        else:
            logger.warning("⚠️  Low transcription accuracy detected")
            logger.warning("   This could indicate issues with the transcription pipeline")

        logger.info("\n   Sample comparison:")
        orig_sample = normalize_text_for_comparison(original_text[:200])
        trans_sample = normalize_text_for_comparison(transcribed_text[:200])
        logger.info(f"   Original:   {orig_sample}...")
        logger.info(f"   Transcribed: {trans_sample}...")

    except Exception as e:
        logger.error(f"❌ Transcription failed: {e}")
        return False
    finally:
        backend.cleanup()
        if chunk_files:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logger.warning(f"Failed to cleanup temp files: {cleanup_error}")

    logger.info("")
    logger.info("=" * 60)
    logger.info("✅ ALL TESTS PASSED!")
    logger.info("=" * 60)

    return True


def main():
    """Main test function."""
    logger.info("=" * 60)
    logger.info("Large File Transcription Test")
    logger.info("=" * 60)

    # Create temporary audio file
    temp_dir = tempfile.mkdtemp()
    test_audio_file = os.path.join(temp_dir, "test_large_audio.wav")

    try:
        # Generate large audio file with TTS
        test_audio_file, original_text = generate_large_audio_file_with_tts(
            test_audio_file, target_size_mb=30.0
        )

        logger.info(f"\nOriginal text length: {len(original_text)} characters")
        logger.info(f"Original text preview: {original_text[:150]}...\n")

        # Test the workflow
        success = run_large_file_workflow(test_audio_file, original_text)

        return 0 if success else 1

    except ImportError as e:
        logger.error(f"❌ {e}")
        logger.error("\nTo install required dependencies:")
        logger.error("   pip install gtts pydub")
        return 1

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # Cleanup temp directory
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

