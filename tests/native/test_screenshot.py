"""Capture an actual X display and exercise the native screenshot protocol."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_x11_capture_choose_annotate_crop_and_keep(tmp_path):
    executable = shutil.which("Xvfb")
    if executable is None:
        pytest.skip("Xvfb is required to verify a real screen capture")
    server = subprocess.Popen([executable, "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    displays = queue.Queue()
    threading.Thread(target=lambda: displays.put(server.stdout.readline().strip()), daemon=True).start()
    try:
        display = displays.get(timeout=5)
        assert display, server.stderr.read()
        code = r'''
import json, sys
from pathlib import Path
class NoGui:
    def find_spec(self, name, *_args):
        if name.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('Forbidden GUI import: ' + name)
sys.meta_path.insert(0, NoGui())
from Xlib import X
from Xlib.display import Display
from PIL import Image
from services.desktop_runtime import QCoreApplication, QTimer
from services.native_desktop.commands import dispatch
from services.native_desktop.snap import NativeSnapSession, _step_ink
from services.native_desktop.ui import NativeUIController
from types import SimpleNamespace
connection = Display()
root = connection.screen().root
window = root.create_window(100, 120, 620, 450, 0, connection.screen().root_depth,
    X.InputOutput, X.CopyFromParent, background_pixel=0xffffff)
window.set_wm_name('Native screenshot capture fixture')
window.map()
connection.sync()
app = QCoreApplication([])
events, kept, failures = [], [], []
ui = None
def receive(kind, data):
    events.append(kind)
    if kind != 'screenshot.choose':
        return
    assert data['width'] == 1280 and data['height'] == 800, data
    match = next(value for value in data['windows'] if value['title'] == 'Native screenshot capture fixture')
    assert (match['x'], match['y'], match['width'], match['height']) == (100, 120, 620, 450), match
    with Image.open(data['image_path']) as source:
        assert source.getpixel((105, 125))[:3] == (255, 255, 255)
    annotations = [
        {'tool': 'arrow', 'points': [{'x': 140, 'y': 160}, {'x': 245, 'y': 205}], 'color': '#FF4F3A', 'width': 4},
        {'tool': 'rect', 'points': [{'x': 280, 'y': 155}, {'x': 410, 'y': 235}], 'color': '#3A7BFD', 'width': 4},
        {'tool': 'freehand', 'points': [{'x': 450, 'y': 160}, {'x': 480, 'y': 190}, {'x': 520, 'y': 155}], 'color': '#1FA463', 'width': 4},
        {'tool': 'highlight', 'points': [{'x': 145, 'y': 280}, {'x': 220, 'y': 295}, {'x': 270, 'y': 280}], 'color': '#F5A524', 'width': 18},
        {'tool': 'text', 'points': [{'x': 320, 'y': 280}], 'color': '#FF4F3A', 'width': 4, 'font_size': 24, 'text': 'Native capture'},
        {'tool': 'ellipse', 'points': [{'x': 140, 'y': 370}, {'x': 260, 'y': 450}], 'color': '#3A7BFD', 'width': 4},
        {'tool': 'step', 'points': [{'x': 360, 'y': 410}], 'color': '#F5A524', 'width': 4, 'font_size': 22, 'radius': 24, 'text': '1'},
    ]
    result = dispatch(SimpleNamespace(ui=ui), 'screenshot.complete', {'id': data['id'],
        'region': {'x': 110, 'y': 130, 'width': 580, 'height': 410}, 'annotations': annotations})
    assert result == {'ok': True}
ui = NativeUIController(receive)
session = NativeSnapSession(ui, take='region')
def on_kept(image):
    destination = Path(sys.argv[1]) / 'captured-and-annotated.png'
    assert image.save(destination, 'PNG')
    with Image.open(destination) as edited:
        assert edited.size == (580, 410)
        assert edited.getpixel((170, 25)) == (58, 123, 253)
        assert edited.getpixel((5, 5)) == (255, 255, 255)
        assert edited.getpixel((250, 280)) != (255, 255, 255)
    kept.append(str(destination))
    app.quit()
session.kept.connect(on_kept)
session.failed.connect(lambda message: (failures.append(message), app.exit(2)))
QTimer.singleShot(8000, lambda: app.exit(3))
assert app.exec() == 0, failures
assert kept and not session.active and session.id not in ui._snap_sessions
assert _step_ink('#F5A524') == 'black' and _step_ink('#FF4F3A') == 'white'
assert 'screenshot.capture' in events and 'screenshot.choose' in events
ui.cleanup()
window.destroy()
connection.close()
print(json.dumps({'source': [1280, 800], 'crop': [580, 410], 'window': [100, 120, 620, 450], 'tools': 7, 'output': kept[0]}))
'''
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
            env=dict(os.environ, DISPLAY=f":{display}", OPENWHISPER_DESKTOP_RUNTIME="native",
                     OPENWHISPER_DATA_DIR=str(tmp_path / "data")), capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert Path(report["output"]).is_file()
        assert report["tools"] == 7
    finally:
        server.terminate()
        server.wait(timeout=5)
