# Fresh auditable record-lock count verification

This is the final scratch-only rerun after the second shared-directory loss.
It is distinct from both recovered aggregate observations and the erased11:52 run.
Raw checkpoint JSON, logs, exit statuses, exact UTC bounds, script copies and
provenance are preserved here. Both cases completed successfully (exit0).

| Begin/abort pairs | Baseline retained locks | Candidate retained locks |
|---:|---:|---:|
|0|0|0|
|100|100|0|
|1,000|1,000|0|
|5,000|5,000|0|

Baseline169d254 and immutable candidate285d01d used real loopback pinned TLS,
production HostRecordStore, disposable settings/storage and fixture-only tokens.
The existing public WAV was required by common CLI validation but never uploaded;
no speech inference ran. Portable records_lock_churn.py was copied unchanged.
The helper validates source HEAD and rejects dirty tracked source files.

At5,000 pairs, baseline traced live allocations were1,574,189bytes; candidate
183,567bytes. These are supplemental tracemalloc readings, not RSS or isolated
attribution to the lock fix. Baseline map/key-tuple/lock lower bound747,544bytes
is not directly comparable to candidate48bytes, which counts only the weak-map
wrapper. The decisive result is5,000 retained lock entries versus zero.

Shared-machine soaks or recovery tasks may overlap. No latency/throughput claim
is made from recorded elapsed times. Counts verify absence of per-completed-record
registry retention in this candidate, not every concurrent lock lifecycle.
SHA256SUMS covers all evidence files except itself and interpreter cache files.
