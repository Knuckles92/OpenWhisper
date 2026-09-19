# Saved meeting outputs for direct review

Text extracted from the frozen September 18, 2026 experiment. Completion flags describe the saved run; cancelled records are partial. These are saved record fields, not screenshots of the rendered application. Evidence IDs remain in the source JSON.

## IN1009 / baseline

Completed: True; elapsed: 223.2 seconds.

**Topic:** From source location to speaker identity: audio features, clustering, and separation for the conversation-analysis table

**Summary:** Thesis project review for a table prototype that analyzes conversations. The discussion walks from source location to speaker identity: once the voice signal is separated, spectrum/pitch modeling plus measures such as rate of speech, dynamics and energy characterize a person. Location alone yields only speech segments and place-based clusters, which break down when people move, so the target flexible setup needs more; a former student's offline software groups small speech clusters by person. Separation is not a priority feature for now but is needed to extract per-person features because people talk over each other. The advisor will send paper links (localization/detection, activity decision, location clustering, single-channel calibration); the student will report on any usable software before finishing in roughly five-six months; then they go see Olivier.

### key_points

- Going from a source location to a person's identity requires using the separated signal and building a model of the person from the spectrum (FFT, magnitude transform). — item `it_926e8f8fcd78`
- Pitch is the base vocal frequency, and how the vocal tract, nose and head cavities shape it is almost unique, so pitch plus that shaping identifies a speaker; FFT is enough for this analysis. — item `it_3239903ab699`
- Rate of speech and voice dynamics/energy are personal too but vary with emotion, and can be used to quantify a person's involvement in the discussion. — item `it_15be69cc1a36`
- The individual measures are cheap; the expensive part is the decision (who it is, or whether the person moved), which needs statistical identity models. — item `it_a020cab0ef1a`
- Location alone only extracts speech segments and place-based clusters; it cannot say these are the same person and breaks down once someone moves (fine for roughly ten minutes). — item `it_359f8fbc936d`
- For the current fixed setting location would suffice, but because the target is a flexible place, more than location is needed. — item `it_1fda40b6c7a3`
- A former student's offline software groups small speech clusters into per-person clusters using measures like pitch from the spectrum; the proposed pipeline is location to small speech parts, then offline grouping into speech segments. — item `it_81f5a08a4674`
- Separation matters not for speech recognition itself but to extract per-person pitch, rate and energy, since people speak simultaneously and interrupt each other. — item `it_0d414e1ed068`
- Further directions include semantic context and emotion detection, and keyword spotting so the professor can be alerted when people talk about him. — item `it_7ab248f87e28`
- MATLAB suits offline analysis but cannot be trusted for online real-time work, so that code has to be written separately. — item `it_ae613249e653`

### decisions

- Separation is not a priority feature for now; it can be added later as a small step. — item `it_b46a05b261cd`
- For overlapping speech, the speakers must be separated first before voice recognition. — item `it_914900eb19aa`
- Rather than pointing to the papers, the advisor will send the links. — item `it_90e2a8ad77d3`
- Wrap up and go see Olivier next. — item `it_1cb12542ce45`

### action_items

- Advisor: send links to the three papers (localization/detection, activity decision, location clustering) plus the single-channel calibration paper with online code. — item `it_a5410e3790d4`
- Student: if a practically usable software version is built before finishing the thesis, tell the advisor (target roughly five-six months). — item `it_f08b6e2e0a45`

### risks

- A single fixed activity threshold may not hold across environments such as a cafeteria or library; the current calibration approach is only partial. (severity=medium) — item `it_c720149e3f43`
- Location-only identity breaks when a speaker moves or is replaced, so the flexible target setup needs more than location. (severity=medium) — item `it_2bd247905193`
- Very short utterances (one word per second, an isolated "yes") are hard: detected events are sums of small parts and need reprocessing to count as one intervention. (severity=medium) — item `it_b7cf66a3bc68`
- Real-time online processing cannot use MATLAB; the code has to be ported. (severity=low) — item `it_1cff544cf182`

### timeline

- Revisit the two opening questions, including clustering of the same people who move. (start_s=36.15) — item `it_9000ff6db987`
- From source location to personal identity: modeling the person from the spectrum. (start_s=55.57) — item `it_0c860a87d789`
- Pitch and vocal-tract shaping as personal identity cues; FFT as the tool. (start_s=105.55) — item `it_e7730409cb9b`
- Which measures matter (rate, dynamics, energy) and how cost shifts to the decision step. (start_s=232.25) — item `it_65366f858908`
- Location versus identity: clustering limits and why the flexible target needs more. (start_s=361.99) — item `it_024c8afa524e`
- Former student's offline clustering software and the two-level pipeline. (start_s=484.2) — item `it_8b0d9b712ef0`
- Separation is needed to extract per-person features despite talk-over. (start_s=581.34) — item `it_81ed266b7ea1`
- Context, emotion and keyword spotting as further directions. (start_s=739.32) — item `it_e1fbbb230f13`
- Advisor offers three papers and will send links. (start_s=817.78) — item `it_997b2338e1b7`
- Short-utterance handling and first-prototype focus on speech quantity. (start_s=961.32) — item `it_b798cba18cfe`
- MATLAB versus online real-time, then wrap up and go see Olivier. (start_s=1149.71) — item `it_e2f810df45bb`

### live_notes

- Going from a source location to a person's identity is a different problem: use the separated signal, transform the magnitude spectrum, and build a model of the person (FFT). (heading=From location to speaker identity; start_s=55.57) — item `it_7b472a09fe2b`
- Minutes: the student's questions are reviewed in order; the location-to-identity question is the one not yet covered. Identity work operates on the already separated signal rather than on raw location. (heading=From location to speaker identity; start_s=55.57) — item `it_580c163355d6`
- Minutes: pitch is discussed as a personal base frequency, shaped by the vocal tract and nasal/head cavities; the student knows the theory but not yet the implementation, and FFT is confirmed as sufficient. (heading=Pitch and vocal-tract cues; start_s=105.55) — item `it_26a1ba8de1db`
- Minutes: rate of speech and dynamics/energy are raised as personal but emotion-dependent measures that could quantify involvement. Cost discussion concludes the measures themselves are cheap, while the decision step is what gets expensive and needs statistical models. (heading=Measures, emotion and cost; start_s=232.25) — item `it_b916777c28f7`
- Minutes: location gives segments and place-clusters but not 'same person' and degrades over about ten minutes if people move. The current fixed setting could rely on location, but the flexible target cannot. A former student's software clusters small speech clusters by person from measures such as pitch; the plan is location to small parts, then offline grouping. (heading=Location vs. identity and offline clustering; start_s=361.99) — item `it_5f27691889ab`
- Minutes: separation is not needed for recognition as such but to get per-person pitch, rate and energy, because people overlap and interrupt; for simultaneous speech, separate before recognition. (heading=Why separation is needed; start_s=581.34) — item `it_0f2dc09d5811`
- Minutes: semantic context and emotion detection are mentioned as possible extensions outside the project scope, along with keyword spotting so the professor can be alerted when he is being discussed. (heading=Context, emotion and keyword spotting; start_s=739.32) — item `it_6a8ff5fccadf`
- Minutes: the advisor offers three papers — localization/detection, deciding whether somebody is active (fixed threshold versus adaptive across environments), and clustering of locations over time — plus a single-channel calibration paper whose code is online. The student asks for the links instead. (heading=Papers to send; start_s=817.78) — item `it_511941aa4bc4`
- Minutes: the work is currently MATLAB-based; MATLAB can mix with other code offline but is not trusted for online real-time, so the real-time part must be coded. The discussion closes and the group heads to see Olivier. (heading=MATLAB vs. real-time and wrap-up; start_s=1149.71) — item `it_b53f7a976c9a`

## IN1009 / assisted

Completed: True; elapsed: 164.0 seconds.

**Topic:** Using FFT-based voice measures and location clustering to identify and separate speakers for a conversation-analysis table prototype

**Summary:** The student presented their thesis project on a conversation-analysis table prototype. Picking up on earlier questions, the discussion focused on going from speaker location to identity. Location alone can only extract speech segments, not tell who is speaking, so FFT-based spectral analysis is needed to build a model of a person: pitch (vocal-cord fundamental frequency) plus how the mouth and nasal cavities transform it is nearly unique, and rate of speech, dynamics, and energy quantify identity, involvement, and emotion. Individual measures are cheap, but the hard part is the final decision — especially re-identifying someone who has moved, which needs statistical identity models over time; location clustering only suffices short-term or in static settings. The advisor noted people often talk over each other, so basic separation is needed to extract per-person features, and pointed to a former student's offline clustering software. Three reference papers (localization/detection, activity-decision, location clustering) plus single-channel calibration code were offered; a fixed activity threshold is unreliable across environments. The student will email the links and hopes to produce practically usable software before finishing the thesis in roughly five to six months; real-time online processing cannot use MATLAB and must be coded.

### key_points

- Location alone cannot identify a speaker — it only extracts speech segments; FFT-based spectral analysis is needed to build a model of the person. — item `it_6533b445494d`
- Pitch is the fundamental frequency of vocal-cord vibration, then transformed by the mouth and nasal cavities, making a person's voice nearly unique. — item `it_bf0fe28c9034`
- Pitch, rate of speech, and energy are personal measures that can identify speakers and quantify their involvement and emotion in the discussion. — item `it_07e5aaf1cb08`
- The individual measures are cheap, but the expensive part is making the final decision — who the person is, or whether they moved. — item `it_29322589b88b`
- Re-identifying someone who leaves and returns elsewhere is the most complicated case, requiring statistical identity models of the person built from the voice measures. — item `it_212b18dffe60`
- Location-based clustering is enough short-term (e.g., 10 minutes) or in static settings like this room, but a person may move or be replaced, so more than location is needed for a flexible deployment. — item `it_e898fe69a98e`
- A former student's software can cluster small speech segments by person offline, grouping them from voice measures such as pitch rather than from location. — item `it_fe96347cdba4`
- Even without full speech recognition, basic separation is needed because people often talk over and interrupt each other, to extract pitch and rate per person. — item `it_0b3eda5ddd30`
- A single fixed activity threshold is unreliable across environments like a cafeteria or library; automatic calibration is the better approach. — item `it_b19d50ef9641`
- Real-time online processing cannot use MATLAB, so the expensive parts must be recoded. — item `it_fdbfa1512c79`
- The student hopes to produce practically usable software before finishing their thesis in roughly five to six months. — item `it_cac11c4e8530`

### decisions

- Share the three reference papers by email/link rather than walking through them in the meeting. — item `it_ad683a25ac94`
- Full separation is not an important feature for now — basic separation is only required where per-person voice measures are needed. — item `it_34e368eb2c93`

### action_items

- Email the three reference papers (FFT localization/detection, activity decision, location clustering) and the single-channel calibration code. — item `it_4967f76c401d`
- Try to build a practically usable software version before finishing the thesis and let the team know if it works out. — item `it_34eee933278e`
- Continue to the next item by going to see Olivier. — item `it_a00c365bce27`

### risks

- A single fixed activity threshold breaks across different environments (cafeteria, library), limiting a one-size-fits-all deployment. (severity=medium) — item `it_135a4e2762a9`
- Re-identifying a person who moves is the most complicated unsolved problem and needs statistical identity models over time. (severity=high) — item `it_2a66fdf00d50`
- Real-time online processing cannot run in MATLAB, requiring a costly recode of the expensive components. (severity=medium) — item `it_acfb02000d70`

### timeline

- Revisit earlier questions: same people moving and clustering; the untold question is going from location to identity. (start_s=36.15) — item `it_f743cb7f1196`
- Explain that FFT-based spectral analysis is needed to build a model of a person from their voice. (start_s=55.57) — item `it_36dc99137045`
- Pitch, vocal tract, and voice dynamics discussed as personal identity and involvement measures. (start_s=105.55) — item `it_f1d0c66ad041`
- Cost and the hard case: decisions are expensive and re-identifying a moved person is hardest. (start_s=255.81) — item `it_addd76a8c58e`
- Location clustering works for short or static settings; a former student's software clusters speech by voice measures offline. (start_s=361.99) — item `it_b6ab8a813277`
- Separation debated for overlapping/interrupting speakers; it is not a priority now but needed for per-person features. (start_s=580.0) — item `it_0243b452e23d`
- Advisor offers three papers and single-channel calibration code; fixed thresholds flagged as environment-dependent. (start_s=817.78) — item `it_91db125af985`
- Closing: student will email links; MATLAB ruled out for real-time so code must be written, then the group leaves to see Olivier. (start_s=1064.2) — item `it_9cd17c630035`

### live_notes

- The session resumes on the earlier questions about same people moving and clustering. The question not yet covered is going from location to the identity of a person. Location can only extract speech segments and cluster a place where noise regularly comes from; it does not tell you these are the same person. FFT-based spectral analysis is needed to build a model of the person. (heading=Opening: location vs. identity, why FFT; start_s=36.15) — item `it_2d93b12cc6d1`
- Pitch is the fundamental frequency of vocal-cord vibration, then transformed through the mouth and nasal cavities — a near-unique signature. Together with rate of speech, voice dynamics, and energy, these measures can identify a speaker and quantify involvement and emotion in the discussion. They are individually cheap, but the expensive part is the final decision (who it is, or whether the person moved). (heading=Pitch, vocal tract, and voice measures; start_s=105.55) — item `it_3d246cb37332`
- For offline analysis, a former student wrote software that clusters small clusters of speech and groups them by person automatically from voice measures (pitch etc.), not from location. The plan: use location and lower-level analysis to get small speech parts, then group them at a higher level, possibly offline. The student hopes to have a practically usable version before finishing the thesis in roughly five to six months. (heading=Offline clustering software and thesis timeline; start_s=480) — item `it_92147f151292`
- Separation was debated: a speaker may be interested more in knowing that it is not the same people, or that someone new started talking. Separation matters because overlapping and interrupting speakers are common, and per-person pitch, rate, and energy cannot be extracted otherwise. The group agreed full separation is not an important feature for now. (heading=Separation and overlapping speech; start_s=580) — item `it_d26d6a2fb02d`
- Semantic context and keyword spotting were raised as further directions (e.g., detecting when someone is talked about, or an event trigger). The advisor offered three papers — sensor-based localization/detection, deciding whether somebody is active, and clustering locations over time — plus single-channel calibration code online. A single fixed activity threshold is unreliable across environments, and automatic calibration is preferable to manual calibration. (heading=Papers, calibration, and semantic context; start_s=739.32) — item `it_d197d1472fae`
- Short utterances (one word per second, or just "yes") may merge into a sum of small parts and need reprocessing; detecting such minimal participation matters once the system moves from quantity of speech to finer detail. The student will email the paper links rather than review them live. Real-time online work cannot use MATLAB, so the expensive parts must be coded. (heading=Short utterances, MATLAB, and next steps; start_s=961.32) — item `it_80ba661d664c`
- Discussion of the decisions layer: a person may be away only ten minutes and return to a different seat, so long-horizon identity relies on statistical models rather than a single location cluster. For the online case, the advisor suggests applying these measures in a light way, e.g., to give feedback. (heading=Decisions and the moving-person problem; start_s=255.81) — item `it_fcf32a2d2bb9`

## IN1009 / thin-gemini

Completed: True; elapsed: 12.2 seconds.

**Topic:** Audio Processing for Speaker Localization and Identification

**Summary:** The discussion covers extracting speaker identity and speech segments using audio features such as FFT, pitch, vocal tract modeling, and speech rate. The participants explore the necessity of speaker separation for speech recognition and conversation dynamics, practical constraints between online and offline processing, thresholding across diverse environments, and sharing relevant papers and codebase links.

### key_points

- Moving from location to speaker identity requires analyzing separated signals via spectrum transformations and FFT to extract pitch and vocal tract characteristics. (commitment_status=not_applicable) — item `it_0`
- Rate of speech varies over time and can be measured alongside pitch and energy to help detect emotion and level of involvement in discussions. (commitment_status=not_applicable) — item `it_1`
- The computationally heavier task is taking the final decision, particularly determining identity when a person moves away and returns to sit at a different location. (commitment_status=not_applicable) — item `it_2`
- Location alone only allows extracting speech segments, which is inadequate when tracking participants across flexible seating arrangements. (commitment_status=not_applicable) — item `it_3`
- Software left by a previous student can cluster small speech segments and group them automatically by person offline based on measures like pitch. (commitment_status=not_applicable) — item `it_4`
- The speaker hopes to produce practically usable software before completing their thesis in about five to six months. (commitment_status=suggested) — item `it_5`
- Basic speaker separation is required when participants speak simultaneously or interrupt each other, particularly before running voice recognition. (commitment_status=not_applicable) — item `it_6`
- Short conversational interjections such as occasional affirmations can indicate participation, though initial prototype versions will focus primarily on overall speech quantity. (commitment_status=not_applicable) — item `it_9`
- While research workflows utilize MATLAB with occasional C implementations for intensive sections, MATLAB is not considered reliable for real-time online deployments. (commitment_status=not_applicable) — item `it_10`

### action_items

- Send the links to the three papers covering sector-based localization, activity thresholding, and clustering. (commitment_status=accepted) — item `it_8`

### risks

- Using a single fixed threshold to detect whether someone is active can fail across varying acoustic environments such as cafeterias or libraries. (commitment_status=not_applicable) — item `it_7`

## IN1009 / thin-gemini-assisted

Completed: True; elapsed: 22.6 seconds.

**Topic:** Speech Processing, Speaker Identification, and System Implementation

**Summary:** The discussion centered on methods for separating speakers and mapping sound localization to individual identities using acoustic features such as pitch, vocal tract characteristics, and speech rate. Participants examined the necessity of separation for speech recognition and conversation analysis, practical complexities like varying acoustic environments and movement, and technical implementation choices between MATLAB and low-level code.

### key_points

- Location data alone only yields speech segments from particular directions; identifying unique individuals requires analyzing separated signals via features like pitch and vocal tract transformations extracted from FFT spectra. (commitment_status=not_applicable) — item `it_0`
- Rate of speech and vocal dynamics vary individually and with emotion, providing measurable ways to quantify involvement in discussions. (commitment_status=not_applicable) — item `it_1`
- A past student's software is available to group short segments of speech into speaker clusters using pitch and spectral measurements. (commitment_status=not_applicable) — item `it_3`
- Signal separation is essential prior to voice recognition because participants frequently talk concurrently and interrupt one another. (commitment_status=not_applicable) — item `it_4`
- A paper and associated online code are available detailing single-channel calibration to assist microphone level adjustment. (commitment_status=not_applicable) — item `it_6`
- Brief vocal interjections like 'yes' register as distinct small intervals that can be grouped into interventions to indicate conversational participation. (commitment_status=not_applicable) — item `it_7`
- While MATLAB is used for rapid development and testing, real-time online processing typically requires coding lower-level implementations such as C. (commitment_status=not_applicable) — item `it_9`

### action_items

- Send the links to the three relevant papers covering sector-based localization, activity decision-making, and clustering. (commitment_status=accepted) — item `it_8`

### risks

- Tracking identities becomes complex if speakers move or leave and return to different seating positions, making simple spatial clustering insufficient over time. (commitment_status=not_applicable) — item `it_2`
- Using a fixed single threshold for activity detection fails across differing acoustic environments like cafeterias or libraries, necessitating adaptive or automatic calibration. (commitment_status=not_applicable) — item `it_5`

## IN1005 / baseline

Completed: False; elapsed: 240.0 seconds.

**Topic:** Combining PLSA topic models with PageRank link information to classify text-poor web pages, and how to evaluate the combined model

**Summary:** The speaker presents a project (related to but separate from Ripple) that combines PLSA topic models with PageRank-style link information so pages with little text can be classified from the pages they link to; a per-page alpha weights text against link evidence. The algorithm produces a second topic vector per page, and the meeting debates how to evaluate it: likelihood/perplexity is weak, Pedro proposes using a web directory's categories as ground truth (train unsupervised, then classify unlabeled pages), and the group worries directory categories may not match the learned aspects. An ICML marble-clustering analogy leads the speaker to accept pairwise-similarity evaluation as fair. On a ~2000-page crawl from the Ed App home page the link model correctly called a French page 'French' even though its only words were English, though link spreading flattens topics by averaging across linked pages. The speaker closes by presenting David Blei's correlated topic models and proposing to apply link spreading to Gaussian topic representations.

### key_points

- Project goal: combine PLSA topic models with PageRank-style link information so pages with little or no text can be classified from the pages they link to. — item `it_227b8837fc13`
- Algorithm: plug PageRank into PLSA as an iterative algorithm, where a per-page alpha weights how much to trust the page's own text versus the links pointing to it. — item `it_e02c26ee0c71`
- The method yields a second topic vector per page (PLSA vs PLSA+links), leaving no clear way to tell which representation is better. — item `it_148d0f641121`
- Pedro's evaluation proposal: use a web directory (Yahoo/DMOZ) as ground truth categories; train both models unsupervised, then classify unlabeled pages by distribution similarity and measure agreement with the directory. — item `it_8a3c86473db5`
- Concern raised: directory categories may not match learned aspects, e.g. PLSA may separate English from French when the directory says good vs bad, so proximity to the directory only measures fit to that task. — item `it_8810001e2b81`
- Marbles/ICML analogy: people cluster marbles differently but agree on pairwise similarity, so topic models should be evaluated on pairwise similarity rather than matching any single hard clustering. — item `it_4038a3c6865f`
- Experimental result: a ~2000-page crawl from the Ed App home page separated clear English and French topics; the link model correctly called a French page 'French' even though its only words were English 404 text, while PLSA called it English. — item `it_a437016b43a8`
- Downside of link spreading: averaging each page's distribution with linked pages flattens and blurs topics (Slashdot example), losing page-specific information, so alpha may need to be set per page. — item `it_42e6b66a8674`
- Correlated topic models (Blei, NIPS): LDA/PLSA lose expressiveness past roughly 100-200 topics because topics repel each other; CTM replaces the Dirichlet with a Gaussian mapped to the simplex, making topic-distribution closeness measurable. — item `it_712363624a91`
- Speaker's new idea: apply link spreading to the Gaussian representations instead (linked pages have similar mean and covariance), which might be a better model than PLSA+links. — item `it_6642e55b6d5f`

### decisions

- Agreed that evaluating the models against a concrete task (e.g. predicting a page's directory category via pairwise distribution similarity) is a fair benchmark, while acknowledging it shows task fit rather than a universally 'better' clustering. — item `it_70ef43ffcc2a`
- The project is parked for now: the speaker is stuck on other work for the next month or two before returning to it. — item `it_090a07623ee9`

### action_items

- Send the group the link to the correlated topic model (Blei) paper. — item `it_9a5886dcb68c`
- Talk to David to get a month or two to work on the link/topic-model project again. — item `it_0a97cef24541`
- Regenerate the earlier web-page experiment results that were only stored in temp 2. — item `it_29657f7518e3`

### risks

- No accepted way to compare topic models: directory categories are just one subjective clustering and may not align with the learned aspects. (severity=medium) — item `it_6999cbbc3cac`
- Link spreading can flatten and distort a page's topic distribution (Slashdot), losing information about what that page is actually about. (severity=medium) — item `it_c1ea973bb5c1`
- PageRank-style methods work best on small-world networks; small crawls are not small-world, so results depend heavily on corpus size. (severity=medium) — item `it_a4d14056609b`
- PLSA/LDA stop being expressive past roughly 100-200 topics, after which added topics are effectively garbage. (severity=low) — item `it_2697bccbe6f5`

### timeline

- David raises using topic models over linked documents/networks; speaker frames the project (related to but separate from Ripple). (start_s=52.46) — item `it_df3ef6e3bb8b`
- Problem stated: web pages carry very little text, so infer their topic from the pages they link to. (start_s=91.01) — item `it_c0bfacd4134b`
- Proposal: combine PageRank link authority with PLSA topic models. (start_s=134.9) — item `it_835e0081cd96`
- The algorithm produces a second topic vector per page, exposing the evaluation problem. (start_s=349.2) — item `it_22900a4e26ed`
- Pedro's car example and directory-based evaluation proposal; debate over whether directory clustering is a fair ground truth. (start_s=529.3) — item `it_300319bb2ed3`
- ICML marble-clustering analogy leads to pairwise-similarity evaluation; speaker becomes convinced the setup is fair. (start_s=1340.75) — item `it_424a801f53f4`
- Results on a ~2000-page Ed App crawl: English/French topics separated; link model beats PLSA on a French page whose only words were English. (start_s=1710.3) — item `it_36521ffdba20`
- Speaker presents correlated topic models (Blei), proposes Gaussian link spreading, and wraps up agreeing to share the link and push David for time. (start_s=2477.86) — item `it_ecc1aa918915`

### live_notes

- David raises a line of work the speaker had been pursuing: applying topic models to documents that are linked into a network. The speaker frames it as related to but separate from Ripple, and notes he has not touched it for a couple of months. (heading=Framing: topic models over linked documents; start_s=52.46) — item `it_dce8f5003d17`
- Core problem: web pages often have very little text, but their topic can be inferred from the text of the pages they link to. The goal is to combine topic models for text-rich pages with link information for text-poor pages. (heading=Problem: text-poor web pages; start_s=91.01) — item `it_34028572cc58`
- Proposal: bring together PLSA topic distributions with PageRank link authority. PageRank spreads authority along links; the idea is that a page's topic is partly from its own text and partly from what the linking pages say it is about, weighted by alpha. (heading=Combining PageRank with PLSA; start_s=134.9) — item `it_5fcb81c7c061`
- Algorithm detail: a PageRank-style sum over incoming links is folded into the PLSA update, giving an iterative algorithm. PLSA is trained first, then the link-spreading step is initialized from it and iterated to convergence, producing a second topic vector per page (PLSA vs PLSA+links). The per-page alpha controls trust in text versus links. (heading=The algorithm; start_s=182.23) — item `it_4c91761fdd88`
- Evaluation is the hard part: with two topic vectors per page there is no quantitative way to say which is better. PLSA literature tends to be qualitative (look at the clusters) or uses likelihood, and perplexity is essentially the same. Pedro's car example (a two-word 'Lotus Elise' page) shows why text-only rules fail. (heading=How do we evaluate it?; start_s=349.2) — item `it_8a2c2385e066`
- Pedro proposes using a web directory (Yahoo/DMOZ) as ground-truth categories: train both models unsupervised over labeled and unlabeled pages, then classify unlabeled pages by distribution similarity and score agreement with the directory, possibly via an SVM on the labeled documents. Debate follows: directory categories may not match learned aspects (e.g. PLSA separating English/French when the directory says good/bad), so the benchmark measures fit to a task, not a universal 'better' clustering. (heading=Pedro's directory benchmark and the fairness debate; start_s=529.3) — item `it_657d280c8282`
- An ICML marble-clustering analogy resolves the debate: people cluster marbles differently but agree strongly on pairwise similarity. So instead of checking a hard clustering, compare the models' pairwise distribution similarities against human/ground-truth pairs and average over many documents. The speaker accepts this: 'I'm convinced.' (heading=Marbles analogy and pairwise evaluation; start_s=1340.75) — item `it_6c695cd8e652`
- Experiment: a ~2000-page crawl from the Ed App home page gave enough intra-cluster links to run the algorithm. PLSA split clearly into an English and a French topic. The link model won a telling case: a French page whose only captured words were an English '404 this page cannot be found' message was correctly called French because it linked to French sites (and .fr), whereas PLSA called it English. (heading=Results: Ed App crawl, English vs French; start_s=1710.3) — item `it_4b9d444fd217`
- Two counterweights to the link model. First, averaging over all linked pages flattens every topic distribution (Slashdot being linked from everywhere loses its specific topic), so alpha probably needs to be set per page from heuristics (e.g. amount of text) or learned from a task. Second, PageRank-style methods need small-world networks, so they improve with larger crawls; Wikipedia was suggested as a well-structured, link-rich evaluation corpus, with citation archives as another option. (heading=Flattening downside, alpha per page, corpus choice; start_s=1929.6) — item `it_7548c71d618b`

## IN1005 / assisted

Completed: False; elapsed: 240.0 seconds.

**Topic:**

**Summary:** (empty)

## IN1005 / thin-gemini

Completed: True; elapsed: 11.9 seconds.

**Topic:** Evaluating Topic Models with Hyperlink Structure and Correlated Topic Models

**Summary:** The discussion focuses on an algorithm that combines probabilistic latent semantic analysis (PLSA) with PageRank to model linked documents with sparse text. The participants examine strategies for evaluating this model, proposing the use of web directory categories (e.g., Yahoo or DMOZ) or Wikipedia corpora as classification tasks rather than relying purely on perplexity or manual inspection. Additionally, correlated topic models (CTM) are explored as an alternative approach to model similarity across topic distributions.

### key_points

- A method was devised to combine PLSA topic distributions with PageRank link structure to deduce topics for web pages with little or no text. (commitment_status=not_applicable) — item `it_0`
- The algorithm initializes topic distributions using PLSA and iteratively updates them via link propagation weighted by an alpha parameter balancing text and link information. (commitment_status=not_applicable) — item `it_1`
- Evaluating latent topic models using directory structures (like Yahoo or DMOZ) was suggested, where known category labels serve as ground truth for matching document topic distributions. (commitment_status=suggested) — item `it_3`
- Concern was raised that human categorization in directories may not align with unsupervised latent clusters, though the team concurred it functions adequately as a relative benchmark. (commitment_status=not_applicable) — item `it_4`
- Alpha parameter selection could be set via document-level heuristics (such as text length or image count) or learned by optimizing evaluation task performance. (commitment_status=suggested) — item `it_5`
- Wikipedia was proposed as a closed, well-structured dataset for evaluation because internal article links can be extracted while excluding navigation or directory links. (commitment_status=suggested) — item `it_6`
- Correlated topic models (CTM) replace the Dirichlet prior with a Gaussian distribution, allowing correlations between topics to be modeled and mapped onto the simplex. (commitment_status=not_applicable) — item `it_7`
- It was suggested that link smoothing could be applied across Gaussian representations from correlated topic models by matching means and covariances rather than PLSA distributions. (commitment_status=suggested) — item `it_8`

### action_items

- Send the link for the correlated topic models research to the team. (commitment_status=accepted) — item `it_9`

### risks

- Because links diffuse across the broader network, the iterative link model can cause topic distributions across linked pages to flatten and become overly uncertain. (commitment_status=not_applicable) — item `it_2`

## IN1005 / thin-gemini-assisted

Completed: True; elapsed: 22.4 seconds.

**Topic:** Topic Modeling Combined with PageRank and Model Evaluation

**Summary:** The discussion focuses on an algorithm that integrates PageRank into PLSA to handle web pages with sparse text by leveraging hyperlink network structure. The participants explore evaluation methodologies, agreeing on testing topic distributions via directory classification benchmarks (e.g., Yahoo, DMOZ, or Wikipedia). They also examine challenges such as distribution flattening and parameter weighting, concluding with an overview of correlated topic models using Gaussian distributions.

### key_points

- Applying PLSA directly to web pages is problematic because many pages have minimal text, despite their context being inferable from linked pages. (commitment_status=not_applicable) — item `it_0`
- The proposed approach initializes topic distributions with PLSA and iteratively updates them using a PageRank-style propagation over network links, balanced by an alpha weighting parameter. (commitment_status=not_applicable) — item `it_1`
- Evaluating topic representations purely through likelihood or perplexity was questioned because a model can fit text well while failing to capture desired semantic distinctions. (commitment_status=not_applicable) — item `it_2`
- Participants agreed that evaluating the models against a real-world task, such as predicting human directory categories (e.g., Yahoo or DMOZ) using distribution similarity, provides a fair benchmark. (commitment_status=not_applicable) — item `it_3`
- The alpha parameter can be tuned per page based on heuristics such as text length or optimized through performance on the evaluation task. (commitment_status=not_applicable) — item `it_5`
- Wikipedia was suggested as a suitable test corpus because of its dense internal link network and well-defined category structure. (commitment_status=suggested) — item `it_6`
- Correlated topic models replace the Dirichlet distribution in LDA with a Gaussian space mapped down to the topic simplex, enabling correlation and similarity comparisons between topic distributions. (commitment_status=not_applicable) — item `it_7`
- Offer made to share the link to the correlated topic models paper with the team. (commitment_status=suggested) — item `it_8`

### risks

- Propagating topic weights across interconnected pages can cause topic distributions to flatten and become overly uncertain, especially when pages are linked to by diverse, broad sites. (commitment_status=not_applicable) — item `it_4`

## IN1007 / baseline

Completed: True; elapsed: 208.7 seconds.

**Topic:** Continuity of long-temporal-window speech features: TRAPS, FDLP, and Hilbert-envelope modeling

**Summary:** Knowledge-transfer meeting on long-temporal-window speech features, framed explicitly to make sure the TRAPS/FDLP work does not die as its original contributors leave. The host walks Joel through the history of FDLP (Mario's ~50 ms-segment work, the MATLAB-to-C++ rewrite, unpublished knob-tuning experiments) and inventories the front ends in use: MFCC/PLP, TRAPS with various lengths and warping, tandem, and FDLP. FDLP models the Hilbert envelope of subbands (about 1 s frames, DCT, independent per-band processing) and is being applied to speech coding using 15 bands, while literature suggests ~12 may be optimal; carrier modeling degrades for broader bands and computation is far heavier than MFCC. The host points Joel to Mario's and Les Atlas's papers and commits to sending papers and code pointers.

### key_points

- Meeting is framed as a continuity/knowledge-transfer session so the long-temporal feature work survives; Joel is expected to pick up keyword spotting and related ideas rather than the host doing all the talking. — item `it_de42d9f05d83`
- TRAPS features use long temporal windows — from a single frame up to about one second — and DCT is applied to the TRAPS trajectory itself. — item `it_e0b995c7f99f`
- FDLP splits speech into ~1 s frames, applies DCT, divides the spectrum into overlapping subbands, and processes each band independently. — item `it_15401aac8a76`
- Each subband is decomposed into a Hilbert envelope (modeled by LPC/autoregressive prediction) and a Hilbert carrier/excitation; narrow bands give near-sinusoidal carriers while broader bands become noise-like. — item `it_e933f8f06a82`
- Current speech-coding setup uses 15 frequency bands as a reasonable setting (could go to ~12), while papers suggest ~12 bands may be near-optimal. — item `it_f79b16f59774`
- Spectral transform prediction (STLP), an old technique from the speaker's 1983 work, lets the envelope model balance fitting peaks versus dips instead of fitting peaks only. — item `it_37075592cc99`
- FDLP is roughly 100x more expensive than computing MFCCs (huge FFT/DCT, 10 ms stepping, no optimization), though optimization and hardware cut backend recognizer turnaround from ~1.5 hours to 11–18 minutes. — item `it_f54116fd2f87`
- Recommended reading: Mario's ~3 papers (SAPA, Interspeech, ASRU) plus IDIAP reports, and Les Atlas's rigorous DCT-based coding work. — item `it_cb183c9cccac`

### decisions

- Continue and hand over the FDLP/TRAPS research thread so the work carries on, with Joel taking it forward and the host staying available for support. — item `it_3630530b41b7`

### action_items

- Send Joel the papers related to FDLP and point him to all the related code and file locations. — item `it_2fb07faf2927`
- Joel to take the full feature-extraction + MLP + backend setup and run it end-to-end once, then share what he has read. — item `it_94310f645017`

### risks

- The earlier FDLP work was never published because an essential correctness question was left unresolved, so the prior results remain unverified. (severity=medium) — item `it_0a5cf3afa6bd`
- Envelope/carrier modeling degrades as bands get broader: the carrier is no longer cosine-like, and using noise everywhere yields whispered-sounding speech. (severity=medium) — item `it_9fc561a338a8`
- The work is not yet published or patented; there is a confidentiality concern about disclosing it before the funding organization can pursue IP protection. (severity=low) — item `it_15175146d5a8`

### timeline

- Frame the meeting: the host will listen and Joel will ask questions, to ensure continuity of the work. (start_s=21.5) — item `it_e32b66411656`
- Joel describes his background reading on long temporal features, TRAPS, and tandem systems. (start_s=113.82) — item `it_09179d18e7a5`
- Origin story of FDLP: Mario's early work with ~50 ms segments and the realization it could estimate spectral energies. (start_s=160.35) — item `it_42a48f2b21ad`
- MATLAB-to-C++ rewrite and knob-tuning experiments that were ultimately never published. (start_s=223.09) — item `it_82c7073800f4`
- Inventory of front ends: MFCC/PLP, TRAPS with various lengths and warping, tandem, then FDLP in more depth. (start_s=525.45) — item `it_b49977470c0e`
- FDLP speech-coding detail: 1 s frames, DCT, subbands, then Hilbert envelope and carrier decomposition, with the whiteboard. (start_s=723.5) — item `it_04c03d938bc4`
- Band-count tradeoff: 15 bands works well, ~12 may be optimal; fewer bands complicate the carrier/excitation. (start_s=1344.44) — item `it_473ffba1cc7c`
- Literature pointers: Mario's papers/reports and Les Atlas's work; Mario's MATLAB files and results table are still available. (start_s=1512.74) — item `it_d6c5baaa0717`
- Handover commitments: send papers, share code locations, and keep the thread alive; wrap-up and logistics. (start_s=1870.61) — item `it_ce8a27ac63e4`

### live_notes

- The meeting is being recorded to keep a record, and is framed as a continuity/knowledge-transfer session: the host will listen, Joel will ask questions. The aim is to make sure the long-temporal feature work is not forgotten and that others can build on it; Joel is expected to pick up keyword spotting and related ideas. (heading=Framing & purpose; start_s=21.5) — item `it_ff44ba58b346`
- Joel reports he started from features using long temporal frames, TRAPS and tandem, a paper on fitting an AR model to the DCT of the signal, and is gradually getting used to the setup. (heading=Joel's background reading; start_s=113.82) — item `it_141b6e7b8c8a`
- FDLP traces back to Mario's work (early segments ~50 ms), which was presented at a meeting and recognized as useful for estimating spectral energies. Mario programmed in MATLAB for about half a year; the host later rewrote it in C++. Knob-tuning over compression factors was done but never published because an essential correctness question was unresolved, and computation was ~100x heavier than MFCCs. (heading=History of FDLP; start_s=160.35) — item `it_3d1342d4a9b2`
- Joel already has the feature-extraction part producing phoneme posteriors and only needs the (easy) HTK backend; he will take the whole setup and run it once. The front ends on hand: MFCC/PLP for comparison, TRAPS with various lengths (one frame up to ~1 s), DCT applied to the TRAPS trajectory, warping (keeping central points, subsampling boundaries), ghost TRAPS, tandem (PLP/MFCC + MLP context), and FDLP. (heading=Feature front-end inventory; start_s=525.45) — item `it_69d2d046bb44`
- FDLP application: split speech into ~1 s frames, DCT, divide into overlapping subbands, and process each band independently. It is used for speech coding (encode to few bits, decode back with reasonable quality), splitting into Hilbert envelope and Hilbert carrier/excitation. Compression preserves dips as well as peaks; the predictor fits a compressed signal that can be expanded back. Currently 15 bands, ~12 may be optimal. (heading=FDLP & speech coding; start_s=723.5) — item `it_fc1ae7809a96`
- Whiteboard walk-through comparing time-domain LPC synthesis (source + 1/A(z) filter) to the frequency-domain picture: each subband is a time signal whose DCT yields a Hilbert envelope and a carrier close to a cosine. Narrow bands give near-sinusoidal carriers; broader bands look less cosine-like, and noise excitation everywhere sounds like whispered speech. The speaker realized the STLP old technique (1983) applies here to balance peak vs dip fitting. (heading=Hilbert envelope on the whiteboard; start_s=1092) — item `it_ea8981d4d11e`
- Advice on reading: Mario's ~3 papers (SAPA, Interspeech, ASRU) and IDIAP reports; Les Atlas's work on DCT-domain coding (rigorous, Stanford). Mario's MATLAB implementations, files, and a large Excel results table are still available locally. (heading=Literature pointers; start_s=1512.74) — item `it_326c56cc9680`
- The host commits to sending Joel the related papers and pointing him to all the FDLP code; the location of files will be stated on the recording so Joel can rewind and browse. Joel will run the setup and may come back with questions, and the host remains reachable on Skype. (heading=Handover commitments; start_s=1870.61) — item `it_1935b166909c`
- Time check (~half hour, fine) and wrap-up. The host will be away until late November and then teaching a combined speech-processing/perception course, expecting at least four students. Closing remarks thank the participants and the session ends. (heading=Logistics & wrap-up; start_s=1947.45) — item `it_256bf5501bd2`

## IN1007 / assisted

Completed: False; elapsed: 240.0 seconds.

**Topic:** Handing the FDLP / temporal-envelope feature work to Joel

**Summary:** A senior researcher briefs Joel on the lab's FDLP (frequency-domain linear prediction) line of work so it can carry on after its main contributors leave. FDLP fits an AR model to the DCT of the signal to estimate spectral energies more elegantly than frame-by-frame estimation; it grew from Mario's work on AR-modeling the DCT, then C++ rewrites and TRAPS variants. The lab holds feature extraction (phoneme posteriors), a tandem MLP, MFCC/PLP, TRAPS of various lengths, DCT-on-TRAPS (Gaussian-like filtering), warping, and FDLP for speech coding. FDLP is ~100x costlier than MFCC and was never published because of an unresolved question. For coding they compare Hilbert envelope and carrier, split the spectrum into ~15 sub-bands (12 reportedly optimal) with compression ~0.1, and flag the non-cosine carrier as the main open problem. Hermansky's 1983 spectral-transform work balances fitting peaks vs dips. The host will send papers and point Joel to the FDLP code; Joel will run the full setup once and stay reachable on Skype.

### key_points

- The meeting's purpose is to hand off the temporal-envelope / FDLP work so it does not die; Joel is the main hope to pick up keyword spotting and envelope-modeling ideas. — item `it_e94093b41e43`
- FDLP fits an AR model to the DCT of the signal to estimate spectral energies; the older approach first estimated frame-by-frame at 10 ms and then picked up the trajectory, which is inelegant. — item `it_daa8d8d5a973`
- FDLP is computationally heavy — on the order of 100x more than MFCC — because of huge DCT/FFT everywhere; no optimization was done and it stepped at 10 ms to stay close to TRAPS. — item `it_c7fcb26510ad`
- The FDLP results with tuned compression/length knobs were never published because an essential question remained unresolved and correctness could not be confirmed at the time. — item `it_a16aba8e0474`
- Existing front ends in the lab: MFCC/PLP (for comparison), TRAPS with various lengths, DCT applied to the TRAPS trajectory (Gaussian-like filtering), and warping/resampling variants with more resolution at the center. — item `it_c5308a3faea1`
- Tandem system: PLP or MFCC coefficients are fed into an MLP with some context; the setup exists but has not been experimented with extensively, and Hemant's tandem results are largely in his thesis. — item `it_a2d0c03fa04b`
- FDLP is used for speech coding: roughly 1-second segments, DCT, then sub-band processing into a Hilbert envelope and carrier (residual), which must both be encoded to reconstruct reasonable-quality speech. — item `it_81085261628b`
- Coding uses ~15 frequency bands (12 reportedly optimal); narrower bands make the excitation/carrier approach a sinusoid, while wider bands behave more like noise and sound worse at low frequencies. — item `it_e49d92c4a6cb`
- Spectral transform (STLP, from Hermansky's 1983 paper) lets the predictor balance fitting peaks vs dips, which matters when modeling the Hilbert envelope rather than only spectral peaks. — item `it_9f5ee1240479`
- Mario's files remain in the lab: all the MATLAB implementations and a big Excel table of parameters, compression factors, and speech-recognition results. — item `it_7563c8a1f150`
- The backend recognizer now runs in about 11–15 minutes where it previously took around an hour and a half, thanks partly to hardware and optimization. — item `it_b4b6c0a2b84e`
- Les Atlas's papers are recommended as rigorous, error-free reading on DCT-based coding and temporal-envelope modeling over the past several years. — item `it_c34c41612966`

### decisions

- Joel will take over and continue playing with the FDLP setup so the work carries on rather than dying. — item `it_b64dcc61a7b6`
- The host will send Joel the relevant papers and point him to all the FDLP code locations. — item `it_f57bc43abbee`
- The work stays confidential until published, and any funding sponsor (e.g. Colcom) must get a chance to patent it first. — item `it_5f96e24be15b`

### action_items

- Send Joel the related papers (Mario's Sapa/Interspeech/ASRU papers and related work). — item `it_778e729927de`
- Point Joel to all the FDLP code and Mario's MATLAB implementations and results table. — item `it_be545a183e14`
- Joel will take the entire existing setup and run it once end-to-end. — item `it_90b78c401877`
- Joel can reach the host on Skype for FDLP questions until around May. — item `it_9c983a96719f`

### risks

- FDLP is very inefficient (~100x MFCC) with no optimization, limiting experiments and practical use. (severity=medium) — item `it_103c08d3fd0b`
- An unresolved essential question kept the FDLP results unpublished and unvalidated, so Joel may inherit uncertain ground. (severity=medium) — item `it_a47ff0e241a1`
- The sub-band carrier is not a clean cosine as bands widen — the biggest current problem in the coding model. (severity=high) — item `it_e29f92dc5216`
- Expertise is draining away (Chifang, Barry, and Mario gone), leaving few people who know how to develop quicknet/MLP tools. (severity=medium) — item `it_276cfe346fc5`

### timeline

- Framing: the group meets to transfer the temporal-envelope / FDLP work so it carries on; Joel is to lead and ask questions. (start_s=21.5) — item `it_6f397fbe874c`
- Joel recaps what he already knows: long temporal-frame features, TRAPS, tandem approaches, and AR-modeling the DCT. (start_s=113.82) — item `it_6c0a541af279`
- Origin story: Mario's paper first used AR models on the DCT over short (50 ms) segments, later recognized as a neat way to estimate spectral energies. (start_s=178.2) — item `it_8fff994e462c`
- Knob-tuning experiments (compression factor, lengths) were run but never published because an essential question stayed unresolved. (start_s=250.9) — item `it_0b11210a9564`
- They weigh computational cost: FDLP needs huge DCT/FFT and is ~100x MFCC, with no optimization and a naive 10 ms step. (start_s=271.75) — item `it_5d512b32c676`
- Inventory of what exists: feature extraction produces phoneme posteriors, the HTK backend is missing but easy; front ends include MFCC/PLP, TRAPS, DCT-on-TRAPS, warping, and tandem. (start_s=451.56) — item `it_ed5e4d31b4fc`
- Deep dive on FDLP for speech coding: one-second segments, sub-bands, Hilbert envelope plus carrier/residual, compression ~0.1, and ~15 bands. (start_s=693.48) — item `it_09b75e2dc560`
- STLP / the 1983 spectral-transform paper is recalled as the technique for balancing peaks and dips, followed by reading recommendations and Mario's leftover files and results table. (start_s=1441.8) — item `it_d5d91af2d40d`
- Wrap-up: send papers and code pointers, run the setup end-to-end, stay reachable on Skype until ~May, and discuss the upcoming perceptual-systems teaching. (start_s=1830.66) — item `it_cb1edb62eff6`

### live_notes

- A senior researcher opens by explaining the meeting is a handoff: he will listen while Joel talks. The goal is to make sure the temporal-envelope / FDLP work is not forgotten and can be taken up by others. Joel is expected to ask questions and eventually pick up keyword spotting and envelope-modeling ideas. (heading=Purpose and framing; start_s=21.5) — item `it_206fb57c1073`
- Mario's paper first applied AR modeling to the DCT, originally over short segments; the group realized it could neatly estimate spectral energies instead of the inelegant frame-by-frame-then-trajectory approach. Experiments tuning compression and trajectory length were done but never published because an unresolved essential question prevented confidence in the results. FDLP is also very slow (~100x MFCC) because of large DCT/FFT operations. (heading=Origin, cost, and the unpublished question; start_s=178.2) — item `it_5d9dd3042df7`
- Inventory of the lab's building blocks: feature extraction already yields phoneme posteriors through the neural net; the HTK backend is the only missing piece and is considered easy. Front ends include MFCC/PLP for comparison, TRAPS with various lengths, DCT applied to TRAPS (Gaussian-like filtering), warping/resampling with high center resolution, and a tandem system feeding PLP/MFCC into an MLP with context. Tandem experiments are limited; Hemant's results are mostly in his thesis. (heading=Current setup and front ends; start_s=451.56) — item `it_cccde59fc539`
- For speech coding they split ~1-second frames into frequency sub-bands and decompose each into a Hilbert envelope and a carrier/residual; both must be transmitted to reconstruct speech. They use ~15 bands (12 reportedly optimal) and a compression factor near 0.1. The chief difficulty is that the carrier deviates from a clean cosine as bands widen; higher bands tolerate noise, while low-band noise makes speech sound whispered. (heading=FDLP for speech coding; start_s=693.48) — item `it_b7289d128d3d`
- They recall the spectral-transform technique (STLP) from a 1983 paper as a way to balance fitting peaks and dips — useful for envelope rather than peak-only modeling. Reading is recommended: Mario's Sapa/Interspeech/ASRU papers and Les Atlas's rigorous work on DCT-based coding. Mario's MATLAB implementations and a large Excel results table remain in the lab; the optimized backend recognizer now runs in ~11–15 minutes versus ~1.5 hours before. (heading=Spectral transform, literature, and legacy files; start_s=1441.8) — item `it_e3524e616573`
- Closing: the host will send Joel the papers and point him to all the FDLP code, and asks him to run the whole setup once. They agree the work stays confidential until published, giving sponsors a patent window. Joel can reach the host on Skype until about May. They briefly discuss the host's upcoming perceptual-systems teaching in November and recap a past course's time crunch. (heading=Next steps and closing; start_s=1830.66) — item `it_0996dad15fcc`
- Joel shares where he is: he has read about long temporal-frame features, TRAPS, tandem approaches, and papers on fitting AR models to the DCT of the signal, and is getting used to the setup. The host notes this is what Christos and Mario were doing, referencing Mario's short (50 ms) segment work. (heading=What Joel already knows; start_s=113.82) — item `it_788a461dc6ec`

## IN1007 / thin-gemini

Completed: True; elapsed: 14.2 seconds.

**Topic:** Knowledge handover and research sync on speech features, TRAP models, and FDLP

**Summary:** The group gathers to document technical knowledge and ongoing research before a team member's impending departure, focusing on handing over keyword spotting and temporal envelope modeling work. The discussion covers TRAP features, warping, tandem modeling, and frequency domain linear prediction (FDLP) in speech coding, including sub-band configuration, Hilbert envelope modeling, and spectral compression. Practical details regarding codebases, training scripts, literature references, and administrative/academic status are also reviewed.

### key_points

- The meeting's purpose is to preserve and transfer knowledge regarding ongoing research before a team member leaves, ensuring continuity in keyword spotting and temporal envelope modeling. (commitment_status=not_applicable) — item `it_0`
- Historical work on fitting autoregressive models to the DCT of signals originated with Athineos, Hermansky, and Ellis, initially exploring segments around 50 milliseconds before transitioning to longer temporal contexts. (commitment_status=not_applicable) — item `it_1`
- The QuickNet development status was discussed, noting that while Barry previously maintained it and Brno is exploring four-layer nets, Olivier Bornet is the local expert who resolved large-file handling past the 2GB barrier. (commitment_status=not_applicable) — item `it_3`
- Past experimental work included varying TRAP lengths from one frame up to one second, applying DCT filtering to TRAPs, and warping trajectories by retaining central points while subsampling boundaries down to 15 points. (commitment_status=not_applicable) — item `it_4`
- FDLP speech coding operates on one-second non-overlapping time frames divided into overlapping sub-bands using Gaussian or triangular filters, decomposing them into Hilbert envelopes and carriers. (commitment_status=not_applicable) — item `it_5`
- Using around 15 sub-bands provides a good tradeoff in FDLP coding; decreasing to fewer bands broadens the bands and complicates the carrier, while noise substitution across all bands sounds like whispered speech. (commitment_status=not_applicable) — item `it_6`
- Applying spectral compression before autoregressive modeling allows balanced fitting of both dips and peaks in the Hilbert envelope, avoiding reverberation artifacts. (commitment_status=not_applicable) — item `it_7`
- Relevant literature for the FDLP approach includes papers by Marios Athineos (SAPA, Interspeech, ASRU, and IDIAP reports) as well as publications by Les Atlas. (commitment_status=not_applicable) — item `it_8`
- Marios Athineos's original MATLAB scripts, parameter tables, and experimental recognition results remain accessible in the local workspace. (commitment_status=not_applicable) — item `it_9`
- Petr plans to depart by Friday, November 24, but will remain reachable via Skype until around May of the following year. (commitment_status=not_applicable) — item `it_12`

### action_items

- Provide the backend HTK recognizer setup to Joel to run the recognition pipeline. (commitment_status=accepted) — item `it_10`
- Send the relevant papers and share file locations for the FDLP codebases. (commitment_status=accepted) — item `it_11`

### risks

- Computing spectral energy features via full DCT and FFT on unoptimized 10 ms steps proved computationally inefficient, running roughly 100 times slower than MFCCs. (commitment_status=not_applicable) — item `it_2`

## IN1007 / thin-gemini-assisted

Completed: True; elapsed: 26.5 seconds.

**Topic:** FDLP, TRAPs, and Speech Recognition Project Knowledge Transfer

**Summary:** The group gathers to ensure research knowledge regarding TRAPs, tandem systems, and Frequency Domain Linear Prediction (FDLP) is preserved and transferred to Joel before Petr leaves. They discuss FDLP principles in speech coding, temporal envelope modeling, compression factors, relevant literature, code repositories, and Petr's remaining timeline and availability.

### key_points

- The meeting's purpose is to record and transfer research knowledge on keyword spotting, TRAPs, and temporal envelope models to Joel so ongoing work continues smoothly after Petr leaves. (commitment_status=not_applicable) — item `it_0`
- Joel has established the front-end feature extraction pipeline to generate phoneme posteriors using neural networks and is familiar with running HTK. (commitment_status=not_applicable) — item `it_1`
- Prior research covered MFCC/PLP baselines, TRAPs of various window lengths with DCT, and a temporal warping technique that retained central resolution while downsampling boundaries from 101 points to about 15 points. (commitment_status=not_applicable) — item `it_2`
- In Petr's speech coding work with FDLP, speech is segmented into 1-second frames without time overlap, followed by DCT, subband division using overlapping filters, and computation of the Hilbert envelope and carrier. (commitment_status=not_applicable) — item `it_3`
- Using approximately 15 subbands (or down to 12) provides a reasonable balance; fewer bands make the residual carrier excitation signal more complex. (commitment_status=not_applicable) — item `it_4`
- Spectral transform compression alters how the autoregressive model fits the envelope, allowing balanced modeling of dips rather than strictly fitting spectral peaks. (commitment_status=not_applicable) — item `it_5`
- Key reference papers include works by Marios Athineos (such as ASRU, SAPA, and IDIAP reports) and publications by Les Atlas covering transform coding and signal representations. (commitment_status=not_applicable) — item `it_6`
- Marios Athineos's original MATLAB implementations and an Excel table tracking parameter tunings and recognition results remain available on the system. (commitment_status=not_applicable) — item `it_7`
- Petr will be present until Friday, November 24th, after which he will return to Prague and remain accessible via Skype until around May. (commitment_status=not_applicable) — item `it_10`

### action_items

- Provide Joel with the backend speech recognizer setup that achieves an 11- to 15-minute turnaround. (commitment_status=accepted) — item `it_8`
- Send Joel the relevant papers and provide pointers to all existing FDLP codebases and files. (commitment_status=accepted) — item `it_9`
