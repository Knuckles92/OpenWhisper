"""State operations for tailored reports a participant asks for in their words.

Three ops, one lifecycle: the host ``request_custom_report`` claims a slot
under the state lock (so a double-click cannot start two agents on the same
meeting), the report worker ``finish_custom_report`` fills it in, and the
host may ``remove_custom_report`` when the answer was not what they wanted.

The agent never reaches these ops — the report worker writes as ``system``
with the ``custom-report`` actor id, exactly as the insight reviewer does, so
a meeting-intelligence pass can never forge a report the host did not ask for.
"""
from __future__ import annotations

from copy import deepcopy

from meeting.interfaces import OpResult
from meeting.state.schema import (
    CUSTOM_REPORT_STATUSES,
    MAX_CUSTOM_REPORTS,
    CustomReport,
    new_id,
    now_iso,
)

#: The worker's actor id. Only this system actor may publish report text.
WORKER_ACTOR = "custom-report"

#: Bounds on what a participant may ask for. Long enough for a real brief
#: ("summarize the pricing objections, who raised each, and what we
#: committed to"), short enough that the ask stays an ask.
MAX_REQUEST_CHARS = 2000

#: Bound on one stored report. Reports live in ``state_json``, which is read
#: whole on every snapshot, so a runaway document would slow the dashboard.
MAX_REPORT_CHARS = 60000

MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 500

__all__ = [
    "CUSTOM_REPORT_HANDLERS",
    "MAX_MESSAGE_CHARS",
    "MAX_REPORT_CHARS",
    "MAX_REQUEST_CHARS",
    "MAX_TITLE_CHARS",
    "WORKER_ACTOR",
]


def _effect(report: CustomReport, removed: bool = False) -> dict:
    return {
        "entity": "custom_report",
        "report": report.to_dict(),
        "removed": removed,
    }


def _reject(op, reason: str) -> OpResult:
    return OpResult(ok=False, op=op, reason=reason)


def _clip(value, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


def _ready_for_reports(state) -> bool:
    """True once the durable record is settled enough to report against.

    A report reads the finished corpus, so it waits for the same gate the
    insight review uses: the meeting is over and post-meeting consolidation
    is not still rewriting the cards underneath it.
    """
    return (
        state.status == "ended"
        and state.finalization.status not in ("pending", "running")
    )


def request_custom_report(state, op, ctx) -> OpResult:
    """Host asks for a tailored report; the record starts in ``running``."""
    if ctx.actor_type not in ("host", "system"):
        return _reject(op, "host_only")
    if not _ready_for_reports(state):
        return _reject(op, "meeting_not_ready")
    request = op.get("request")
    if not isinstance(request, str) or not request.strip():
        return _reject(op, "invalid_request")
    if len(request) > MAX_REQUEST_CHARS:
        return _reject(op, "request_too_long")
    run_id = op.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _reject(op, "invalid_run_id")
    if any(r.status == "running" for r in state.custom_reports):
        return _reject(op, "report_running")
    if len(state.custom_reports) >= MAX_CUSTOM_REPORTS:
        return _reject(op, "report_limit_reached")
    report = CustomReport(
        id=new_id("rep"),
        request=request.strip(),
        status="running",
        run_id=run_id,
        message="Reading the meeting and writing your report…",
        requested_by=ctx.actor_id,
    )
    state.custom_reports.append(report)
    return OpResult(ok=True, op=op, target_id=report.id, effect=_effect(report))


def finish_custom_report(state, op, ctx) -> OpResult:
    """The worker publishes the finished report, or why it could not finish."""
    if ctx.actor_type != "system" or ctx.actor_id != WORKER_ACTOR:
        return _reject(op, "system_only")
    report = state.find_custom_report(str(op.get("report_id") or ""))
    if report is None:
        return _reject(op, "unknown_report")
    # A stale run cannot overwrite a report the host already deleted and
    # re-requested: the id may repeat in a retry, the run id never does.
    if report.status != "running" or report.run_id != op.get("run_id"):
        return _reject(op, "stale_report")
    status = op.get("status")
    if status not in CUSTOM_REPORT_STATUSES or status == "running":
        return _reject(op, "invalid_report_status")
    markdown = _clip(op.get("markdown"), MAX_REPORT_CHARS)
    if status == "ready" and not markdown.strip():
        return _reject(op, "empty_report")
    report.status = status
    report.markdown = markdown
    report.title = _clip(op.get("title"), MAX_TITLE_CHARS).strip()
    report.message = _clip(op.get("message"), MAX_MESSAGE_CHARS)
    sources = op.get("sources")
    report.sources = deepcopy(sources) if isinstance(sources, dict) else {}
    report.updated_at = now_iso()
    return OpResult(ok=True, op=op, target_id=report.id, effect=_effect(report))


def remove_custom_report(state, op, ctx) -> OpResult:
    """Host discards a report, finished or still running.

    Removing a running report is also the escape hatch for one stranded by a
    crash: a ``running`` record survives in ``state_json`` and would
    otherwise block every later request. The abandoned worker's finish op
    then lands on a report that no longer exists and is rejected, which is
    the outcome we want — the host's discard wins.
    """
    if ctx.actor_type not in ("host", "system"):
        return _reject(op, "host_only")
    report = state.find_custom_report(str(op.get("report_id") or ""))
    if report is None:
        return _reject(op, "unknown_report")
    state.custom_reports = [r for r in state.custom_reports if r.id != report.id]
    return OpResult(ok=True, op=op, target_id=report.id,
                    effect=_effect(report, removed=True))


CUSTOM_REPORT_HANDLERS = {
    "request_custom_report": request_custom_report,
    "finish_custom_report": finish_custom_report,
    "remove_custom_report": remove_custom_report,
}
