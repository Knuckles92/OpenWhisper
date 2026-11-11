"""EndRun: the finalization steps of one meeting End.

After the engine has stopped capture, drained ASR, saved the terminal status
and told listeners the meeting ``ended``, the remaining work is a short
sequence of optional passes: re-decode, speaker identification, transcript
polish, the final report, and a closing summary. ``EndRun`` plans which of
those steps run, reports each one's progress as a finalization state, and
decides the final outcome.

It holds no engine state. The engine hands it the passes it can run and the
few things the outcome depends on as callables, so the step bookkeeping
reads on its own.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from meeting.finalization import (
    POLISH_TIMEOUT_S, failed_steps_message, insights_ready_message, make_step,
    saved_state_detail,
)

logger = logging.getLogger(__name__)

INSIGHTS_OFF_MESSAGE = "AI insights are off for this meeting."
RECOVERY_PENDING_MESSAGE = (
    "Final insights are unavailable until "
    "transcription recovery finishes."
)

#: Headline shown while each finalization step runs.
_STEP_RUNNING_MESSAGES = {
    "redecode": "Re-transcribing meeting…",
    "speaker_id": "Identifying speakers…",
    "polish": "Cleaning transcript…",
    "consolidation": "Preparing final report…",
}

ProgressCallback = Callable[[str, int, int], None]


class EndRun:
    """What one End carries from capture shutdown through finalization."""

    def __init__(
        self,
        meeting_id: Optional[str],
        *,
        complete: bool,
        unfinished: Any,
        scheduler: Any,
        will_offline: bool,
        will_speaker_id: bool,
        set_finalization: Callable[..., bool],
        redecode: Callable[[ProgressCallback], Any],
        redecode_failure: Callable[[], str],
        identify_speakers: Callable[..., Dict[str, Any]],
        allow_agent_writes: Callable[[], None],
        revoke_agent_writes: Callable[[], None],
        core_is_healthy: Callable[[], bool],
        offline_message: Callable[[], str],
        summary_stats: Callable[[], Dict[str, Any]],
    ) -> None:
        """
        Args:
            meeting_id: The meeting being finalized, for logging.
            complete: True when every chunk was transcribed and stored.
            unfinished: Count of chunks still waiting for transcription.
            scheduler: The intelligence scheduler, or None when offline.
            will_offline: True when the offline re-decode pass will run.
            will_speaker_id: True when cloud speaker identification will run.
            set_finalization: Persists and announces a finalization state;
                takes the engine's ``_set_finalization`` arguments.
            redecode: Runs the offline re-decode pass with a progress
                callback; truthy when a new transcript was committed.
            redecode_failure: Why the last re-decode kept the draft, or "".
            identify_speakers: Runs the cloud speaker pass
                (``progress_cb=``, ``on_start=``) and returns its result.
            allow_agent_writes: Lets the agent change the meeting state.
            revoke_agent_writes: Stops the agent changing the meeting state.
            core_is_healthy: Whether the agent core can take a pass.
            offline_message: Why meeting intelligence is offline.
            summary_stats: Final statistics for the closing summary.
        """
        self.meeting_id = meeting_id
        self.complete = complete
        self.unfinished = unfinished
        self.scheduler = scheduler
        self.will_offline = will_offline
        self.will_speaker_id = will_speaker_id
        self._set_finalization = set_finalization
        self._redecode = redecode
        self._redecode_failure = redecode_failure
        self._identify_speakers = identify_speakers
        self._allow_agent_writes = allow_agent_writes
        self._revoke_agent_writes = revoke_agent_writes
        self._core_is_healthy = core_is_healthy
        self._offline_message = offline_message
        self._summary_stats = summary_stats
        self.cloud_enabled = False
        self.want_polish = False
        self.want_report = False
        self.run_cloud = False
        self.steps: List[Dict[str, Any]] = []
        self.total_steps = 0
        self.offline_ok = False
        self.speaker_ok = False
        self.speaker_skipped = False
        self.speaker_error = ""

    def update_step(self, step_id: str, step_status: str, detail_msg: str = "", *,
                    message: str = "", emit: bool = True) -> None:
        logger.log(
            logging.WARNING if step_status == "failed" else logging.INFO,
            "Meeting finalization meeting_id=%s stage=%s status=%s detail=%s",
            self.meeting_id, step_id, step_status, detail_msg,
        )
        curr_idx = 1
        for idx, s in enumerate(self.steps, 1):
            if s["id"] == step_id:
                s["status"] = step_status
                if detail_msg:
                    s["detail"] = detail_msg
                curr_idx = idx
                break
        top_msg = message or _STEP_RUNNING_MESSAGES.get(step_id, "Finalizing meeting state…")
        self._set_finalization(
            "running",
            top_msg,
            stage=step_id,
            current_step=curr_idx,
            total_steps=self.total_steps,
            step_details=detail_msg,
            steps=self.steps,
            emit=emit,
        )

    def progress(self, step_id: str, template: str) -> ProgressCallback:
        """A ``(detail, current, total)`` callback; ``template`` takes ``curr`` and ``total``."""
        def report(detail: str, curr: int, total: int) -> None:
            self.update_step(
                step_id,
                "running",
                detail,
                message=template.format(curr=curr, total=total),
            )
        return report

    def speaker_outcome(self) -> Tuple[str, str]:
        """Final status and message when speaker identification ends the run."""
        if self.speaker_ok:
            if self.cloud_enabled:
                return "completed", "Speaker identification finished."
            return "completed", (
                "Speaker identification finished. "
                "AI insights are off for this meeting."
            )
        if self.speaker_skipped:
            return "completed", self.speaker_error or (
                "Speaker identification skipped."
            )
        return "failed", self.speaker_error or (
            "Speaker identification failed."
        )

    def plan(self, *, cloud_enabled: bool, want_polish: bool, want_report: bool) -> None:
        """Choose the steps this End runs."""
        self.cloud_enabled = cloud_enabled
        self.want_polish = want_polish
        self.want_report = want_report
        self.run_cloud = bool(
            self.cloud_enabled
            and self.complete
            and self.scheduler is not None
            and self._core_is_healthy()
            and (self.want_polish or self.want_report)
        )
        self.steps = [make_step(step_id) for step_id, enabled in (
            ("redecode", self.will_offline),
            ("speaker_id", self.will_speaker_id),
            ("polish", self.run_cloud and self.want_polish),
            ("consolidation", self.run_cloud and self.want_report),
            ("finalize", True),
        ) if enabled]
        self.total_steps = len(self.steps)

    def announce(self) -> None:
        """Publish the opening finalization state before ``ended`` goes out."""
        if self.will_offline or self.will_speaker_id or self.run_cloud:
            self.update_step(self.steps[0]["id"], "running", self.steps[0]["detail"], emit=False)
        elif not self.cloud_enabled:
            self._set_finalization("disabled", INSIGHTS_OFF_MESSAGE, emit=False)
        elif not self.complete:
            self._set_finalization("unavailable", RECOVERY_PENDING_MESSAGE, emit=False)
        else:
            self._set_finalization("unavailable", self._offline_message(), emit=False)

    def redecode(self) -> None:
        if not self.will_offline:
            return
        self.update_step(
            "redecode",
            "running",
            "Starting high-accuracy session audio re-decoding...",
            message="Re-transcribing meeting…",
        )
        progress = self.progress("redecode", "Re-transcribing meeting (window {curr}/{total})…")
        try:
            self.offline_ok = bool(self._redecode(progress))
        except Exception:
            logger.exception("Offline clean ASR pass failed")
            self.offline_ok = False
        if self.offline_ok:
            self.update_step("redecode", "completed", "High-accuracy re-decoding complete")
        else:
            self.update_step(
                "redecode",
                "failed",
                self._redecode_failure() or "Re-decoding failed; kept live transcript",
            )

    def identify_speakers(self) -> None:
        if not self.will_speaker_id:
            return

        def _announce_upload() -> None:
            self.update_step(
                "speaker_id",
                "running",
                "Uploading system audio for speaker labels…",
                message="Identifying speakers…",
            )

        try:
            speaker_result = self._identify_speakers(
                progress_cb=self.progress("speaker_id", "Identifying speakers (window {curr}/{total})…"),
                on_start=_announce_upload,
            )
        except Exception:
            logger.exception("Cloud speaker identification failed")
            speaker_result = {
                "ok": False, "skipped": False,
                "error": "Speaker identification failed.",
            }
        from meeting.refinalize import speaker_step_outcome

        self.speaker_ok = bool(speaker_result.get("ok"))
        self.speaker_skipped = bool(speaker_result.get("skipped"))
        self.speaker_error = str(speaker_result.get("error") or "")
        self.update_step("speaker_id", *speaker_step_outcome(speaker_result))

    def insights(self) -> None:
        if self.run_cloud and self.scheduler is not None:
            self._run_cloud_passes()
        else:
            self._revoke_agent_writes()
            self._report_without_cloud()

    def _run_cloud_passes(self) -> None:
        try:
            self._allow_agent_writes()
            if self.want_polish:
                self._polish()
            if self.want_report:
                status, message = self._consolidate()
            else:
                status = "completed"
                message = "Transcript cleanup is ready."
        except Exception as exc:
            logger.exception("Post-end consolidation pass failed")
            self._revoke_agent_writes()
            self._set_finalization(
                "failed",
                f"Final insights failed: {exc}",
                stage="failed",
                current_step=self.total_steps,
                total_steps=self.total_steps,
                step_details=f"Final insights failed: {exc}",
                steps=self.steps,
            )
        else:
            self._revoke_agent_writes()
            self._complete(status, message)

    def _polish(self) -> None:
        self.update_step(
            "polish",
            "running",
            "Starting AI transcript cleanup and formatting...",
            message="Cleaning transcript…",
        )
        progress = self.progress("polish", "Cleaning transcript (block {curr}/{total})…")
        polish = getattr(self.scheduler, "run_final_polish", None)
        polish_status = "completed"
        polish_detail = "Transcript cleanup finished"
        if callable(polish):
            try:
                polish_outcome = polish(
                    timeout_s=POLISH_TIMEOUT_S,
                    progress_cb=progress,
                )
                polish_status = getattr(
                    polish_outcome, "status", "completed",
                )
                polish_detail = (
                    getattr(polish_outcome, "message", "")
                    or (
                        "Transcript cleanup finished"
                        if polish_status == "completed"
                        else "Transcript cleanup failed"
                    )
                )
            except Exception as exc:
                logger.exception("Post-end transcript polish failed")
                polish_status = "failed"
                polish_detail = f"Transcript cleanup failed: {exc}"
        self.update_step(
            "polish",
            "completed" if polish_status == "completed" else "failed",
            polish_detail,
        )

    def _consolidate(self) -> Tuple[str, str]:
        self.update_step(
            "consolidation",
            "running",
            "Synthesizing executive summary, key points, decisions, and action items...",
            message="Preparing final report…",
        )

        def _consolidation_progress(detail: str) -> None:
            self.update_step(
                "consolidation",
                "running",
                detail,
                message="Preparing final report…",
            )
        # live_notes is preserved into consolidation so the
        # final report pass can synthesize the meeting notes
        # and reconcile them against the final transcript.
        outcome = self.scheduler.run_consolidation(
            progress_cb=_consolidation_progress,
        )
        status = getattr(outcome, "status", "failed")
        message = getattr(outcome, "message", "") or ""
        if status == "completed":
            self.update_step("consolidation", "completed", "Summary & action items ready")
        else:
            self.update_step("consolidation", "failed", message)
        return status, message

    def _complete(self, status: str, message: str) -> None:
        self.update_step(
            "finalize",
            "running",
            "Saving final transcript and meeting state...",
            message="Finalizing meeting state…",
        )
        summary_stats = self._summary_stats()

        self.update_step(
            "finalize",
            "completed",
            saved_state_detail(summary_stats),
        )

        if any(s.get("status") == "failed" for s in self.steps):
            status = "failed"
            final_msg = failed_steps_message(self.steps)
        elif status == "completed" and self.want_report:
            final_msg = insights_ready_message(summary_stats)
        else:
            final_msg = message

        self._set_finalization(
            status,
            final_msg,
            stage="complete" if status == "completed" else status,
            current_step=self.total_steps,
            total_steps=self.total_steps,
            step_details=final_msg,
            steps=self.steps,
            summary_stats=summary_stats,
        )

    def _report_without_cloud(self) -> None:
        if self.will_speaker_id:
            self._finish_after_speakers()
        elif not self.cloud_enabled:
            self._set_finalization("disabled", INSIGHTS_OFF_MESSAGE)
        elif not self.complete:
            # Cloud passes wait for recovery even when the re-decode worked;
            # without a terminal state here the dashboard would keep showing
            # the re-decode as running.
            if not self.will_offline:
                self._set_finalization("unavailable", RECOVERY_PENDING_MESSAGE)
                return
            self.update_step(
                "finalize", "completed",
                "Saved meeting state after re-transcription.", emit=False,
            )
            self._set_finalization(
                "unavailable",
                RECOVERY_PENDING_MESSAGE,
                stage="unavailable",
                current_step=self.total_steps,
                total_steps=self.total_steps,
                step_details=RECOVERY_PENDING_MESSAGE,
                steps=self.steps,
            )
        elif not self.want_polish and not self.want_report:
            self._set_finalization(
                "completed",
                "Post-meeting cleanup and report are off.",
            )
        elif self.scheduler is None or not self._core_is_healthy():
            self._set_finalization("unavailable", self._offline_message())

    def _finish_after_speakers(self) -> None:
        self.update_step(
            "finalize",
            "completed",
            "Saved meeting state after speaker identification.",
        )
        final_status, final_msg = self.speaker_outcome()
        self._set_finalization(
            final_status,
            final_msg,
            stage=(
                "complete" if final_status == "completed"
                else final_status
            ),
            current_step=self.total_steps,
            total_steps=self.total_steps,
            step_details=final_msg,
            steps=self.steps,
        )
