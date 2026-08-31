"""Static accessibility guardrails for the dependency-free Meeting dashboard."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "webui" / "src"


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_keyboard_focus_is_visible_and_motion_can_be_reduced():
    styles = _source("styles.css")

    assert "button:focus-visible" in styles
    assert "summary:focus-visible" in styles
    assert "outline: 3px solid var(--leaf)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_history_and_timeline_mouse_actions_are_native_buttons():
    history = _source("components/HistoryPane.tsx")
    ribbon = _source("components/report/RibbonReport.tsx")

    assert 'aria-label="Search meeting transcripts"' in history
    assert '<li key={m.id}>\n                    <button' in history
    assert 'className={`history-item${' in history
    assert '<button\n                  key={`${card}-${item.id}`}' in ribbon
    assert "aria-label={`${card === 'decisions'" in ribbon


def test_errors_dialogs_and_loading_states_are_announced():
    app = _source("App.tsx")
    dialog = _source("components/ConfirmDialog.tsx")
    activity = _source("components/ActivityPane.tsx")

    assert 'className="error-screen" role="alert"' in app
    assert 'role="status" aria-live="polite"' in app
    assert "role={danger ? 'alertdialog' : 'dialog'}" in dialog
    assert 'aria-modal="true"' in dialog
    assert 'className="activity-error" role="alert"' in activity
