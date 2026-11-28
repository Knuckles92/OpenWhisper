"""Pure, platform-agnostic state machines for call detection and auto-stop.

All time-dependent methods accept an explicit monotonic timestamp ``now``
(in seconds) so unit tests can drive every transition deterministically
without sleeping.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Final, Mapping, Optional

from meeting.call_detection.models import (
    CallCandidate,
    CallEndEvent,
    CallEndGraceCancelled,
    CallEndGraceStarted,
    CallEndStopDue,
    CallPolicy,
    DetectionAutoRecord,
    DetectionEvent,
    DetectionOffer,
    WatchedAppStatus,
)

#: An offered/dismissed/recorded call stays snoozed until absent this long.
ABSENT_FOR_RESET_S: Final[float] = 60.0
#: Continuous absence required before a call is treated as ended.
CALL_ENDED_AFTER_S: Final[float] = 3.0
#: Grace countdown after call end before Meeting Mode actually stops.
AUTO_STOP_GRACE_S: Final[float] = 30.0
#: Gaps larger than this (e.g. system sleep) restart a pending stability run.
MAX_OBSERVATION_GAP_S: Final[float] = 10.0
#: Brief mic dropout tolerated without resetting a pending stability run.
DROPOUT_GRACE_S: Final[float] = 2.0
#: Default timeout for an unanswered offer toast.
OFFER_TIMEOUT_S: Final[float] = 20.0

_offer_id_counter = itertools.count(1)


def next_offer_id() -> int:
    return next(_offer_id_counter)


@dataclass
class _Run:
    since: float
    last: float

    @classmethod
    def start(cls, now: float) -> "_Run":
        return cls(since=now, last=now)

    def extend(self, now: float) -> float:
        if now - self.last > MAX_OBSERVATION_GAP_S:
            self.since = now
        self.last = now
        return max(0.0, now - self.since)


@dataclass
class _Pending:
    key: str
    application: Optional[str]
    unresolved: bool
    run: _Run


@dataclass
class _SnoozedCall:
    key: str
    application: Optional[str]
    call_code: Optional[str]
    last_present: float

    @classmethod
    def from_candidate(cls, candidate: CallCandidate, now: float) -> "_SnoozedCall":
        return cls(
            key=candidate.key,
            application=candidate.application,
            call_code=candidate.call_code,
            last_present=now,
        )

    def snoozes(self, candidate: CallCandidate) -> bool:
        if self.key == candidate.key:
            return True
        if (
            candidate.application is not None
            and self.application == candidate.application
        ):
            # Two distinct call codes (e.g., switching Google Meet rooms in the
            # same browser) are treated as separate calls.
            if (
                self.call_code is not None
                and candidate.call_code is not None
                and self.call_code != candidate.call_code
            ):
                return False
            return True
        return False


class _SnoozeKind:
    CLEAR = "clear"
    OFFERED = "offered"
    KEPT = "kept"
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"


class CallDetector:
    """Debounces microphone call candidates and manages per-call snooze state."""

    def __init__(self) -> None:
        self._pending: Optional[_Pending] = None
        self._snooze_kind: str = _SnoozeKind.CLEAR
        self._snooze_call: Optional[_SnoozedCall] = None
        self._open_offer_id: Optional[int] = None
        self._withdrawn_offers: list[int] = []

    @property
    def open_offer_id(self) -> Optional[int]:
        return self._open_offer_id

    def take_withdrawn_offers(self) -> list[int]:
        """Drain and return offer IDs that should be closed in the UI."""
        out = list(self._withdrawn_offers)
        self._withdrawn_offers.clear()
        return out

    def _set_snooze(
        self,
        kind: str,
        call: Optional[_SnoozedCall],
        offer_id: Optional[int] = None,
    ) -> None:
        if self._snooze_kind == _SnoozeKind.OFFERED and self._open_offer_id is not None:
            self._withdrawn_offers.append(self._open_offer_id)
        self._snooze_kind = kind
        self._snooze_call = call
        self._open_offer_id = offer_id if kind == _SnoozeKind.OFFERED else None

    def _clear_snooze(self) -> None:
        self._set_snooze(_SnoozeKind.CLEAR, None)

    def _absence_grace_s(self) -> Optional[float]:
        if self._snooze_kind == _SnoozeKind.CLEAR:
            return None
        if self._snooze_kind in (_SnoozeKind.OFFERED, _SnoozeKind.KEPT):
            return ABSENT_FOR_RESET_S
        if self._snooze_kind == _SnoozeKind.STOPPED:
            return CALL_ENDED_AFTER_S
        if self._snooze_kind == _SnoozeKind.TIMED_OUT:
            return 0.0
        return None

    def keep_snooze_alive(self, now: float) -> None:
        """Refresh the snoozed call's presence while a recording is running."""
        if self._snooze_call is not None:
            self._snooze_call.last_present = now

    def mark_offer_answered(self, offer_id: int) -> None:
        """The user clicked Start / Not now / Always record / Don't offer."""
        if self._snooze_kind == _SnoozeKind.OFFERED and self._open_offer_id == offer_id:
            call = self._snooze_call
            self._snooze_kind = _SnoozeKind.KEPT
            self._open_offer_id = None
            self._snooze_call = call

    def offer_timed_out(self, offer_id: int) -> None:
        """The offer toast closed unanswered after its timeout.

        Stale timeouts for already-withdrawn or already-answered offers are
        ignored so they cannot overwrite a ``STOPPED`` or ``KEPT`` snooze.
        """
        if self._snooze_kind == _SnoozeKind.OFFERED and self._open_offer_id == offer_id:
            call = self._snooze_call
            self._snooze_kind = _SnoozeKind.TIMED_OUT
            self._open_offer_id = None
            self._snooze_call = call

    def withdraw_offer(self) -> Optional[int]:
        """Retire the open offer panel while keeping the call snoozed."""
        if self._snooze_kind == _SnoozeKind.OFFERED and self._open_offer_id is not None:
            oid = self._open_offer_id
            self._set_snooze(_SnoozeKind.KEPT, self._snooze_call)
            return oid
        return None

    def recording_stopped(self, now: float) -> None:
        """Notify the detector that a recording has stopped.

        If the snoozed call was already absent for >= CALL_ENDED_AFTER_S,
        clears snooze immediately so the next call is detected right away.
        Otherwise shrinks snooze grace to CALL_ENDED_AFTER_S.
        """
        if self._snooze_call is not None:
            if now - self._snooze_call.last_present >= CALL_ENDED_AFTER_S:
                self._clear_snooze()
                return
            self._set_snooze(_SnoozeKind.STOPPED, self._snooze_call)

    def observe(
        self,
        candidate: Optional[CallCandidate],
        app_policies: Mapping[str, object],
        default_policy: str,
        now: float,
    ) -> Optional[DetectionEvent]:
        """Process one observation at monotonic timestamp ``now``."""
        # 1. Expire snooze if the snoozed call has been absent long enough.
        if self._snooze_call is not None:
            if candidate is not None and self._snooze_call.snoozes(candidate):
                self._snooze_call.last_present = now
                self._pending = None
                return None
            grace = self._absence_grace_s()
            if grace is not None and (now - self._snooze_call.last_present) >= grace:
                self._clear_snooze()

        # 2. No active candidate: tolerate brief mic dropouts before clearing pending.
        if candidate is None:
            if self._pending is not None:
                if (now - self._pending.run.last) > DROPOUT_GRACE_S:
                    self._pending = None
            return None

        # 3. Check policy early: "Don't offer" does nothing and does not accumulate.
        policy = candidate.resolve_policy(app_policies, default_policy)
        if policy == CallPolicy.DONT_OFFER:
            self._pending = None
            return None

        # 4. Update or start pending stability run.
        same_pending = False
        if self._pending is not None:
            if self._pending.key == candidate.key:
                same_pending = True
            elif (
                self._pending.unresolved
                and candidate.application is not None
                and self._pending.application == candidate.application
            ):
                # Browser resolved from generic mic usage to a known call site:
                # carry over accumulated stability time!
                same_pending = True
                self._pending.key = candidate.key
                self._pending.unresolved = candidate.is_unresolved_browser

        if not same_pending:
            self._pending = _Pending(
                key=candidate.key,
                application=candidate.application,
                unresolved=candidate.is_unresolved_browser,
                run=_Run.start(now),
            )
            return None

        elapsed = self._pending.run.extend(now)
        if elapsed < candidate.required_stability_s:
            return None

        # 5. Candidate is stable! Emit Offer or AutoRecord.
        self._pending = None
        snoozed = _SnoozedCall.from_candidate(candidate, now)

        if policy == CallPolicy.ALWAYS_RECORD and candidate.can_always_record():
            self._set_snooze(_SnoozeKind.KEPT, snoozed)
            return DetectionAutoRecord(candidate=candidate)

        offer_id = next_offer_id()
        self._set_snooze(_SnoozeKind.OFFERED, snoozed, offer_id=offer_id)
        return DetectionOffer(candidate=candidate, offer_id=offer_id)


class CallEndTracker:
    """Tracks a watched call app during a recording and manages auto-stop grace.

    The call counts as present while the app holds the microphone **or** has an
    active audio output/playback stream (so muting in a call never triggers a
    false stop), or if an audio-status read fails.
    """

    def __init__(
        self,
        app_id: str,
        app_label: str,
        *,
        started_automatically: bool = False,
        absence_threshold_s: float = CALL_ENDED_AFTER_S,
        grace_s: float = AUTO_STOP_GRACE_S,
    ) -> None:
        self.app_id = app_id
        self.app_label = app_label
        self._started_automatically = started_automatically
        self._absence_threshold_s = absence_threshold_s
        self._grace_s = grace_s
        # Auto-started recordings already observed the app on the mic; manual
        # or on-request recordings wait until the app is seen on the mic at
        # least once before counting absence.
        self._seen_on_microphone: bool = started_automatically
        self._absent_since: Optional[float] = None
        self._grace_deadline: Optional[float] = None
        self._user_kept: bool = False

    @property
    def armed(self) -> bool:
        return self._grace_deadline is not None

    @property
    def grace_deadline(self) -> Optional[float]:
        return self._grace_deadline

    @property
    def user_kept(self) -> bool:
        return self._user_kept

    def remaining_grace_s(self, now: float) -> float:
        if self._grace_deadline is None:
            return 0.0
        return max(0.0, self._grace_deadline - now)

    def keep_recording(self) -> Optional[CallEndGraceCancelled]:
        """User clicked 'Keep recording': cancel countdown and don't auto-stop."""
        self._user_kept = True
        self._absent_since = None
        if self._grace_deadline is not None:
            self._grace_deadline = None
            return CallEndGraceCancelled(reason="user_kept")
        return None

    def observe(
        self,
        status: WatchedAppStatus,
        *,
        auto_stop_enabled: bool,
        now: float,
    ) -> Optional[CallEndEvent]:
        """Advance the tracker with one observation at monotonic time ``now``."""
        if status.on_microphone:
            self._seen_on_microphone = True

        if not auto_stop_enabled:
            self._absent_since = None
            if self._grace_deadline is not None:
                self._grace_deadline = None
                return CallEndGraceCancelled(reason="disabled")
            return None

        if self._user_kept:
            return None

        if status.present:
            self._absent_since = None
            if self._grace_deadline is not None:
                self._grace_deadline = None
                return CallEndGraceCancelled(reason="resumed")
            return None

        # App is absent (neither on mic nor playing audio, and read succeeded).
        if not self._seen_on_microphone:
            return None

        if self._grace_deadline is not None:
            if now >= self._grace_deadline:
                self._grace_deadline = None
                self._absent_since = None
                return CallEndStopDue(app_label=self.app_label, app_id=self.app_id)
            return None

        if self._absent_since is None:
            self._absent_since = now
            return None

        if (now - self._absent_since) >= self._absence_threshold_s:
            self._grace_deadline = now + self._grace_s
            return CallEndGraceStarted(
                app_label=self.app_label,
                app_id=self.app_id,
                deadline=self._grace_deadline,
                grace_s=self._grace_s,
            )

        return None
