"""Standalone check that Carbon global hotkeys register and fire (macOS).

Run from the repo root in your normal GUI session:

    ./venv/bin/python scripts/verify_carbon_hotkeys.py

A small window appears. MINIMIZE it (or click another app to defocus it), then
press one of the hotkeys. Each press should print a line in this terminal —
proving global detection works with NO Accessibility permission granted.

    Ctrl+Alt+R            -> record_toggle
    Ctrl+Alt+Escape       -> cancel
    Ctrl+Alt+Shift+R      -> enable_disable

Press Ctrl+C in the terminal, or close the window, to quit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from services import _hotkey_carbon as carbon
from services.hotkey_manager import is_accessibility_trusted


def main() -> int:
    app = QApplication(sys.argv)

    print(f"Carbon backend available: {carbon.is_available()}")
    print(f"Accessibility trusted:    {is_accessibility_trusted()}  "
          f"(should NOT matter for hotkeys)")
    print("-" * 60)

    counter = {"n": 0}

    def on_action(action: str) -> None:
        counter["n"] += 1
        print(f"  [{counter['n']:>3}] HOTKEY FIRED -> {action}")

    registrar = carbon.CarbonHotkeyRegistrar(on_action=on_action)
    registrar.register_hotkeys({
        "record_toggle": "ctrl+alt+r",
        "cancel": "ctrl+alt+escape",
        "enable_disable": "ctrl+alt+shift+r",
    })

    registered = len(registrar._hotkey_refs)
    print(f"Registered {registered}/3 hotkeys.")
    if registered == 0:
        print("!! Registration failed — check the OSStatus warnings above.")
    else:
        print("Now MINIMIZE the window and press the hotkeys. Watch this terminal.")
    print("-" * 60)

    window = QWidget()
    window.setWindowTitle("Carbon Hotkey Test")
    window.resize(420, 120)
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel(
        "Minimize me, then press:\n"
        "  Ctrl+Alt+R / Ctrl+Alt+Esc / Ctrl+Alt+Shift+R\n"
        "Each press prints a line in the terminal."
    ))
    window.show()

    try:
        return app.exec()
    finally:
        registrar.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
