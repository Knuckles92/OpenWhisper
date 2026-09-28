"""Qt tests for the Settings picker of who runs Meeting Mode's AI insights.

The picker never runs an agent here: every test patches the scan and model
lookups in ``ui_qt.widgets.agent_picker`` (conftest stubs them otherwise).
"""
import os
import threading
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.installed_agents import AgentModel, InstalledAgent
from services.settings import MeetingAgentCore
from ui_qt.widgets import agent_picker
from ui_qt.widgets.agent_picker import (
    BUILTIN,
    FILTER_THRESHOLD,
    FOUND,
    MISSING,
    STAGGER_MS,
    WARNING,
    AgentPicker,
    blocked_notice,
    command_text,
    describe_agent,
    found_count_text,
    usage_caption,
)

CLAUDE_CODE = MeetingAgentCore.CLAUDE_CODE
CODEX = MeetingAgentCore.CODEX
OPENCODE = MeetingAgentCore.OPENCODE

CLAUDE = InstalledAgent(CLAUDE_CODE, "C:/bin/claude.exe", "2.1.281", "Claude Team", True)
CODEX_AGENT = InstalledAgent(CODEX, "C:/bin/codex.exe", "0.158.0", "ChatGPT", True)
OPENCODE_AGENT = InstalledAgent(OPENCODE, "C:/bin/opencode.exe", "2.1.0")
FOUND_TWO = {CLAUDE_CODE: CLAUDE, CODEX: CODEX_AGENT, OPENCODE: None}


@pytest.fixture(autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout=3.0):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


def _models(agent):
    return {
        CLAUDE_CODE: [AgentModel("", "Claude Code default (opus)"),
                      AgentModel("haiku", "Haiku (fastest)"),
                      AgentModel("sonnet", "Sonnet")],
        CODEX: [AgentModel("", "Codex default")],
    }[agent.id]


class TestTileCopy:
    def test_found_agent_shows_version_and_account(self):
        state = describe_agent(CLAUDE_CODE, CLAUDE)
        assert (state.tone, state.pill, state.detail) == (
            FOUND, "Found", "2.1.281 · Claude Team"
        )
        assert state.selectable and state.installed

    def test_missing_agent_is_not_selectable(self):
        state = describe_agent(OPENCODE, None)
        assert (state.tone, state.pill, state.detail) == (MISSING, "Not installed", "")
        assert not state.selectable and not state.installed

    def test_problem_and_sign_in_show_amber_with_the_fix(self):
        old = InstalledAgent(CODEX, "codex", "0.100.0",
                             problem="OpenWhisper needs Codex 0.120.0 or newer. Run `codex x`.")
        state = describe_agent(CODEX, old)
        assert (state.tone, state.pill) == (WARNING, "Update needed")
        assert "\u201ccodex x\u201d" in state.detail and "`" not in state.detail
        assert not state.selectable and state.installed

        signed_out = InstalledAgent(CODEX, "codex", "0.158.0", signed_in=False)
        state = describe_agent(CODEX, signed_out)
        assert (state.tone, state.pill) == (WARNING, "Sign in needed")
        assert "codex login" in state.detail

    def test_unknown_sign_in_is_still_usable(self):
        state = describe_agent(OPENCODE, OPENCODE_AGENT)
        assert state.tone == FOUND and state.selectable
        assert state.detail == "2.1.0"

    def test_count_line(self):
        assert found_count_text(FOUND_TWO) == "Found 2 coding agents on this computer."
        assert found_count_text({CODEX: CODEX_AGENT}) == (
            "Found 1 coding agent on this computer."
        )
        assert found_count_text({}).startswith("No coding agents found")

    def test_usage_caption_names_the_sign_in_and_the_limits(self):
        text = usage_caption(CLAUDE)
        assert text.startswith("Runs through your Claude Code sign-in (Claude Team).")
        assert "counts toward that plan's usage" in text
        assert "no files, no shell" in text
        key = InstalledAgent(CLAUDE_CODE, "claude", "2.1.0", "API key", True)
        assert "billed to that key" in usage_caption(key)
        bedrock = InstalledAgent(CLAUDE_CODE, "claude", "2.1.0", "Amazon Bedrock", True)
        assert "that account's usage" in usage_caption(bedrock)
        assert "providers you signed in to in OpenCode" in usage_caption(OPENCODE_AGENT)

    def test_blocked_notice(self):
        assert "until it is installed" in blocked_notice(OPENCODE, None)
        assert blocked_notice(CLAUDE_CODE, CLAUDE) == ""
        signed_out = InstalledAgent(CODEX, "codex", "0.158.0", signed_in=False)
        assert "Sign in and choose Look again" in blocked_notice(CODEX, signed_out)

    def test_command_text(self):
        assert command_text("Run `claude update`.") == "Run \u201cclaude update\u201d."


class TestScanning:
    def test_cached_scan_shows_at_once_without_animating(self):
        with patch.object(agent_picker, "cached_agents", return_value=FOUND_TWO), \
                patch.object(agent_picker, "scan_installed_agents") as scan:
            picker = AgentPicker()
            picker.ensure_scanned()
        scan.assert_not_called()
        assert not picker.is_scanning()
        for agent_id, tone in ((CLAUDE_CODE, FOUND), (CODEX, FOUND), (OPENCODE, MISSING)):
            tile = picker.tiles[agent_id]
            assert tile.state.tone == tone
            assert tile.arrival == 1.0
            assert not tile.arrival_pending()
            assert tile.shimmer == 0.0
        assert picker.tiles[CLAUDE_CODE].mark.warmth == 1.0
        assert picker.count_label.text() == "Found 2 coding agents on this computer."

    def test_fresh_scan_shimmers_until_the_result_arrives(self):
        gate = threading.Event()
        calls = []

        def scan(refresh=False):
            calls.append(refresh)
            gate.wait(5)
            return FOUND_TWO

        with patch.object(agent_picker, "scan_installed_agents", side_effect=scan):
            picker = AgentPicker()
            picker.ensure_scanned()
            assert picker.is_scanning()
            assert not picker.look_again_button.isEnabled()
            assert "Looking for coding agents" in picker.count_label.text()
            for agent_id in (CLAUDE_CODE, CODEX, OPENCODE):
                tile = picker.tiles[agent_id]
                assert tile.scanning and tile.shimmer == 1.0
                assert tile.pill.text() == "Looking…"
            # The built-in tile has nothing to look for.
            assert not picker.tiles[BUILTIN].scanning
            picker.ensure_scanned()  # a second open does not start another
            gate.set()
            assert _pump_until(lambda: not picker.is_scanning())
        assert calls == [False]
        assert picker.look_again_button.isEnabled()
        assert not picker.tiles[CLAUDE_CODE].scanning
        assert picker.agent(CLAUDE_CODE) == CLAUDE

    def test_fresh_result_lights_tiles_up_one_after_another(self):
        picker = AgentPicker()
        picker.start_scan(refresh=False)
        generation = picker._generation
        picker._on_scan_done(generation, FOUND_TWO)
        first, second, third = (picker.tiles[a] for a in (CLAUDE_CODE, CODEX, OPENCODE))
        assert first.state.tone == FOUND and not first.arrival_pending()
        assert first.arrival < 1.0
        # The shimmer stops for every tile the moment the scan is back.
        assert not any(tile.scanning for tile in (first, second, third))
        assert second.arrival_pending() and third.arrival_pending()
        assert second._arrival_timer.interval() == STAGGER_MS
        assert third._arrival_timer.interval() == 2 * STAGGER_MS
        assert _pump_until(lambda: third.arrival == 1.0 and not third.arrival_pending())
        assert second.state.tone == FOUND and second.mark.warmth == 1.0
        assert third.state.tone == MISSING and third.mark.missing == 1.0
        assert first.pill.text() == "Found"

    def test_look_again_scans_with_refresh(self):
        calls = []

        def scan(refresh=False):
            calls.append(refresh)
            return FOUND_TWO

        with patch.object(agent_picker, "cached_agents", return_value={}), \
                patch.object(agent_picker, "scan_installed_agents", side_effect=scan):
            picker = AgentPicker()
            picker.look_again_button.click()
            assert _pump_until(lambda: not picker.is_scanning())
        assert calls == [True]
        assert _pump_until(lambda: not picker.tiles[CODEX].arrival_pending())
        assert picker.tiles[CODEX].state.tone == FOUND

    def test_a_stale_scan_result_is_ignored(self):
        picker = AgentPicker()
        picker.start_scan(refresh=False)
        stale = picker._generation
        picker.start_scan(refresh=True)
        picker._on_scan_done(stale, FOUND_TWO)
        assert picker.is_scanning()
        assert picker.agents() is None

    def test_a_failing_scan_reads_as_nothing_found(self):
        with patch.object(agent_picker, "scan_installed_agents", side_effect=OSError("x")):
            picker = AgentPicker()
            picker.ensure_scanned()
            assert _pump_until(lambda: not picker.is_scanning())
        tiles = [picker.tiles[a] for a in (CLAUDE_CODE, CODEX, OPENCODE)]
        assert _pump_until(lambda: not any(tile.arrival_pending() for tile in tiles))
        assert all(tile.state.tone == MISSING for tile in tiles)
        assert picker.count_label.text().startswith("No coding agents found")


class TestChoosing:
    @pytest.fixture
    def picker(self):
        with patch.object(agent_picker, "cached_agents", return_value=FOUND_TWO), \
                patch.object(agent_picker, "list_agent_models", side_effect=_models):
            picker = AgentPicker()
            picker.resize(900, 400)
            yield picker

    def test_found_tile_click_requests_that_agent(self, picker):
        requested = []
        picker.choice_requested.connect(requested.append)
        tile = picker.tiles[CODEX]
        QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        assert requested == [CODEX]

    def test_missing_tile_click_does_nothing(self, picker):
        requested = []
        picker.choice_requested.connect(requested.append)
        tile = picker.tiles[OPENCODE]
        QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        assert requested == []
        assert tile.focusPolicy() == Qt.FocusPolicy.NoFocus

    def test_space_on_a_found_tile_requests_it(self, picker):
        requested = []
        picker.choice_requested.connect(requested.append)
        QTest.keyClick(picker.tiles[CLAUDE_CODE], Qt.Key.Key_Space)
        assert requested == [CLAUDE_CODE]

    def test_clicking_the_chosen_tile_again_does_nothing(self, picker):
        requested = []
        picker.choice_requested.connect(requested.append)
        QTest.mouseClick(picker.tiles[BUILTIN], Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        assert requested == []

    def test_get_link_opens_the_install_page(self, picker):
        tile = picker.tiles[OPENCODE]
        assert not tile.link.isHidden()
        assert tile.link.toolTip() == "npm install -g opencode-ai"
        with patch.object(agent_picker.QDesktopServices, "openUrl") as open_url:
            tile.link.click()
        assert open_url.call_args.args[0].toString() == "https://opencode.ai/docs/"
        assert picker.tiles[CLAUDE_CODE].link.isHidden()

    def test_choosing_an_agent_shows_its_models_and_sign_in(self, picker):
        picker.set_saved_models({CLAUDE_CODE: "sonnet", CODEX: "gpt-x"})
        picker.set_choice(CLAUDE_CODE)
        assert picker.tiles[CLAUDE_CODE].selected
        assert not picker.tiles[BUILTIN].selected
        assert not picker.model_card.isHidden()
        combo = picker.model_combo()
        assert combo is picker.short_model_combo
        assert combo.itemText(0) == "Claude Code default (opus)"
        assert combo.currentData() == "sonnet"
        assert "Claude Team" in picker.usage_label.text()
        assert picker.notice.isHidden()

        chosen = []
        picker.model_chosen.connect(lambda agent_id, model: chosen.append((agent_id, model)))
        combo.activated.emit(combo.findData("haiku"))
        assert chosen == [(CLAUDE_CODE, "haiku")]
        combo.activated.emit(combo.findData("haiku"))
        assert chosen == [(CLAUDE_CODE, "haiku")]

    def test_a_saved_model_the_agent_no_longer_lists_is_kept(self, picker):
        picker.set_saved_models({CODEX: "gpt-retired"})
        picker.set_choice(CODEX)
        combo = picker.model_combo()
        assert combo.currentData() == "gpt-retired"
        assert combo.itemText(0) == "Codex default"

    def test_built_in_choice_hides_the_model_row(self, picker):
        picker.set_choice(MeetingAgentCore.DIRECT)
        assert picker.choice() == BUILTIN
        assert picker.tiles[BUILTIN].selected
        assert picker.model_card.isHidden()

    def test_a_chosen_agent_that_is_signed_out_says_why(self):
        signed_out = InstalledAgent(CODEX, "codex", "0.158.0", signed_in=False)
        with patch.object(agent_picker, "cached_agents",
                          return_value={CLAUDE_CODE: CLAUDE, CODEX: signed_out}):
            picker = AgentPicker()
        picker.set_choice(CODEX)
        tile = picker.tiles[CODEX]
        assert tile.selected and tile.state.tone == WARNING
        assert tile.pill.text() == "Sign in needed"
        assert not picker.notice.isHidden()
        assert picker.model_card.isHidden()


class TestOpenCodeModels:
    def test_models_load_off_the_ui_thread_and_filter_when_long(self):
        gate = threading.Event()
        threads = []
        many = [AgentModel("", "OpenCode default")] + [
            AgentModel(f"provider/model-{i}", f"Model {i}") for i in range(FILTER_THRESHOLD + 10)
        ]

        def list_models(agent):
            threads.append(threading.current_thread() is threading.main_thread())
            gate.wait(5)
            return many

        scan = {CLAUDE_CODE: None, CODEX: None, OPENCODE: OPENCODE_AGENT}
        with patch.object(agent_picker, "cached_agents", return_value=scan), \
                patch.object(agent_picker, "list_agent_models", side_effect=list_models):
            picker = AgentPicker()
            picker.set_saved_models({OPENCODE: "provider/model-3"})
            picker.set_choice(OPENCODE)
            assert picker.model_status.text() == "Loading OpenCode's models…"
            assert not picker.model_status.isHidden()
            assert not picker.short_model_combo.isEnabled()
            gate.set()
            assert _pump_until(lambda: picker.model_combo() is picker.long_model_combo)
        assert threads == [False]
        combo = picker.long_model_combo
        assert not combo.isHidden() and picker.short_model_combo.isHidden()
        assert combo.count() == len(many)
        assert combo.currentData() == "provider/model-3"
        assert picker.model_status.isHidden()

        chosen = []
        picker.model_chosen.connect(lambda agent_id, model: chosen.append(model))
        combo.activated.emit(combo.findData("provider/model-7"))
        assert chosen == ["provider/model-7"]
        # A filter fragment left in the editor reverts to the choice.
        combo.lineEdit().setText("mod")
        combo.lineEdit().editingFinished.emit()
        assert combo.currentText() == "Model 7"

    def test_an_empty_model_list_says_opencode_uses_its_default(self):
        scan = {OPENCODE: OPENCODE_AGENT}
        with patch.object(agent_picker, "cached_agents", return_value=scan), \
                patch.object(agent_picker, "list_agent_models",
                             return_value=[AgentModel("", "OpenCode default")]):
            picker = AgentPicker()
            picker.set_choice(OPENCODE)
            assert _pump_until(lambda: "didn't list" in picker.model_status.text())
        assert picker.model_combo().count() == 1


class TestLayout:
    def test_tiles_wrap_two_by_two_when_narrow(self):
        with patch.object(agent_picker, "cached_agents", return_value=FOUND_TWO):
            picker = AgentPicker()
        picker.resize(1000, 400)
        picker.show()
        _pump_until(lambda: False, timeout=0.05)
        assert picker.grid.columns() == 4
        tiles = [picker.tiles[a] for a in (CLAUDE_CODE, CODEX, OPENCODE, BUILTIN)]
        assert len({tile.y() for tile in tiles}) == 1
        picker.resize(480, 600)
        _pump_until(lambda: False, timeout=0.05)
        assert picker.grid.columns() == 2
        assert tiles[0].y() == tiles[1].y() < tiles[2].y() == tiles[3].y()
        assert picker.grid.height() >= tiles[3].geometry().bottom() - 8
        picker.close()

    def test_a_long_problem_grows_its_row_instead_of_overlapping(self):
        old = InstalledAgent(
            OPENCODE, "opencode", "1.4.2",
            problem="OpenWhisper needs OpenCode 2.0.0 or newer. Run `opencode upgrade`.",
        )
        with patch.object(agent_picker, "cached_agents",
                          return_value={CLAUDE_CODE: CLAUDE, CODEX: CODEX_AGENT, OPENCODE: old}):
            picker = AgentPicker()
        # Just wide enough for one row: the text wraps more than it would at
        # the label's preferred width.
        picker.resize(4 * picker.grid.MIN_CARD + 3 * picker.grid.GAP + 10, 400)
        picker.show()
        _pump_until(lambda: False, timeout=0.1)
        assert picker.grid.columns() == 4
        tile = picker.tiles[OPENCODE]
        detail = tile.detail_label
        assert detail.heightForWidth(detail.width()) > detail.fontMetrics().height()
        assert tile.name_label.geometry().bottom() < detail.geometry().top()
        assert detail.geometry().bottom() <= tile.card_rect().bottom()
        assert detail.height() >= detail.heightForWidth(detail.width())
        # Every tile in the row takes the tallest one's height.
        heights = {picker.tiles[a].height() for a in (CLAUDE_CODE, CODEX, OPENCODE, BUILTIN)}
        assert len(heights) == 1
        picker.close()
