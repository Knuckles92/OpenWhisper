"""Screenshot sessions using the host desktop's capture tools, without Qt."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from services.desktop_runtime import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


def _step_ink(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return "black" if sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear, strict=True)) >= 0.3 else "white"


@contextmanager
def _windows_capture_coordinates():
    """Read both capture pixels and window rectangles in physical coordinates."""
    previous = setter = None
    if sys.platform == "win32":
        try:
            import ctypes
            setter = ctypes.windll.user32.SetThreadDpiAwarenessContext
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
            previous = setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            setter = None
    try:
        yield
    finally:
        if setter is not None and previous:
            setter(previous)


def _layout_bounds(screens):
    left = min(screen["x"] for screen in screens)
    top = min(screen["y"] for screen in screens)
    right = max(screen["x"] + screen["width"] for screen in screens)
    bottom = max(screen["y"] + screen["height"] for screen in screens)
    return (left, top), (right - left, bottom - top)


def _mac_displays():
    import Quartz
    error, displays, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if error or not count:
        displays = [Quartz.CGMainDisplayID()]
    result = []
    for display in displays:
        rect = Quartz.CGDisplayBounds(display)
        result.append({"x": rect.origin.x, "y": rect.origin.y,
            "width": rect.size.width, "height": rect.size.height})
    return result


def _compose_displays(screens, destination):
    """Combine mixed-density screen PNGs at one physical-pixel scale."""
    from PIL import Image
    origin, size = _layout_bounds(screens)
    images = []
    for screen in screens:
        with Image.open(screen["path"]) as image:
            images.append(image.convert("RGB"))
    scale = max(image.width / screen["width"] for image, screen in zip(images, screens, strict=True))
    combined = Image.new("RGB", (round(size[0] * scale), round(size[1] * scale)))
    for screen, image in zip(screens, images, strict=True):
        target = (round(screen["width"] * scale), round(screen["height"] * scale))
        if image.size != target:
            image = image.resize(target, Image.Resampling.LANCZOS)
        combined.paste(image, (round((screen["x"] - origin[0]) * scale),
                               round((screen["y"] - origin[1]) * scale)))
    combined.save(destination, "PNG")
    return origin, size


@dataclass
class CapturedImage:
    """PNG-backed image accepted by walkthrough storage and portable exports."""
    path: Path

    def copy(self):
        return CapturedImage(self.path)

    def isNull(self):
        return not self.path.is_file()

    def save(self, destination, _format="PNG"):
        try:
            if self.path.resolve() != Path(destination).resolve():
                shutil.copyfile(self.path, destination)
            return True
        except OSError:
            return False


def _linux_tool():
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("grim"):
        return "grim"
    for executable in ("gnome-screenshot", "spectacle", "scrot", "grim"):
        if shutil.which(executable):
            return executable
    return None


def snap_support():
    if sys.platform == "darwin":
        try:
            import Quartz
            if not Quartz.CGPreflightScreenCaptureAccess():
                return False, "Allow Screen Recording access for OpenWhisper to take screenshots."
        except ImportError:
            pass
    if sys.platform in ("win32", "darwin") or _linux_tool():
        return True, ""
    if os.environ.get("DISPLAY"):
        return True, ""
    return False, "Install a desktop screenshot tool (grim, gnome-screenshot, spectacle, or scrot) to capture screenshots."


class NativeSnapSession(QObject):
    kept = pyqtSignal(object)
    dropped = pyqtSignal()
    failed = pyqtSignal(str)
    _captured = pyqtSignal(object, str)

    def __init__(self, ui, *, hint="", take="region", front=False):
        super().__init__()
        self.ui = ui
        self.take = take
        self.front = front
        self.id = uuid4().hex
        self.image = None
        self.width = self.height = 0
        self.active = True
        self._cancel = threading.Event()
        self._process = None
        self._capture_bounds = None
        self._captured.connect(self._choose)
        if not hasattr(ui, "_snap_sessions"):
            ui._snap_sessions = {}
        ui._snap_sessions[self.id] = self
        self.ui.emit("screenshot.capture", {"active": True, "take": take, "hint": hint})
        # Callers connect kept/dropped/failed immediately after construction.
        QTimer.singleShot(250, self._start)

    def _start(self):
        if not self.active:
            return
        threading.Thread(target=self._capture, name="native-screenshot", daemon=True).start()

    def _capture(self):
        path = Path(tempfile.mkdtemp(prefix="openwhisper-snap-")) / "screen.png"
        error = ""
        try:
            command = self._command(path)
            if sys.platform == "darwin":
                screens = _mac_displays()
                for index, screen in enumerate(screens, start=1):
                    if self._cancel.is_set():
                        raise RuntimeError("Screenshot canceled")
                    screen["path"] = path.with_name(f"display-{index}.png")
                    self._process = subprocess.Popen(["screencapture", "-x", "-D", str(index), str(screen["path"])],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    _output, errors = self._process.communicate(timeout=120)
                    if self._process.returncode:
                        raise RuntimeError(errors.decode("utf-8", "replace").strip() or "Screen capture failed")
                self._capture_bounds = _compose_displays(screens, path)
            elif command:
                self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                _output, errors = self._process.communicate(timeout=120)
                if self._process.returncode and not self._cancel.is_set():
                    error = (errors.decode("utf-8", "replace").strip() or "Screenshot canceled")[:500]
            else:
                from PIL import ImageGrab
                with _windows_capture_coordinates():
                    ImageGrab.grab(all_screens=True).save(path, "PNG")
            if not error and path.is_file():
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
        except Exception as exc:
            error = str(exc)
        self._captured.emit(CapturedImage(path) if path.is_file() else None, error)

    def _command(self, path):
        if sys.platform == "darwin":
            return ["screencapture", "-x", str(path)]
        if sys.platform != "linux":
            return None
        tool = _linux_tool()
        if tool == "gnome-screenshot":
            return [tool, "-f", str(path)]
        if tool == "spectacle":
            return [tool, "-b", "-n", "-f", "-o", str(path)]
        if tool == "scrot":
            return [tool, str(path)]
        if tool == "grim":
            return [tool, str(path)]
        return None

    def _choose(self, image, error):
        if self._cancel.is_set() or image is None or error:
            self._finish(image, error)
            return
        from PIL import Image
        self.image = image
        with Image.open(image.path) as source:
            self.width, self.height = source.size
        windows, origin, desktop_size = window_choices()
        if self._capture_bounds is not None:
            origin, desktop_size = self._capture_bounds
        scale_x = self.width / desktop_size[0] if desktop_size[0] else 1
        scale_y = self.height / desktop_size[1] if desktop_size[1] else 1
        for window in windows:
            window.update(x=(window["x"] - origin[0]) * scale_x,
                          y=(window["y"] - origin[1]) * scale_y,
                          width=window["width"] * scale_x, height=window["height"] * scale_y)
        self.ui.emit("screenshot.choose", {"id": self.id, "image_path": str(image.path),
            "width": self.width, "height": self.height, "mode": self.take, "windows": windows})

    def complete(self, params):
        if not self.active or self.image is None:
            raise ValueError("This screenshot is no longer open.")
        if params.get("cancelled") is True:
            self.cancel()
            return
        destination = self.image.path.with_name("edited.png")
        render_capture(self.image.path, destination, params.get("region"), params.get("annotations", []))
        self._finish(CapturedImage(destination), "")

    def _finish(self, image, error):
        if not self.active:
            return
        self.active = False
        self.ui._snap_sessions.pop(self.id, None)
        self.ui.emit("screenshot.capture", {"active": False})
        if self._cancel.is_set() or (image is None and not error) or error == "Screenshot canceled":
            self.dropped.emit()
        elif error:
            self.failed.emit(error)
        else:
            self.kept.emit(image)

    def keep(self):
        self.ui.emit("screenshot.keep", {"id": self.id})

    def cancel(self):
        self._cancel.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._finish(None, "")

    def close(self):
        self.cancel()

    def drop(self):
        self.cancel()


def render_capture(source, destination, region, annotations):
    """Render original-image pixel coordinates, then crop the resulting PNG."""
    from PIL import Image, ImageDraw, ImageFont

    if not isinstance(annotations, list) or len(annotations) > 500:
        raise ValueError("Too many screenshot annotations.")
    with Image.open(source) as incoming:
        image = incoming.convert("RGBA")
    width, height = image.size

    def coordinate(value, maximum):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid screenshot coordinates.")
        return max(0, min(maximum, round(value)))

    if region is None:
        box = (0, 0, width, height)
    else:
        if not isinstance(region, dict):
            raise ValueError("Choose a screenshot region.")
        x = coordinate(region.get("x"), width)
        y = coordinate(region.get("y"), height)
        right = coordinate(x + coordinate(region.get("width"), width), width)
        bottom = coordinate(y + coordinate(region.get("height"), height), height)
        if right <= x or bottom <= y:
            raise ValueError("Choose a non-empty screenshot region.")
        box = (x, y, right, bottom)
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise ValueError("Invalid screenshot annotation.")
        tool = annotation.get("tool")
        if tool not in ("arrow", "rect", "rectangle", "freehand", "text", "highlight", "ellipse", "step"):
            raise ValueError("Unknown screenshot annotation tool.")
        color = annotation.get("color", "#EF4444")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            raise ValueError("Choose a valid annotation color.")
        weight = annotation.get("width", 3)
        if type(weight) not in (float, int) or not math.isfinite(weight) or not 1 <= weight <= 200:
            raise ValueError("Choose an annotation width between 1 and 200 pixels.")
        weight = round(weight)
        raw_points = annotation.get("points")
        if not isinstance(raw_points, list) or not 1 <= len(raw_points) <= 50_000:
            raise ValueError("Invalid annotation points.")
        points = []
        for point in raw_points:
            if not isinstance(point, dict):
                raise ValueError("Invalid annotation point.")
            points.append((coordinate(point.get("x"), width), coordinate(point.get("y"), height)))
        first, last = points[0], points[-1]
        bounds = (min(first[0], last[0]), min(first[1], last[1]),
                  max(first[0], last[0]), max(first[1], last[1]))
        if tool in ("text", "step"):
            text = annotation.get("text", "")
            if not isinstance(text, str) or len(text) > 10_000:
                raise ValueError("Screenshot text is too long.")
            font_size = annotation.get("font_size", max(14, weight * 5))
            radius = annotation.get("radius", max(14, weight * 5))
            for value in (font_size, radius):
                if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 2000:
                    raise ValueError("Choose a valid screenshot text size.")
            try:
                font_path = Path(__file__).resolve().parents[2] / "native" / "assets" / "fonts" / "Inter-Regular.ttf"
                font = ImageFont.truetype(str(font_path), round(font_size))
            except OSError:
                font = ImageFont.load_default(size=round(font_size))
            if tool == "step":
                draw.ellipse((first[0] - radius, first[1] - radius,
                              first[0] + radius, first[1] + radius), fill=color)
                draw.text(first, text, fill=_step_ink(color), font=font, anchor="mm")
            else:
                padding = max(3, round(font_size * 0.25))
                text_bounds = draw.multiline_textbbox((0, 0), text, font=font)
                label_width = text_bounds[2] - text_bounds[0] + 2 * padding
                label_height = text_bounds[3] - text_bounds[1] + 2 * padding
                draw.rounded_rectangle((first[0], first[1], first[0] + label_width,
                    first[1] + label_height), radius=max(3, padding), fill=color)
                draw.multiline_text((first[0] + padding - text_bounds[0],
                    first[1] + padding - text_bounds[1]), text, fill="white", font=font)
        elif tool in ("rect", "rectangle"):
            rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
            draw.rectangle(bounds, fill=(*rgb, 24), outline=color, width=weight)
        elif tool == "ellipse":
            draw.ellipse(bounds, outline=color, width=weight)
        elif tool == "highlight":
            rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
            if len(points) >= 2:
                draw.line(points, fill=(*rgb, 96), width=weight, joint="curve")
        else:
            if len(points) < 2:
                continue
            draw.line(points, fill=color, width=weight, joint="curve")
            if tool == "arrow":
                angle = math.atan2(last[1] - first[1], last[0] - first[0])
                size = max(10, weight * 4)
                draw.polygon([last, (last[0] - size * math.cos(angle - 0.5), last[1] - size * math.sin(angle - 0.5)),
                    (last[0] - size * math.cos(angle + 0.5), last[1] - size * math.sin(angle + 0.5))], fill=color)
    Image.alpha_composite(image, layer).crop(box).convert("RGB").save(destination, "PNG")


def window_choices():
    """Return visible host windows and the captured desktop coordinate bounds."""
    windows = []
    origin, size = (0, 0), (0, 0)
    try:
        if sys.platform == "win32":
            return _windows_choices()
        elif sys.platform == "darwin":
            import Quartz
            origin, size = _layout_bounds(_mac_displays())
            for window in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0):
                rect = window.get("kCGWindowBounds", {})
                title = window.get("kCGWindowName") or window.get("kCGWindowOwnerName")
                if title and window.get("kCGWindowLayer", 0) == 0:
                    windows.append({"id": str(window["kCGWindowNumber"]), "title": title,
                        "x": rect["X"], "y": rect["Y"], "width": rect["Width"], "height": rect["Height"]})
        elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            monitors = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=3)
            layout = []
            for monitor in json.loads(monitors.stdout):
                if monitor.get("disabled") or not monitor.get("width") or not monitor.get("height"):
                    continue
                scale = monitor.get("scale") or 1
                width, height = monitor["width"] / scale, monitor["height"] / scale
                if monitor.get("transform", 0) % 2:
                    width, height = height, width
                layout.append({"x": monitor["x"], "y": monitor["y"], "width": width, "height": height})
            if layout:
                origin, size = _layout_bounds(layout)
            clients = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=3)
            for window in json.loads(clients.stdout):
                if window.get("mapped", True) and not window.get("hidden", False):
                    windows.append({"id": window["address"], "title": window.get("title") or window.get("class", "Window"),
                        "x": window["at"][0], "y": window["at"][1], "width": window["size"][0], "height": window["size"][1]})
        elif os.environ.get("DISPLAY"):
            from Xlib.display import Display
            display = Display()
            root = display.screen().root
            geometry = root.get_geometry()
            size = (geometry.width, geometry.height)
            for window in root.query_tree().children:
                try:
                    title = window.get_wm_name()
                    rect = window.get_geometry()
                    at = root.translate_coords(window, 0, 0)
                    if title and window.get_attributes().map_state == 2:
                        windows.append({"id": str(window.id), "title": title, "x": at.x, "y": at.y,
                            "width": rect.width, "height": rect.height})
                except Exception:
                    continue
            display.close()
    except Exception:
        logger.debug("Could not enumerate screenshot windows", exc_info=True)
    return windows, origin, size


def _windows_choices():
    import ctypes
    from ctypes import wintypes
    windows = []
    user = ctypes.windll.user32
    with _windows_capture_coordinates():
        origin = (user.GetSystemMetrics(76), user.GetSystemMetrics(77))
        size = (user.GetSystemMetrics(78), user.GetSystemMetrics(79))
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetWindowRect.restype = wintypes.BOOL
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.restype = ctypes.c_int
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL

        def collect(hwnd, _param):
            rect = wintypes.RECT()
            if not user.IsWindowVisible(hwnd) or not user.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            text = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
            user.GetWindowTextW(hwnd, text, len(text))
            if text.value and rect.right > rect.left and rect.bottom > rect.top:
                windows.append({"id": str(hwnd), "title": text.value, "x": rect.left, "y": rect.top,
                    "width": rect.right - rect.left, "height": rect.bottom - rect.top})
            return True

        user.EnumWindows(callback_type(collect), 0)
    return windows, origin, size
