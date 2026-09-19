# Direct review of the finished meeting records

September 18, 2026. **My judgment after reading the outputs: the TypeSafe-assisted approach did not produce a meaningful overall improvement in the delivered meeting records.** It made some useful local edits, mostly to citations and qualification. It did not recover important omissions or reliably prevent the more serious interpretation and attribution mistakes.

I read all 12 saved records across IN1009, IN1005 and IN1007, including the production agent's summaries, cards, timelines and live notes, and all three human-reference transcripts. I also compared each assisted Gemini draft with its repaired version. This is my qualitative review as the current assistant, not a new Gemini scoring pass, an independent human review or a blinded experiment. I already knew the arms and earlier scores. I did not listen to the audio or inspect the rendered application.

The original judge was OpenRouter `google/gemini-3.8-flash`, the configured cleanup model with low reasoning. That same model also wrote the concise drafts. Its item audit checked human-reference windows around citations; it did not assess the full final product. Its attempted full-record coverage audit failed validation and supplied no usable completeness scores.

[All reviewed output text](benchmarks/typesafe-direct-review-outputs.md) is preserved in a readable appendix. The [review manifest](benchmarks/typesafe-direct-review-manifest.json) records source hashes, completion flags and exact repair changes. Raw experiment files remain unchanged.

## Which records would I use?

| Meeting | My preference for a concise record to edit and share | Effect of adding TypeSafe in these runs |
|---|---|---|
| IN1009: speaker localization and identity | Plain Gemini draft | No overall gain. The assisted draft adds a useful calibration/code reference but loses other useful context. Pi assistance makes several claims too definite. |
| IN1005: topic models and link propagation | Mixed; plain Gemini is the better starting point for content, assisted Gemini is more cautious about commitments | A local improvement in qualification, offset by missing substantive content. Current Pi contains valuable detail but was cancelled; assisted Pi delivered no record within the budget. |
| IN1007: speech research handoff | Plain Gemini draft, with an added unresolved-correctness warning | No overall gain. The assisted Gemini draft omits the major computational-cost warning. Both concise drafts omit an unresolved technical question important to the handoff. |

This preference is about the actual saved artifacts, not an established ranking of model families. Pi has substantially more room for narrative detail and produces additional sections; the concise writer is not a replacement for every production feature. I would edit every arm before treating it as an authoritative record.

## IN1009: useful summary, but technical advice becomes certainty

The plain Gemini draft captures the main useful distinctions: location is not identity; movement makes tracking harder; per-person features require separation; offline clustering software exists; thresholds vary with environment; and brief acknowledgments matter differently from raw speaking time. It correctly keeps the hoped-for software within five or six months as a suggestion, while retaining the actual paper-link commitment.

The assisted Gemini draft adds the single-channel calibration paper and code, which is helpful. But it omits the speaker's tentative software horizon and the distinction between the initial prototype's speech-quantity goal and later participation analysis. Its MATLAB statement is stronger than the plain draft's cautious phrasing. I prefer the plain draft as the compact foundation, adding the calibration resource.

The full Pi records provide more narrative coverage but need more corrections:

- Source at approximately **1129-1159 s** describes using MATLAB with some C code and says the speaker would not trust MATLAB for real-time work. Baseline Pi turns this into a risk that real-time processing "cannot use MATLAB"; assisted Pi adds a required "costly recode." That is a personal caution turned into a technical prohibition and an unstated cost.
- At **675-720 s**, a participant says separation is not very important for now, while the expert explains why basic separation is still needed. The assisted notes turn this into "The group agreed full separation is not an important feature." That is stronger consensus than the exchange establishes.
- The source says identifying a moved person is "probably the most complicated" task at **270-300 s**. The assisted risk calls it the "most complicated unsolved problem," adding "unsolved" and losing the qualification.
- Both Pi records rely on inferred "advisor" and "student" roles. The human transcript identifies the same speaker D discussing the software hope and promising paper links. The assisted record alternates those inferred role labels across sections. I would retain a speaker identifier or leave the owner unspecified rather than resolve roles from prose.

The actual TypeSafe-triggered repair of the assisted Gemini draft changed **one item's citations and no visible wording**. It did not cause the content differences from the independently generated plain draft, and it did not improve the reader-facing summary.

## IN1005: preserve the research contribution and the discussion's resolution

A useful final record must explain the PLSA-plus-links proposal, the agreed task-based evaluation rationale, the risk of flattening distributions, possible evaluation corpora and alpha tuning, and the closing idea about propagating information between Gaussian topic representations.

The plain Gemini draft preserves that last research idea; the assisted Gemini draft merely describes correlated topic models and the offer to share a paper. Losing the proposed next extension is material in a research meeting. Both concise drafts also omit the concrete positive result: the linked model identifies a French page despite its only extracted text being an English 404 message (**1823-1889 s**). That example explains why the technique is worth pursuing. Both also omit the speaker's competing work for the next month or two (**2452-2475 s**), useful context for follow-up timing.

The assisted Gemini draft is more cautious about the paper link, describing an offer instead of assigning an accepted action. The source says "if you want" at **2730-2744 s**, with brief acknowledgments from others. A follow-up may be expected, but I would not treat that as an unambiguous assigned task. This caution was already present in the assisted initial draft, so it cannot be credited to the repair step.

The current Pi record, despite cancellation, is the richest source of reusable detail: it retains the empirical example, the evaluation debate, the flattening problem and the Gaussian extension. But it needs reconciliation and editing:

- Its decision says task-based evaluation is accepted, yet its risk says there is "No accepted way to compare topic models." The source contains an explicit "I'm convinced" at **1697-1711 s** after distinguishing representation similarity from matching a single hard clustering. The final record should retain the limitation without presenting the earlier concern as wholly unresolved.
- It turns "I can regenerate that" about old results at **345-360 s** into an assigned regeneration task.
- "Ed App" is an ASR corruption of IDIAP in the corpus example.
- Its action to talk to David follows ASR at **2777-2792 s**, beyond the human annotation's ending. I cannot verify that action from the available reference, so I do not count it as disproved.

The assisted Pi record is entirely empty at its 240-second cancellation. That is a delivery failure in this run, not a lower factual-accuracy score for a completed record.

The Gemini repair makes some sensible distinctions: a directive to evaluate becomes a statement that the approach was judged fair, and Wikipedia receives an explicit suggested status. It changes five item texts and some citations, but does not recover the omitted research contribution, result example or scheduling context.

## IN1007: a handoff needs risks and correct people, not just fluent technical detail

The plain Gemini draft is a useful compact handoff. It names available code and literature, retains the HTK backend and paper/code follow-ups, gives the departing researcher's availability, and preserves the roughly 100-times-MFCC computational cost. The assisted draft gives a clearer inventory of Joel's existing pipeline, but loses that cost warning entirely.

More importantly, **both concise drafts omit the unresolved correctness question** that stopped earlier experiments from being published (**255-296 s**). A new owner needs to know which results still need validation. Both Pi records preserve this warning, which is a meaningful coverage advantage of the longer records. It should refer to those earlier experiments, rather than imply that all FDLP research was unpublished or invalid.

The Pi records also demonstrate the cost of losing speaker identity:

- Baseline notes say "The host will be away until late November." The reference at **1950-1966 s** says the departing researcher will still be present until November 24. Both the person and direction of availability are wrong.
- The assisted summary says Joel will stay reachable on Skype. The source at **2180-2189 s** is speaker D, the departing researcher, offering that availability to Joel. Other sections of the same output describe the relationship differently.
- Several technical actions are attributed to the generic "host" even though the reference distinguishes host A, recipient C, and departing researcher D. A polished paragraph masks a materially confused ownership model.

The assisted Gemini repair adds citations to five items and softens one sentence about subband tradeoffs. It leaves the summary unchanged and does not add either the missing correctness warning or cost warning. Those omissions existed before repair; they were not deleted by TypeSafe.

## Where the original scoring was too crude

The Gemini judge marked the assisted handoff-purpose item overstated because "TRAPs" was absent from its narrow reference window. TRAPs is clearly discussed in Joel's background at **120-165 s** and throughout the meeting. The distinction is insufficient local citation coverage versus false meeting content; those should not receive the same product-quality interpretation.

It also marked the plain Gemini QuickNet item contradicted because the human transcript includes "he is no good in Quicknet." Nearby speech calls Olivier an expert and credits his large-file fix (**422-450 s**). That awkward passage needs audio review before treating the item as a definite contradiction. My review does not certify the wording either; I would state the supported contribution and omit the disputed expertise label.

There are deeper issues that the narrow item scores did not capture: an unresolved concern persisting after agreement, important research ideas absent from the record, and the wrong person attached to availability. This is why supported-item percentages were not a satisfactory answer about the delivered product.

## What the repair loop actually accomplished

Across the three assisted Gemini records, before versus after repair:

- **All three summaries and topics were unchanged.**
- Item counts stayed **10, 9 and 11**; no items were added or removed.
- **Six of 30 item texts changed.** Ten items received citation changes; these groups overlap. One item moved from decisions to key points, and one gained suggested status.
- None of the important omissions identified above was restored.

I would describe that as small editorial and citation improvements, not an overall quality breakthrough. Differences between independently generated plain and assisted records cannot all be attributed to TypeSafe. In particular, IN1007 had no high-threshold streaming signals supplied to either assisted writer.

For this product, my priorities from the direct review are: preserve speaker attribution; reconcile concerns against later resolutions; require coverage of accepted outcomes, major research ideas and unresolved blockers; then verify narrow claims and citations. TypeSafe may help with the state comparisons, but that capability still needs to be tested directly. The current blanket verification step does not address the largest gaps in the final deliverable.
