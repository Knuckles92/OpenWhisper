"""Remote engine: one OpenWhisper serves its speech engine to paired computers.

The host runs ``SpeechHost`` (host.py) behind a TLS WebSocket. A paired
client selects the Remote engine (transcriber/remote_backend.py), which sends
the same operations the local speech worker takes over stdin, so dictation,
incremental decoding and the live preview run on the host unchanged.
"""
