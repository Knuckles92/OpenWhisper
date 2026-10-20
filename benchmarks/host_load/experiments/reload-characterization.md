# Lifecycle characterization experiments

These diagnostics intentionally demonstrate **existing unsafe behavior**, not
contracts production should preserve. They are outside the normal `tests/`
regression suite. Run explicitly when investigating the engine lifecycle:

```
python -m pytest benchmarks/host_load/experiments/test_reload_characterization.py
```

They use production backend/controller code with a controlled interruptible
worker, not real ASR weights or a native-worker performance measurement. A future
atomic engine lease should change these outcomes; update the diagnostics rather
than preserving the hazard. See the Host Mode research report for interpretation.
