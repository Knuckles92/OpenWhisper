# Asset and runtime provenance

No model/runtime binaries should be committed with this experiment.

- Production baseline: OpenWhisper `169d254`.
- NeMo-Speech.cpp source tag v0.1.0 resolves to `4f9676226f667d14608487df744f375db87127f8`.
- Official Linux x86-64 CPU archive: https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.1.0/nemo-speech-0.1.0-linux-x86_64-cpu.tar.gz
  SHA256 `0f74131d631ad2c694cf0ec53490866bb6461147959589a69fb6fc231944065b` (download verified against OpenWhisper manifest).
- Parakeet-v3 Q8 GGUF: https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/541d1f99c6b0c3cd0b11a95167540bb8edefd82b/parakeet-tdt-0.6b-v3.q8_0.gguf
  SHA256 `e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e` (download verified against OpenWhisper manifest).
- Whisper model: `Systran/faster-whisper-tiny.en`, exact snapshot `0d3d19a32d3338f10357c0889762bd8d64bbdeba`.
  https://huggingface.co/Systran/faster-whisper-tiny.en/tree/0d3d19a32d3338f10357c0889762bd8d64bbdeba
  model.bin content SHA256 from the downloaded Hub metadata: `1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a`.
  Download config.json, model.bin, tokenizer.json and vocabulary.txt from that snapshot, not floating main.
- Public speech: https://raw.githubusercontent.com/ggerganov/whisper.cpp/master/samples/jfk.wav
  SHA256 of the tested WAV `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e`. The URL floats; verify this hash for exact reproduction.

Example pinned model download using the official Hugging Face client in the app environment:

```sh
ORT_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_TELEMETRY=1 python -c 'from huggingface_hub import snapshot_download; snapshot_download("Systran/faster-whisper-tiny.en", revision="0d3d19a32d3338f10357c0889762bd8d64bbdeba", local_dir="/path/to/tiny.en", allow_patterns=["config.json","model.bin","tokenizer.json","vocabulary.txt"])'
sha256sum /path/to/tiny.en/model.bin /path/to/jfk.wav /path/to/parakeet-v3.gguf
```

Checksums establish exact artifacts, not GPU compatibility. Use the official CUDA runtime distribution, not this CPU binary, for later GPU native tests. Neither adapter in this directory is presented as a GPU benchmark.
