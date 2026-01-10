# Changelog

All notable changes to OpenWhisper will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-01-10

### Added
- **Real-time Streaming Transcription** - Live text preview while recording with draggable overlay
- **Caret Paste Indicator** - Visual feedback showing where text will be pasted
- **Dynamic Streaming Settings** - Reconfigure streaming behavior without restart
- **Enhanced Crash Diagnostics** - Improved logging with Qt message handling for debugging
- **Window Geometry Persistence** - App remembers size and position between sessions
- **Audio Input Device Selection** - Choose your preferred microphone from settings

### Fixed
- Window vertical resizing not working properly
- Numpad hotkeys now correctly distinguished from regular number keys(Thanks meonester)
- Crashes on workstations without GPU or unsupported compute configurations
- Various stability improvements for CPU-only systems

### Changed
- Optimized CUDA/GPU detection and fallback behavior
- Improved model benchmark tooling
- Updated Python 3.12 recommendation for best compatibility
