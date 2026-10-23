"""Keep X11 global shortcuts out of the focused app.

pynput's X11 listener records key events (XRecord) and cannot suppress
them, so a shortcut also reached whatever had focus: every dictation with
the default Num * shortcut started with "**", and Num − left a "-".

A passive grab (XGrabKey on the root window) delivers the key to this
connection instead of the focused window. XRecord still sees it, so the
pynput listener keeps matching and dispatching exactly as before; this
module only takes the keystroke away from other apps. Each shortcut is
grabbed with every combination of Caps Lock and Num Lock, which X counts
as modifiers.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# keysym -> "kp <name>" suffix, kept in step with _hotkey_pynput._X11_KEYPAD_NAMES.
_KEYPAD_KEYSYMS: Dict[str, int] = {}


def _keypad_keysym(name: str) -> Optional[int]:
    if not _KEYPAD_KEYSYMS:
        from services._hotkey_pynput import _X11_KEYPAD_NAMES

        _KEYPAD_KEYSYMS.update({value: keysym for keysym, value in _X11_KEYPAD_NAMES.items()})
    return _KEYPAD_KEYSYMS.get(name)


def keysym_for(main_key: str) -> Optional[int]:
    """The X keysym for a hotkey's main key name, or None if it has none.

    Names are the ones ``_hotkey_pynput.key_to_name`` produces: ``kp *``,
    pynput ``Key`` names (``f8``, ``page_up``), single characters, ``vk<n>``.
    """
    if not main_key:
        return None
    if main_key.startswith("kp "):
        return _keypad_keysym(main_key[3:])
    if main_key.startswith("vk") and main_key[2:].isdigit():
        return int(main_key[2:])
    if len(main_key) == 1:
        from pynput._util.xorg import char_to_keysym

        return char_to_keysym(main_key)
    from services._hotkey_pynput import _load_pynput_keyboard

    key = getattr(_load_pynput_keyboard().Key, main_key, None)
    vk = getattr(getattr(key, "value", None), "vk", None)
    return vk if isinstance(vk, int) else None


class X11KeyGrabber:
    """Passive root-window grabs for the configured keyboard shortcuts."""

    def __init__(self, display=None):
        # This connection is used from two threads: the drain below and
        # the caller's grab updates.
        import Xlib.threaded  # noqa: F401
        from Xlib import X
        from Xlib.display import Display

        self._X = X
        self._display = display or Display()
        self._root = self._display.screen().root
        self._lock = threading.Lock()
        self._grabbed: Set[Tuple[int, int]] = set()
        self._closed = False
        self._failed: Set[str] = set()
        self._thread = threading.Thread(target=self._drain, name="x11-key-grab", daemon=True)
        self._thread.start()

    def _modifier_masks(self) -> Dict[str, int]:
        X = self._X
        return {"ctrl": X.ControlMask, "shift": X.ShiftMask, "alt": X.Mod1Mask, "cmd": X.Mod4Mask}

    def _lock_variants(self) -> Tuple[int, ...]:
        from pynput._util.xorg import numlock_mask

        numlock = numlock_mask(self._display) or 0
        caps = self._X.LockMask
        return tuple({0, caps, numlock, caps | numlock})

    def _combinations(self, hotkeys: Iterable[str]) -> Set[Tuple[int, int]]:
        from services._hotkey_common import is_mouse_key
        from services._hotkey_pynput import parse_hotkey

        masks = self._modifier_masks()
        locks = self._lock_variants()
        wanted: Set[Tuple[int, int]] = set()
        for hotkey in hotkeys:
            if not hotkey:
                continue
            modifiers, main_key = parse_hotkey(hotkey)
            if not main_key or is_mouse_key(main_key):
                continue
            keysym = keysym_for(main_key)
            keycode = self._display.keysym_to_keycode(keysym) if keysym else 0
            if not keycode:
                if hotkey not in self._failed:
                    self._failed.add(hotkey)
                    logger.info("No key on this keyboard for shortcut %s; not grabbing it", hotkey)
                continue
            base = 0
            for modifier in modifiers:
                base |= masks.get(modifier, 0)
            wanted.update((keycode, base | lock) for lock in locks)
        return wanted

    def register_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        """Grab exactly the shortcuts in ``hotkeys``' values; release the rest."""
        with self._lock:
            if self._closed:
                return
            try:
                wanted = self._combinations(hotkeys.values())
                self._apply(wanted)
            except Exception:
                logger.warning("Could not update X11 shortcut grabs", exc_info=True)

    def unregister_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._apply(set())
            except Exception:
                logger.debug("Could not release X11 shortcut grabs", exc_info=True)

    def _apply(self, wanted: Set[Tuple[int, int]]) -> None:
        from Xlib.error import CatchError

        X = self._X
        for keycode, modifiers in self._grabbed - wanted:
            self._root.ungrab_key(keycode, modifiers)
        refused = []
        for keycode, modifiers in sorted(wanted - self._grabbed):
            # Another program (a desktop shortcut) may already own the key:
            # X refuses with BadAccess, and that shortcut then keeps passing
            # through, as before.
            catch = CatchError()
            self._root.grab_key(keycode, modifiers, False, X.GrabModeAsync, X.GrabModeAsync,
                                onerror=catch)
            self._display.sync()
            if catch.get_error() is not None:
                refused.append((keycode, modifiers))
        self._display.sync()
        self._grabbed = wanted - set(refused)
        if refused:
            logger.info("X11 refused %d shortcut grab(s); those keys still reach the focused app",
                        len(refused))

    def _drain(self) -> None:
        """Read and drop the grabbed key events; pynput reacts to them via XRecord."""
        while not self._closed:
            try:
                self._display.next_event()
            except Exception:
                if not self._closed:
                    logger.debug("X11 key grab connection ended", exc_info=True)
                return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._apply(set())
            except Exception:
                logger.debug("Could not release X11 shortcut grabs", exc_info=True)
            self._closed = True
        try:
            self._display.close()
        except Exception:
            logger.debug("Could not close the X11 key grab connection", exc_info=True)


def create_grabber() -> Optional[X11KeyGrabber]:
    """A grabber for this X session, or None where one can't be made."""
    try:
        return X11KeyGrabber()
    except Exception as exc:
        logger.info("X11 shortcut grabs unavailable; shortcuts also reach the focused app: %s", exc)
        return None
