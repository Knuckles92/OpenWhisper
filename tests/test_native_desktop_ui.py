"""Verify the native presentation contract in a process where Qt is forbidden."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = """
import sys
class NoGui:
    def find_spec(self, fullname, *_args):
        if fullname.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('Forbidden GUI import: ' + fullname)
sys.meta_path.insert(0, NoGui())
"""


def run_native(source, tmp_path):
    env = dict(os.environ, OPENWHISPER_DESKTOP_RUNTIME="native",
               OPENWHISPER_DATA_DIR=str(tmp_path), HF_HUB_OFFLINE="1")
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(GUARD) + textwrap.dedent(source)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_adapter_preserves_service_results_and_live_state(tmp_path):
    run_native("""
        from services.desktop_runtime import QCoreApplication
        from services.native_desktop.ui import NativeUIController
        from services.overlay_state import OverlayState
        app = QCoreApplication([])
        events = []
        ui = NativeUIController(lambda kind, data: events.append((kind, data)))
        ui.set_status('Listening')
        ui.on_dictation_started()
        ui.main_window.is_recording = True
        ui.main_window._update_recording_state()
        ui.set_overlay_state(OverlayState.RECORDING)
        ui.update_audio_levels([0.2, 0.9])
        ui.main_window.set_partial_transcription('Draft words', False)
        ui.set_transcript('Finished words.', raw='finished words')
        ui.set_transcription_stats(1.2, 3.0, 1400)
        ui.on_model_download_started('base')
        ui.on_model_download_progress('base', 40, 100)
        assert ui.state['recording'] is True
        assert ui.state['overlay_state'] == 'recording'
        assert len(ui.state['levels']) == 49 and ui.state['levels'][-1] == 0.9
        assert ui.state['transcript'] == 'Finished words.'
        assert ui.state['raw'] == 'finished words' and not ui.state['preview']
        assert ui.state['stats']['audio_duration'] == 3
        assert ui.state['model_downloads']['base']['done'] == 40
        ui.on_meeting_live_event('segments', {'items': [{'id': 1, 'text': 'Hello'}]})
        ui.on_meeting_live_event('segments', {'items': [{'id': 1, 'text': 'Hello again'}, {'id': 2, 'text': 'Second'}]})
        ui.on_meeting_live_event('segments', {'items': [], 'removed_ids': [2]})
        assert ui.state['meeting_live']['transcript'] == 'Hello again'
        assert any(kind == 'transcription.result' for kind, _ in events)
        assert any(kind == 'meeting.live' for kind, _ in events)
        selected = []
        ui.on_model_changed = selected.append
        from config import config
        display = next(key for key, value in config.MODEL_VALUE_MAP.items() if value == 'api')
        ui.select_transcription_backend(display)
        assert selected == ['api'] and ui.state['engine'] == 'api'
        assert ui.insert_into_scratchpad('unfocused') is False
        ui.state['scratchpad_focused'] = True
        assert ui.insert_into_scratchpad('still unfocused') is False
        ui.state['window_active'] = True
        assert ui.insert_into_scratchpad('first') is True
        assert ui.insert_into_scratchpad('second') is True
        assert ui.state['scratchpad'] == 'first\\nsecond'
        ui.cleanup()
    """, tmp_path)


def test_native_screenshot_crops_and_renders_annotations(tmp_path):
    run_native("""
        from pathlib import Path
        from PIL import Image
        from config import data_root
        from services.native_desktop.snap import CapturedImage, render_capture
        root = Path(data_root())
        source, result = root / 'source.png', root / 'edited.png'
        Image.new('RGB', (200, 100), 'white').save(source)
        render_capture(source, result, {'x': 20, 'y': 10, 'width': 100, 'height': 60}, [
            {'tool': 'rect', 'points': [{'x': 25, 'y': 15}, {'x': 70, 'y': 50}], 'color': '#FF0000', 'width': 3},
            {'tool': 'arrow', 'points': [{'x': 80, 'y': 20}, {'x': 110, 'y': 60}], 'color': '#0000FF', 'width': 2},
            {'tool': 'step', 'points': [{'x': 95, 'y': 35}], 'color': '#1FA463', 'width': 2, 'text': '1'},
        ])
        with Image.open(result) as image:
            assert image.size == (100, 60)
            assert image.getpixel((5, 5)) == (255, 0, 0)
            assert image.getpixel((99, 0)) == (255, 255, 255)
        kept = root / 'kept.png'
        assert CapturedImage(result).copy().save(kept, 'PNG')
        for region in ({'x': 0, 'y': 0, 'width': 0, 'height': 20}, {'x': float('nan'), 'y': 0, 'width': 20, 'height': 20}):
            try:
                render_capture(source, result, region, [])
            except ValueError:
                pass
            else:
                raise AssertionError('invalid crop accepted')
        from services.desktop_runtime import QCoreApplication
        from services.native_desktop.snap import NativeSnapSession
        from services.native_desktop.ui import NativeUIController
        app = QCoreApplication([])
        events, kept = [], []
        ui = NativeUIController(lambda kind, data: events.append((kind, data)))
        session = NativeSnapSession(ui, take='region')
        session.kept.connect(kept.append)
        session._choose(CapturedImage(source), '')
        choice = next(data for kind, data in events if kind == 'screenshot.choose')
        assert choice['id'] == session.id and choice['width'] == 200
        session.complete({'region': {'x': 10, 'y': 5, 'width': 80, 'height': 30}, 'annotations': []})
        assert not session.active and session.id not in ui._snap_sessions
        with Image.open(kept[0].path) as image:
            assert image.size == (80, 30)
        ui.cleanup()
    """, tmp_path)


def test_multidisplay_capture_frame_matches_window_pixels_at_mixed_density(tmp_path):
    run_native("""
        from pathlib import Path
        from PIL import Image
        from config import data_root
        from services.desktop_runtime import QCoreApplication
        from services.native_desktop import snap
        from services.native_desktop.ui import NativeUIController
        root = Path(data_root())
        low, retina, combined = root / 'low.png', root / 'retina.png', root / 'combined.png'
        Image.new('RGB', (100, 100), 'red').save(low)
        Image.new('RGB', (400, 200), 'blue').save(retina)
        screens = [
            {'x': -100, 'y': -50, 'width': 100, 'height': 100, 'path': low},
            {'x': 0, 'y': 0, 'width': 200, 'height': 100, 'path': retina},
        ]
        origin, size = snap._compose_displays(screens, combined)
        assert origin == (-100, -50) and size == (300, 150)
        with Image.open(combined) as image:
            assert image.size == (600, 300)
            assert image.getpixel((0, 0)) == (255, 0, 0)
            assert image.getpixel((200, 100)) == (0, 0, 255)
            assert image.getpixel((200, 0)) == (0, 0, 0)
        snap.window_choices = lambda: ([{'id': 'window', 'title': 'Retina window',
            'x': 10, 'y': 5, 'width': 40, 'height': 30}], origin, size)
        app = QCoreApplication([])
        events = []
        ui = NativeUIController(lambda event, data: events.append((event, data)))
        session = snap.NativeSnapSession(ui, take='window')
        session._choose(snap.CapturedImage(combined), '')
        choice = next(data for event, data in events if event == 'screenshot.choose')
        window = choice['windows'][0]
        assert (window['x'], window['y'], window['width'], window['height']) == (220, 110, 80, 60)
        assert choice['width'] == 600 and choice['height'] == 300
        ui.cleanup()
    """, tmp_path)


def test_native_prompt_answers_keep_owner_loop_responsive(tmp_path):
    run_native("""
        from services.desktop_runtime import QCoreApplication, QTimer
        from services.native_desktop.ui import NativeUIController
        app = QCoreApplication([])
        events = []
        ui = NativeUIController(lambda kind, data: events.append((kind, data)))
        ticks = []
        QTimer.singleShot(1, lambda: ticks.append('responsive'))
        def answer():
            prompt = ui.state['prompts'][0]
            try:
                ui.answer_prompt(prompt['id'], 'invalid')
            except ValueError:
                pass
            else:
                raise AssertionError('invalid answer accepted')
            ui.answer_prompt(prompt['id'], True)
        QTimer.singleShot(3, answer)
        accepted = ui.request_prompt('test', 'Continue?', 'A real choice',
            [{'label': 'Yes', 'value': True}, {'label': 'No', 'value': False}], default=False)
        assert accepted is True and ticks == ['responsive']
        assert not ui.state['prompts']
        assert [kind for kind, _ in events] == ['prompt', 'prompt.closed']
        ui.cancel_prompts()
        assert ui.request_prompt('test', 'Closed', '', [], default='closed') == 'closed'
    """, tmp_path)


def test_programmatic_scratchpad_answers_and_focused_dictation_are_persisted(tmp_path):
    run_native("""
        from pathlib import Path
        from config import config
        from services.desktop_runtime import QCoreApplication
        from services.native_desktop.ui import NativeUIController
        from services.native_desktop.scratchpad import ScratchpadStore
        app = QCoreApplication([])
        path = Path(config.SCRATCHPAD_FILE)
        path.write_text('Earlier saved notes', encoding='utf-8')
        ui = NativeUIController(lambda *_args: None)
        assert ui.state['scratchpad'] == 'Earlier saved notes'
        ui.show_in_scratchpad('An action result — 日本語')
        ui.state.update(window_active=True, scratchpad_focused=True)
        assert ui.insert_into_scratchpad('Spoken words')
        ui.cleanup()
        restored = ScratchpadStore(path)
        assert restored.text == 'An action result — 日本語\\nSpoken words'
        assert restored.close()
    """, tmp_path)


def test_real_controller_constructs_and_cleans_up_without_gui(tmp_path):
    run_native("""
        from services.desktop_runtime import QCoreApplication
        from services.application_controller import ApplicationController
        from services.native_desktop.ui import NativeUIController
        from transcriber import LocalWhisperBackend
        app = QCoreApplication([])
        ui = NativeUIController(lambda *_args: None)
        controller = ApplicationController(ui, local_backend=LocalWhisperBackend(load=False))
        try:
            assert controller.transcription_runtime.controller is controller
            assert controller.meeting_runtime.controller is controller
            assert ui.on_record_start == controller.start_recording
            assert ui.on_meeting_start == controller.meeting_runtime.start_meeting
        finally:
            controller.cleanup()
        assert not any(n.startswith(('PyQt6', 'ui_qt')) for n in sys.modules)
    """, tmp_path)


def test_audio_capture_callback_never_writes_transport_and_coalesces_meter_updates(tmp_path):
    run_native("""
        import threading
        from services.desktop_runtime import QCoreApplication
        from services.native_desktop.ui import NativeUIController
        app = QCoreApplication([])
        owner = threading.get_ident()
        events = []
        def emit(kind, payload):
            assert threading.get_ident() == owner
            events.append((kind, payload))
        ui = NativeUIController(emit)
        def capture_callback():
            for level in (0.1, 0.3, 0.9):
                ui.update_audio_levels([level] * 20)
        worker = threading.Thread(target=capture_callback)
        worker.start()
        worker.join(1)
        assert not worker.is_alive()
        assert events == [] and ui.state['levels'][-1] == 0
        app.processEvents()
        assert events == [('audio.level', {'level': 0.9})]
        assert ui.state['levels'][-1] == 0.9
        ui.cleanup()
    """, tmp_path)
