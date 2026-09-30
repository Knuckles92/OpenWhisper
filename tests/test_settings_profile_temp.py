import cProfile
import pstats
import time

from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.utils.font_scale import apply_ui_theme


def test_profile_settings(_session_qt_application):
    apply_ui_theme("dark")
    profile = cProfile.Profile()
    profile.enable()
    start = time.perf_counter()
    dialog = SettingsDialog()
    built = time.perf_counter()
    dialog.refresh()
    refreshed = time.perf_counter()
    dialog.show()
    _session_qt_application.processEvents()
    shown = time.perf_counter()
    profile.disable()
    print(f"SETTINGS build={built-start:.3f}s refresh={refreshed-built:.3f}s show={shown-refreshed:.3f}s")
    pstats.Stats(profile).strip_dirs().sort_stats("cumtime").print_stats(40)
    profile.dump_stats(".tmp/settings-before.prof")
    dialog.close()
