#!/usr/bin/env python3

import os
import sys
import logging

from services.audio_processor import audio_processor
from config import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_chunking():
    test_file = "test_chunking_audio.wav"

    if not os.path.exists(test_file):
        logger.error(f"Test file not found: {test_file}")
        return False

    logger.info("Testing chunking functionality...")

    try:
        needs_splitting, file_size_mb = audio_processor.check_file_size(test_file)
        logger.info(f"File size check: {file_size_mb:.1f} MB, needs_splitting: {needs_splitting}")

        if not needs_splitting:
            logger.warning("File is not large enough to trigger chunking")
            return False

        logger.info("Starting audio splitting...")

        def progress_callback(message):
            logger.info(f"Progress: {message}")

        chunk_files = audio_processor.split_audio_file(test_file, progress_callback)

        if not chunk_files:
            logger.error("Failed to split audio file")
            return False

        logger.info(f"Successfully created {len(chunk_files)} chunks:")

        total_size = 0
        for i, chunk_file in enumerate(chunk_files):
            chunk_size_mb = os.path.getsize(chunk_file) / (1024 * 1024)
            total_size += chunk_size_mb
            logger.info(f"  Chunk {i+1}: {chunk_file} ({chunk_size_mb:.1f} MB)")

        logger.info(f"Total chunk size: {total_size:.1f} MB (original: {file_size_mb:.1f} MB)")

        logger.info("Testing transcription combination...")

        mock_transcriptions = [f"Mock transcription for chunk {i+1}" for i in range(len(chunk_files))]
        combined_text = audio_processor.combine_transcriptions(mock_transcriptions)

        logger.info(f"Combined {len(mock_transcriptions)} transcriptions into {len(combined_text)} characters")

        logger.info("Cleaning up temporary files...")
        audio_processor.cleanup_temp_files()

        logger.info("✅ Chunking test completed successfully!")
        return True

    except Exception as e:
        logger.error(f"Chunking test failed: {e}")
        audio_processor.cleanup_temp_files()
        return False

if __name__ == "__main__":
    success = test_chunking()
    sys.exit(0 if success else 1)
