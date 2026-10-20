#!/usr/bin/env bash
set -euo pipefail
ROOT=/workspace/scratch/cc20469b78dc
HERE=$ROOT/fresh-lock-churn-final
source "$ROOT/host-scaling-runtime/safe-env.sh"
PY=$ROOT/host-scaling-runtime/venv/bin/python
AUDIO=$ROOT/host-batching-final/data/jfk.wav
{
 date -u +'%Y-%m-%dT%H:%M:%SZ'
 "$PY" --version
 git -C "$ROOT/openwhisper-host-scaling" rev-parse HEAD
 git -C "$ROOT/openwhisper-host-baseline" rev-parse HEAD
 git -C "$ROOT/openwhisper-host-soak-source" rev-parse HEAD
 sha256sum "$HERE"/scripts/*.py "$AUDIO"
} > "$HERE/provenance.txt"
for label in baseline candidate; do
 if [ "$label" = baseline ]; then source=$ROOT/openwhisper-host-baseline; rev=169d254; else source=$ROOT/openwhisper-host-soak-source; rev=285d01d; fi
 date -u +'%Y-%m-%dT%H:%M:%SZ' > "$HERE/$label-started-utc.txt"
 set +e
 "$PY" "$HERE/scripts/records_lock_churn.py" --source-root "$source" --expected-revision "$rev" --audio "$AUDIO" --output "$HERE/$label-checkpoints.json" > "$HERE/$label.log" 2>&1
 result=$?
 set -e
 echo "$result" > "$HERE/$label-exit-status.txt"
 date -u +'%Y-%m-%dT%H:%M:%SZ' > "$HERE/$label-finished-utc.txt"
 cat "$HERE/$label.log"
 [ "$result" -eq 0 ] || exit "$result"
done
