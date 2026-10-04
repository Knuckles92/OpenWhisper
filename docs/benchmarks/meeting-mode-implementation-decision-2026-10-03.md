# Meeting Mode accuracy: real-meeting validation and implementation decision

## Evaluation basis

The audio tests use natural AMI research meetings and timed human word
annotations. The established ten-meeting live-draft benchmark covers 8.16
audio hours and 90,284 reference words; its shipping auto-model draft scored
28.83% five-minute-window tcWER. This follow-up evaluates five changes on
separate, explicitly identified subsets. A change in one metric cannot be
treated as a change in transcript WER unless the transcript was actually
rescored. AMI headset mixes are not a recording from this application's mic
and OS loopback setup.

## Results and decisions

| Priority | Idea | Real-meeting measurement | Decision |
|---:|---|---|---|
| 1 | Guided capture check and callback-stall recovery | Production signal monitor replayed on all ten complete meetings: **0/5,581** false no-signal results in annotated speech-rich five-second probes, and **0/146** false callback-stall alarms on zero-annotated-word probes. Injected mute and 1% gain faults were detected on **9,059/9,059** annotated speech windows. | **Implemented.** A host can explicitly check mic and system audio; a PortAudio stream that stays open but stops delivering blocks is restarted. No recovered words or actual device-switch success is claimed. |
| 2 | One-occurrence human correction by default | On cached drafts for three natural meetings, 17 hypothetical early one-word fixes were applied globally to later speech: **2** reduced later word errors, **13** increased them, and **2** tied. | **Implemented.** A selection anchored to one transcript passage defaults to a reversible one-occurrence edit; meeting-wide replacement requires an explicit choice. This proxy does not estimate how often real users make a correction or its realized WER gain. |
| 3 | Targeted ASR retry at hard chunk cuts | Four complete meetings unseen by the retry policy, **3.51 hours / 39,455 reference words**: CPU Whisper-base tcWER **39.72% → 39.48%** (−0.24 points; 94 fewer errors), with **0/4** meeting regressions. Retried **68/1,346** chunks and added **196.6 s** on top of **2,057.0 s** baseline decode. | Defer automatic replacement. Two meetings were nearly flat, and the configured auto/turbo model was not validated. |
| 4 | Audio-source speaker cues | A frozen four-headset mapping got **7,467/8,358 = 89.33%** speaker attribution at word midpoints on held-out IN1005; a synthetic mic/remote pair got **93.07%** accuracy, **87.06%** balanced accuracy. Parallel-source ASR on a separate 300-second natural slice worsened WER from **33.20%** on a mono mix to **35.90%**. | Defer. The source tests use headset tracks or synthetic mixes, not product mic/loopback or a matched comparison with the existing diarizer. |
| 5 | Automatic evidence gate for action and decision cards | On 26 cards from three natural meetings, the existing review flagged all **26**, including **20** previously audited as supported. A threshold chosen on two meetings kept all ten held-out cards, matched the default's **8/10** agreement with the prior audit, and missed both overstated cards. | Defer. No held-out gain, and audit labels are model judgments rather than human gold. |

The priority order is the **implementation decision**, weighing the measured
accuracy signal, expected effect on users, and readiness for the shipping
workflow. These rows have different
denominators and outcomes; their percentages are not additive.

### What the two implemented changes do

Capture health observes callback arrival and a lightweight 250 ms signal
window. A guided five-second check displays whether any signal arrived. It
does not claim to recognize speech or prove that the chosen device is the
meeting source: **142/146** windows with no annotated words still registered
audio signal in the replay, and a level-matched wrong source evaded the level
check in all injected trials. Quiet audio alone never raises a passive fault.
The dashboard now distinguishes an open device from one actually delivering
blocks; a host can repeat the check after changing devices.

One-occurrence corrections keep the raw ASR segment intact, anchor the edit
to one cited segment, and apply it consistently in repository reads, exports,
and the dashboard. A base-text fingerprint suspends the edit if a later
meeting-wide rule or transcript revision changes that segment, avoiding an
accidental shift to another occurrence. The save transaction rejects a stale
selection before recording the note, and agent transcript polish cannot bake
an active scoped edit into the raw text. Undo restores the raw reading. The
unconditional pre-meeting glossary prompt was not enabled: a term-dense pilot
raised exact target hits from **1/7 to 7/7** but increased WER from **42.52%
to 47.51%** across 301 reference words; a fixed-time English-pinned holdout
also regressed on IN1014 (**27.56% to 32.58%**).

### Test and measurement boundaries

- The capture replay is real meeting audio through the production signal
  monitor. Mutes, gain loss, and wrong-source swaps were injected; no real
  user's corrective action or resulting transcript improvement was measured.
  A delayed counter update from just before a guided check can also be
  credited to that check; strict post-click proof would require a
  server-acknowledged start time.
- The ASR retry, glossary and source-ASR tests use local CPU models or
  proxies. Their gains do not establish a benefit for the configured GPU
  auto/turbo model. The GPU was occupied during this run, so no competing
  application was interrupted to free it.
- Correction and evidence tests use historical drafts/cards and proxy labels.
  There are no timestamped human correction events or human-adjudicated final
  minutes in the available corpus.
- On the four full ASR meetings, the frozen rule scored IN1002 **43.44% →
  43.40%**, IN1012 **41.58% → 40.98%**, IN1001 **40.35% → 40.07%**, and IN1016
  **35.22% → 35.21%**. Its combined decode real-time factor rose from
  **0.163 to 0.178**. It preserves the durable live draft and measured a
  separate final transcript candidate; the application does not currently
  replace segments with that candidate.
- The final broad Meeting Mode Python suite had **1,462 passes, 4 skips, and
  1 deselection** under `venv`. The deselected test requires the declared
  `mcp` package absent from that environment; it **passed separately** under
  `.venv`, where `mcp` is installed. All **164** dashboard tests passed, the
  production dashboard build passed, and `git diff --check` found no
  whitespace errors.

The pilots and held-out detail are in
[the original ranking](meeting-mode-accuracy-ranking-2026-10-03.md),
[ASR notes](meeting-mode-asr-improvements-2026-10-03.md), and
[source/correction follow-up](meeting-accuracy-heldout-2026-10-03.md).
Scripts live in `benchmarks/meeting_mode/`; local downloaded audio and
generated result JSON files are under its ignored `data/` and `results/`
directories. The corpus reference is the [AMI corpus](https://groups.inf.ed.ac.uk/ami/corpus/).
