let meta = #{
    name: "review-meeting-mode",
    description: "Review OpenWhisper meeting-mode across six dimensions, adversarially verify each finding, write a report",
    when_to_use: "Review meeting-mode code (meeting/, webui, sidecar, meeting UI/tests) with parallel dimensions and adversarial verification",
    phases: [
        #{ title: "Review", detail: "one reviewer per meeting-mode dimension" },
        #{ title: "Verify", detail: "one skeptic per in-scope finding" },
        #{ title: "Report", detail: "markdown report of confirmed findings" },
    ],
};

const MAX_PER_DIM = 4;
const MAX_TOTAL = 24;

let findings_schema = #{
    "type": "object",
    "required": ["findings"],
    "properties": #{
        "findings": #{
            "type": "array",
            "maxItems": 4,
            "items": #{
                "type": "object",
                "required": ["file", "issue", "severity"],
                "properties": #{
                    "file": #{ "type": "string" },
                    "issue": #{ "type": "string" },
                    "severity": #{ "type": "string", "enum": ["high", "medium", "low"] },
                    "evidence": #{ "type": "string" },
                },
            },
        },
    },
};

let verdict_schema = #{
    "type": "object",
    "required": ["real", "reason", "evidence"],
    "properties": #{
        "real": #{ "type": "boolean" },
        "reason": #{ "type": "string" },
        "evidence": #{ "type": "string" },
    },
};

fn trimmed(s) {
    if type_of(s) == "string" {
        s.trim();
        s
    } else {
        ""
    }
}

fn has_prefix(path, pre) {
    if type_of(path) != "string" || type_of(pre) != "string" { return false; }
    if pre.len() == 0 { return false; }
    if path.len() < pre.len() { return false; }
    path.sub_string(0, pre.len()) == pre
}

fn strip_dot_slash(path) {
    let p = trimmed(path);
    if has_prefix(p, "./") {
        p.sub_string(2, p.len() - 2)
    } else {
        p
    }
}

fn is_in_scope(path) {
    let p = strip_dot_slash(path);
    if p == "" { return false; }
    let prefixes = [
        "meeting/",
        "sidecar/",
        "webui/",
        "services/runtime/meeting.py",
        "services/models.py",
        "services/database.py",
        "ui_qt/widgets/meeting_mode_tab.py",
        "ui_qt/widgets/past_meetings_panel.py",
        "ui_qt/dialogs/meeting_",
        "tests/test_meeting_",
        "tests/test_past_meetings_panel.py",
        "benchmarks/meeting_mode/",
        "specs/meeting-mode-",
    ];
    for pre in prefixes {
        if has_prefix(p, pre) { return true; }
    }
    false
}

fn scope_block() {
    let s = "";
    s += "IN-SCOPE PATHS ONLY (the orchestrator drops anything else):\n";
    s += "- meeting/\n";
    s += "- sidecar/\n";
    s += "- webui/\n";
    s += "- services/runtime/meeting.py\n";
    s += "- services/models.py and services/database.py (meeting tables / migrations only)\n";
    s += "- ui_qt/widgets/meeting_mode_tab.py\n";
    s += "- ui_qt/widgets/past_meetings_panel.py\n";
    s += "- ui_qt/dialogs/meeting_*.py\n";
    s += "- tests/test_meeting_*.py and tests/test_past_meetings_panel.py\n";
    s += "- benchmarks/meeting_mode/ and specs/meeting-mode-*\n";
    s
}

fn sort_by_severity(items) {
    let high = [];
    let mid = [];
    let low = [];
    for f in items {
        let sev = "";
        if f.severity != () { sev = trimmed(f.severity); }
        if sev == "high" {
            high.push(f);
        } else if sev == "medium" {
            mid.push(f);
        } else {
            low.push(f);
        }
    }
    let out = [];
    for f in high { out.push(f); }
    for f in mid { out.push(f); }
    for f in low { out.push(f); }
    out
}

fn take_first(items, n) {
    let out = [];
    let i = 0;
    for f in items {
        if i >= n { break; }
        out.push(f);
        i += 1;
    }
    out
}

fn as_map_output(r) {
    r != () && r.success && r.output != () && type_of(r.output) == "map"
}

fn copy_finding(f, dim_id) {
    let file = "";
    let issue = "";
    let sev = "medium";
    let ev = "";
    if f.file != () { file = strip_dot_slash(f.file); }
    if f.issue != () { issue = trimmed(f.issue); }
    if f.severity != () && trimmed(f.severity) != "" { sev = trimmed(f.severity); }
    if f.evidence != () { ev = trimmed(f.evidence); }
    #{
        file: file,
        issue: issue,
        severity: sev,
        evidence: ev,
        dimension: dim_id,
    }
}

fn review_prompt(dim, base, focus, diff_hint) {
    let p = "";
    p += "You are reviewing OpenWhisper Meeting Mode in this repository. ";
    p += "Use read-only tools (read_file, grep, and git diff) to inspect the actual code. ";
    p += "Do not answer from memory. An empty findings list is valid ONLY after you have ";
    p += "opened multiple in-scope files and searched for the issues in this dimension.\n\n";
    p += "Dimension: " + dim.title + "\n";
    p += dim.ask + "\n\n";
    p += scope_block();
    p += "\n" + diff_hint + "\n";
    if base != "" {
        p += "\nGit base ref: " + base + "\n";
        p += "Run: git diff ";
        p += base;
        p += " -- meeting/ sidecar/ webui/ services/runtime/meeting.py services/models.py services/database.py ui_qt/widgets/meeting_mode_tab.py ui_qt/widgets/past_meetings_panel.py ui_qt/dialogs tests/test_meeting_ tests/test_past_meetings_panel.py benchmarks/meeting_mode/ specs\n";
    }
    if focus != "" {
        p += "\nAdditional focus from the user: ";
        p += json_encode(focus);
        p += "\n";
    }
    p += "\nHard product constraints to check when they apply:\n";
    p += "- meeting/ must not import Qt.\n";
    p += "- New meeting tables use fresh names. Do not revive deleted meeting-mode code. ";
    p += "Leave services/database.py _drop_removed_meeting_tables untouched.\n";
    p += "- Agents have state-only authority: validated structured patches, no shell/fs/external actions.\n";
    p += "- Human-touched items (pinned, edited, confirmed) must be protected from agent overwrite.\n";
    p += "- Generated claims should carry evidence segment ids.\n";
    p += "- Audio must not leave the machine. Transcript/state may go to OpenRouter only after consent.\n\n";
    p += "Report at most 4 concrete findings. Each finding must cite a real in-scope file path ";
    p += "and a specific defect or gap you observed in the code. ";
    p += "severity is high, medium, or low. Put a short evidence snippet or symbol name in evidence.\n";
    p
}

fn verify_prompt(f) {
    let p = "";
    p += "Adversarially verify this OpenWhisper meeting-mode review finding. ";
    p += "You are a skeptic. Default real=false. Set real=true ONLY if you independently open the file ";
    p += "and find concrete evidence that the issue is real in the shipped code.\n\n";
    p += "File: ";
    p += json_encode(f.file);
    p += "\nClaimed issue: ";
    p += json_encode(f.issue);
    p += "\nClaimed severity: ";
    p += json_encode(f.severity);
    p += "\nDimension: ";
    p += json_encode(f.dimension);
    if f.evidence != () && trimmed(f.evidence) != "" {
        p += "\nReviewer evidence: ";
        p += json_encode(f.evidence);
    }
    p += "\n\nUse read_file and grep on that file and its callers/tests. ";
    p += "If the file does not exist, or the behavior is intentional, tested, or speculative, real=false. ";
    p += "evidence must quote what you read (function or class name, or a short snippet).\n";
    p
}

fn finding_heading(idx, f) {
    let n = (idx + 1).to_string();
    let sev = "medium";
    let file = "";
    let dim = "";
    if f.severity != () { sev = trimmed(f.severity); }
    if f.file != () { file = trimmed(f.file); }
    if f.dimension != () { dim = trimmed(f.dimension); }
    let s = "";
    s += "### ";
    s += n;
    s += ". `";
    s += file;
    s += "` — ";
    s += sev;
    s += "\n\n";
    if dim != "" {
        s += "Dimension: ";
        s += dim;
        s += "\n\n";
    }
    s
}

fn build_report(base, focus, confirmed, rejected, unverified, dropped_scope, dropped_cap) {
    let report = "";
    report += "# Meeting-mode review\n\n";
    report += "Confirmed ";
    report += confirmed.len().to_string();
    report += " of ";
    report += (confirmed.len() + rejected.len() + unverified.len()).to_string();
    report += " in-scope findings after adversarial verification.\n\n";
    report += "Only **confirmed** findings survived an independent skeptic that had to quote evidence. ";
    report += "Failed or empty verifications are listed as unverified, not as confirmations.\n\n";
    report += "## Scope\n\n";
    report += scope_block();
    report += "\n";
    if base != "" {
        report += "Git base: `";
        report += base;
        report += "`\n\n";
    } else {
        report += "No git base given — reviewers inspected the current meeting-mode tree.\n\n";
    }
    if focus != "" {
        report += "User focus: ";
        report += focus;
        report += "\n\n";
    }
    if dropped_scope > 0 {
        report += "Dropped ";
        report += dropped_scope.to_string();
        report += " out-of-scope finding(s) (path not in the meeting-mode allowlist).\n\n";
    }
    if dropped_cap > 0 {
        report += "Dropped ";
        report += dropped_cap.to_string();
        report += " finding(s) over the cap of ";
        report += MAX_TOTAL.to_string();
        report += " (kept highest severity first).\n\n";
    }

    report += "## Confirmed\n\n";
    if confirmed.len() == 0 {
        report += "None.\n\n";
    } else {
        let i = 0;
        for f in confirmed {
            report += finding_heading(i, f);
            if f.issue != () {
                report += trimmed(f.issue);
                report += "\n\n";
            }
            if f.reason != () && trimmed(f.reason) != "" {
                report += "Verifier: ";
                report += trimmed(f.reason);
                report += "\n\n";
            }
            if f.verify_evidence != () && trimmed(f.verify_evidence) != "" {
                report += "Evidence: ";
                report += trimmed(f.verify_evidence);
                report += "\n\n";
            }
            i += 1;
        }
    }

    report += "## Rejected by skeptics\n\n";
    if rejected.len() == 0 {
        report += "None.\n\n";
    } else {
        let i = 0;
        for f in rejected {
            report += finding_heading(i, f);
            if f.issue != () {
                report += trimmed(f.issue);
                report += "\n\n";
            }
            if f.reason != () && trimmed(f.reason) != "" {
                report += "Why rejected: ";
                report += trimmed(f.reason);
                report += "\n\n";
            }
            i += 1;
        }
    }

    report += "## Unverified\n\n";
    if unverified.len() == 0 {
        report += "None — every in-scope finding got a usable skeptic verdict.\n";
    } else {
        report += "These did not get a usable verdict (failed slot, missing evidence, or unusable output). ";
        report += "They are **not** confirmations.\n\n";
        let i = 0;
        for f in unverified {
            report += finding_heading(i, f);
            if f.issue != () {
                report += trimmed(f.issue);
                report += "\n\n";
            }
            i += 1;
        }
    }
    report
}

let base = "";
let focus = "";
if args != () && type_of(args) == "map" {
    if args.base != () { base = trimmed(args.base); }
    if args.focus != () { focus = trimmed(args.focus); }
}

let diff_hint = "No base commit given. Review the current meeting-mode tree on disk, not from memory.";
if base != "" {
    diff_hint = "Focus on changes since the git base, but read the current files — do not review a diff hunk in isolation.";
    try {
        let diff_text = git_diff_since(base);
        if type_of(diff_text) == "string" {
            log("Diff since " + base + " is " + diff_text.len().to_string() + " chars");
        }
    } catch (err) {
        log("git_diff_since failed; reviewers will inspect the current tree");
        diff_hint = "Diff against the base was unavailable. Review the current meeting-mode tree on disk.";
    }
}

let dimensions = [
    #{
        id: "engine-state",
        title: "engine, state store, patches, persist, clock",
        ask: "Look for correctness bugs: races on the single-writer store, patch validation holes, revision or pin-protection bypasses, clock pause-credit or gap-fill errors, persist/snapshot seq mismatches, and any Qt import inside meeting/.",
    },
    #{
        id: "audio-asr-diarize",
        title: "capture, spool, ASR, diarization, playback",
        ask: "Look for audio-pipeline bugs: device/loopback selection, spool crash-safety, chunk cuts, ASR retry/timestamps, diarizer pin preservation, Me/Others fallback, and playback/export of the wrong channel or clock.",
    },
    #{
        id: "agent-insights",
        title: "agent core, scheduler, evidence, reinsight, recall",
        ask: "Look for intelligence-loop bugs: unvalidated patches, agent overwrite of human-touched items, missing evidence ids, scheduler coalescing/topic-shift mistakes, sidecar RPC/auth holes, and reinsight/recall corrupting state.",
    },
    #{
        id: "recovery-consent",
        title: "recovery, exclusive mode, consent, data lifecycle",
        ask: "Look for lifecycle bugs: crash recovery of interrupted sessions, exclusive-mode leaks that leave dictation hotkeys or the whisper model in a bad state, consent/cloud-toggle holes that could send audio, and delete/retention not cascading spool files.",
    },
    #{
        id: "web-dashboard",
        title: "FastAPI, WebSocket, auth, React dashboard",
        ask: "Look for web/multi-user bugs: token compare, host vs guest authorization, WS hello/resync, last-write-wins vs audit, optimistic client_action_id, and dashboard actions that skip the Python patch validator.",
    },
    #{
        id: "tests",
        title: "missing or weak meeting-mode tests",
        ask: "Compare meeting-mode behavior in the in-scope production files against tests/test_meeting_*.py. Report only concrete untested or weakly tested behavior you can point at in both a production file and the test tree. An empty list is valid after you have read the tests.",
    },
];

phase("Review");
let jobs = [];
for dim in dimensions {
    jobs.push(#{
        prompt: review_prompt(dim, base, focus, diff_hint),
        label: "review:" + dim.id,
        capability_mode: "read-only",
        output_schema: findings_schema,
    });
}
let results = parallel(jobs);

let raw = [];
let dim_i = 0;
for r in results {
    let dim_id = dimensions[dim_i].id;
    if as_map_output(r) && r.output.findings != () && type_of(r.output.findings) == "array" {
        let n = 0;
        for f in r.output.findings {
            if n >= MAX_PER_DIM { break; }
            if f != () && type_of(f) == "map" && f.file != () && f.issue != () {
                if trimmed(f.file) != "" && trimmed(f.issue) != "" {
                    raw.push(copy_finding(f, dim_id));
                    n += 1;
                }
            }
        }
    } else {
        log("Reviewer " + dim_id + " returned no usable findings object");
    }
    dim_i += 1;
}

let in_scope = [];
let dropped_scope = 0;
let seen = #{};
for f in raw {
    if !is_in_scope(f.file) {
        dropped_scope += 1;
    } else {
        let key = fingerprint(f.file + "|" + f.issue);
        if seen[key] == () {
            seen[key] = true;
            in_scope.push(f);
        }
    }
}
if dropped_scope > 0 {
    log("Dropped " + dropped_scope.to_string() + " out-of-scope finding(s)");
}

let ranked = sort_by_severity(in_scope);
let dropped_cap = 0;
if ranked.len() > MAX_TOTAL {
    dropped_cap = ranked.len() - MAX_TOTAL;
    log("Dropped " + dropped_cap.to_string() + " finding(s) over cap " + MAX_TOTAL.to_string());
    ranked = take_first(ranked, MAX_TOTAL);
}

let findings = ranked;
log(findings.len().to_string() + " in-scope finding(s) after filters");

let confirmed = [];
let rejected = [];
let unverified = [];

if findings.len() == 0 {
    log("No in-scope findings from reviewers");
} else {
    phase("Verify");
    let vjobs = [];
    for f in findings {
        vjobs.push(#{
            prompt: verify_prompt(f),
            label: "verify:" + f.dimension,
            capability_mode: "read-only",
            output_schema: verdict_schema,
        });
    }
    let verdicts = parallel(vjobs);

    let i = 0;
    for v in verdicts {
        let f = findings[i];
        if as_map_output(v)
            && v.output.real == true
            && v.output.evidence != ()
            && trimmed(v.output.evidence) != "" {
            let reason = "";
            if v.output.reason != () { reason = trimmed(v.output.reason); }
            confirmed.push(#{
                file: f.file,
                issue: f.issue,
                severity: f.severity,
                dimension: f.dimension,
                evidence: f.evidence,
                reason: reason,
                verify_evidence: trimmed(v.output.evidence),
            });
        } else if as_map_output(v) && v.output.real == false {
            let reason = "";
            if v.output.reason != () { reason = trimmed(v.output.reason); }
            rejected.push(#{
                file: f.file,
                issue: f.issue,
                severity: f.severity,
                dimension: f.dimension,
                reason: reason,
            });
        } else {
            unverified.push(f);
        }
        i += 1;
    }
    log(confirmed.len().to_string() + "/" + findings.len().to_string() + " findings survived verification");
}

phase("Report");
let report = build_report(base, focus, confirmed, rejected, unverified, dropped_scope, dropped_cap);
let report_path = write_scratch_file("report.md", report);
complete(#{
    path: report_path,
    report: report,
    summary: confirmed.len().to_string() + " confirmed finding(s)",
    confirmed: confirmed,
    rejected_count: rejected.len(),
    unverified_count: unverified.len(),
    dropped_out_of_scope: dropped_scope,
    dropped_over_cap: dropped_cap,
});
