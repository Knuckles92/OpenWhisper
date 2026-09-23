"""Carbon global-hotkey backend: key mapping, registration, and dispatch.

This backend is macOS-only in production -- it is what lets hotkeys work with
no Accessibility grant -- but everything except the framework handle is plain
Python, so the registrar runs on every platform here against a fake Carbon
library.

Two things are deliberately split by platform. The ``kVK_ANSI_*`` table is a
literal in the module, so it is checked everywhere. Named keys (space, esc,
F-keys) are read out of ``pynput``'s ``Key`` enum, whose ``vk`` values are the
*current* platform's -- X11 keysyms on Linux, macOS virtual keycodes on Darwin
-- so those assertions only hold on Darwin.
"""
from __future__ import annotations

import ast
import ctypes
import sys
import unittest
import unittest.mock
from pathlib import Path

import pytest

pytest.importorskip(
    "pynput.keyboard",
    reason="the Carbon backend imports pynput's keyboard backend at import time",
)

from services import _hotkey_carbon as carbon  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeCarbon:
    """Stand-in for the Carbon CDLL, recording what the registrar asks for.

    Only the six entry points ``_load_carbon`` configures are implemented.
    ``register_status`` and ``install_status`` let a test make the OS refuse,
    which is the branch a real Mac reaches when another app already owns a
    combination (``eventHotKeyExistsErr``).
    """

    APP_TARGET = 0x1234

    def __init__(self, register_status: int = 0, install_status: int = 0):
        self.register_status = register_status
        self.install_status = install_status
        self.installs = 0
        self.registered: list[tuple[int, int, int]] = []
        self.unregistered: list[int] = []
        self.event_kind = carbon._K_EVENT_HOTKEY_PRESSED
        self.parameter_status = 0
        self.event_hotkey_id = 0
        self._next_ref = 0x1000

    def GetApplicationEventTarget(self):
        return self.APP_TARGET

    def InstallEventHandler(self, target, proc, count, specs, user_data, out_ref):
        assert target == self.APP_TARGET
        # Both edges must be requested, or push-and-hold never stops.
        kinds = {(specs[i].eventClass, specs[i].eventKind) for i in range(count)}
        assert kinds == {
            (carbon._K_EVENT_CLASS_KEYBOARD, carbon._K_EVENT_HOTKEY_PRESSED),
            (carbon._K_EVENT_CLASS_KEYBOARD, carbon._K_EVENT_HOTKEY_RELEASED),
        }
        if self.install_status == 0:
            self.installs += 1
        return self.install_status

    def RegisterEventHotKey(self, keycode, modifiers, hotkey_id, target, options,
                            out_ref):
        assert target == self.APP_TARGET
        assert hotkey_id.signature == carbon._HOTKEY_SIGNATURE
        if self.register_status != 0:
            return self.register_status
        self._next_ref += 1
        out_ref._obj.value = self._next_ref
        self.registered.append((keycode, modifiers, hotkey_id.id))
        return 0

    def UnregisterEventHotKey(self, ref):
        self.unregistered.append(ref.value)
        return 0

    def GetEventKind(self, event):
        return self.event_kind

    def GetEventParameter(self, event, name, desired, actual_type, size,
                          actual_size, out_data):
        assert name == carbon._K_EVENT_PARAM_DIRECT_OBJECT
        assert desired == carbon._TYPE_EVENT_HOTKEY_ID
        if self.parameter_status == 0:
            out_data._obj.id = self.event_hotkey_id
        return self.parameter_status


class CarbonRegistrarTestCase(unittest.TestCase):
    """Base that swaps the module-global framework handle for a fake."""

    register_status = 0
    install_status = 0

    def setUp(self):
        self.carbon_lib = FakeCarbon(
            register_status=self.register_status,
            install_status=self.install_status,
        )
        patcher = unittest.mock.patch.object(carbon, "_carbon", self.carbon_lib)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.actions: list[tuple[str, bool]] = []
        self.registrar = carbon.CarbonHotkeyRegistrar(
            on_action=lambda action, released: self.actions.append((action, released))
        )


class TestFourCharCodes(unittest.TestCase):
    """The OSType constants are packed by hand and never round-tripped."""

    def test_packs_big_endian(self):
        self.assertEqual(carbon._four_char_code("keyb"), 0x6B657962)

    def test_module_constants_match_carbon(self):
        self.assertEqual(carbon._K_EVENT_CLASS_KEYBOARD, 0x6B657962)  # 'keyb'
        self.assertEqual(carbon._K_EVENT_PARAM_DIRECT_OBJECT, 0x2D2D2D2D)  # '----'
        self.assertEqual(carbon._TYPE_EVENT_HOTKEY_ID, 0x686B6964)  # 'hkid'
        self.assertEqual(carbon._HOTKEY_SIGNATURE, 0x4F57484B)  # 'OWHK'

    def test_event_kinds_match_carbon(self):
        self.assertEqual(carbon._K_EVENT_HOTKEY_PRESSED, 5)
        self.assertEqual(carbon._K_EVENT_HOTKEY_RELEASED, 6)


class TestModifierMasks(unittest.TestCase):
    """Values from <Carbon/HIToolbox/Events.h>; a wrong mask binds the wrong combo."""

    def test_masks(self):
        self.assertEqual(carbon._CARBON_MODIFIERS, {
            "cmd": 0x0100,
            "shift": 0x0200,
            "alt": 0x0800,
            "ctrl": 0x1000,
        })

    def test_no_stray_modifier_names(self):
        # parse_hotkey canonicalizes to exactly these four; anything else here
        # would silently never be applied.
        self.assertEqual(set(carbon._CARBON_MODIFIERS), {"cmd", "shift", "alt", "ctrl"})


class TestAnsiKeycodeTable(unittest.TestCase):
    """``_ANSI_KEYCODES`` is a literal, so it is checked on every platform.

    These are physical key positions from ``<Carbon/HIToolbox/Events.h>``. The
    non-obvious ones are the digits: 5 and 6 are transposed relative to the
    numeric order, as are 7/8/9.
    """

    CANONICAL = {
        "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
        "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
        "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
        "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
        "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D,
        "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
        "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A,
        ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
    }

    def test_table_matches_carbon(self):
        self.assertEqual(carbon._ANSI_KEYCODES, self.CANONICAL)

    def test_every_letter_and_digit_resolves(self):
        for key in "abcdefghijklmnopqrstuvwxyz0123456789":
            with self.subTest(key=key):
                self.assertEqual(carbon.keycode_for(key), self.CANONICAL[key])

    def test_no_duplicate_keycodes(self):
        codes = list(carbon._ANSI_KEYCODES.values())
        self.assertEqual(len(codes), len(set(codes)))


class TestKeycodeResolution(unittest.TestCase):
    def test_missing_main_key_is_unmappable(self):
        # parse_hotkey returns None for an empty hotkey string; registering
        # keycode 0 there would bind "A" to an unassigned action.
        self.assertIsNone(carbon.keycode_for(None))
        self.assertIsNone(carbon.keycode_for(""))

    def test_unknown_name_is_unmappable(self):
        self.assertIsNone(carbon.keycode_for("play_pause"))
        self.assertIsNone(carbon.keycode_for("nonexistent_key"))

    def test_raw_vk_names_carry_their_keycode(self):
        # pynput emits vk<N> for keys with no character; the number is already
        # the macOS keycode.
        self.assertEqual(carbon.keycode_for("vk80"), 80)
        self.assertEqual(carbon.keycode_for("vk0"), 0)

    def test_malformed_vk_name_is_unmappable(self):
        self.assertIsNone(carbon.keycode_for("vkx"))
        self.assertIsNone(carbon.keycode_for("vk"))


@unittest.skipUnless(sys.platform == "darwin", "pynput vks are the host platform's")
class TestSpecialKeycodesOnMacOS(unittest.TestCase):
    """Named keys come from pynput, so only Darwin sees macOS keycodes."""

    def test_media_keys_are_excluded(self):
        # Only pynput's Darwin backend marks synthetic vks with _is_media.
        self.assertNotIn("media_play_pause", carbon._SPECIAL_KEYCODES)
        self.assertNotIn("media_volume_up", carbon._SPECIAL_KEYCODES)

    def test_named_keys_match_carbon(self):
        expected = {
            "space": 49, "esc": 53, "enter": 36, "tab": 48, "backspace": 51,
            "delete": 117,  # kVK_ForwardDelete; backspace is kVK_Delete
            "up": 126, "down": 125, "left": 123, "right": 124,
            "home": 115, "end": 119, "page_up": 116, "page_down": 121,
            "f1": 122, "f5": 96, "f12": 111, "f13": 105, "f19": 80,
        }
        for name, code in expected.items():
            with self.subTest(key=name):
                self.assertEqual(carbon.keycode_for(name), code)


class TestHandlerInstallation(CarbonRegistrarTestCase):
    def test_handler_is_installed_once_across_re_registration(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.registrar.register_hotkeys({"record": "ctrl+alt+t"})
        self.assertEqual(self.carbon_lib.installs, 1)

    def test_cleanup_keeps_the_handler_for_process_life(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.registrar.cleanup()
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.assertEqual(self.carbon_lib.installs, 1)


class TestHandlerInstallFailure(CarbonRegistrarTestCase):
    install_status = -9866  # eventHandlerAlreadyInstalledErr

    def test_nothing_is_registered_without_a_handler(self):
        # A hotkey registered with no handler would be claimed from every other
        # app and then silently swallowed.
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.assertEqual(self.carbon_lib.registered, [])
        self.assertEqual(self.registrar._id_to_action, {})


class TestRegistration(CarbonRegistrarTestCase):
    def test_registers_each_hotkey_with_its_modifier_mask(self):
        self.registrar.register_hotkeys({
            "record": "ctrl+alt+r",
            "cancel": "cmd+shift+c",
        })
        by_action = {
            self.registrar._id_to_action[hotkey_id]: (keycode, modifiers)
            for keycode, modifiers, hotkey_id in self.carbon_lib.registered
        }
        self.assertEqual(by_action, {
            "record": (0x0F, 0x1000 | 0x0800),
            "cancel": (0x08, 0x0100 | 0x0200),
        })

    def test_unmappable_key_is_skipped_without_losing_the_rest(self):
        self.registrar.register_hotkeys({
            "media": "ctrl+alt+play_pause",
            "record": "ctrl+alt+r",
        })
        self.assertEqual(sorted(self.registrar._id_to_action.values()), ["record"])

    def test_ids_are_unique_per_action(self):
        self.registrar.register_hotkeys({
            "record": "ctrl+alt+r",
            "cancel": "ctrl+alt+c",
            "toggle": "ctrl+alt+t",
        })
        self.assertEqual(len(set(self.registrar._id_to_action)), 3)
        self.assertEqual(
            sorted(self.registrar._id_to_action.values()),
            ["cancel", "record", "toggle"],
        )

    def test_re_registration_releases_the_previous_hotkeys(self):
        # HotkeyManager re-registers on every settings change; leaking refs
        # would keep stale combinations claimed system-wide.
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        first_refs = [ref.value for ref in self.registrar._hotkey_refs]
        self.registrar.register_hotkeys({"record": "ctrl+alt+t"})
        self.assertEqual(self.carbon_lib.unregistered, first_refs)
        self.assertEqual(len(self.registrar._hotkey_refs), 1)
        self.assertEqual(list(self.registrar._id_to_action.values()), ["record"])

    def test_unregister_all_clears_the_dispatch_map(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.registrar.unregister_all()
        self.assertEqual(self.registrar._hotkey_refs, [])
        self.assertEqual(self.registrar._id_to_action, {})

    def test_empty_map_releases_everything(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.registrar.register_hotkeys({})
        self.assertEqual(self.registrar._id_to_action, {})
        self.assertEqual(len(self.carbon_lib.unregistered), 1)


class TestRegistrationRefused(CarbonRegistrarTestCase):
    register_status = -9878  # eventHotKeyExistsErr

    def test_a_claimed_combination_is_skipped_not_raised(self):
        # Another app owning the combo is expected; the app must keep starting.
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.assertEqual(self.registrar._id_to_action, {})
        self.assertEqual(self.registrar._hotkey_refs, [])

    def test_a_refused_hotkey_is_not_dispatchable(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.carbon_lib.event_hotkey_id = 1
        self.registrar._handle_event(None, object(), None)
        self.assertEqual(self.actions, [])


class TestDispatch(CarbonRegistrarTestCase):
    """``_handle_event`` runs on the Qt main thread and must never raise."""

    def setUp(self):
        super().setUp()
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.hotkey_id = next(iter(self.registrar._id_to_action))

    def _fire(self):
        self.carbon_lib.event_hotkey_id = self.hotkey_id
        return self.registrar._handle_event(None, object(), None)

    def test_press_dispatches_with_released_false(self):
        self.carbon_lib.event_kind = carbon._K_EVENT_HOTKEY_PRESSED
        self.assertEqual(self._fire(), 0)
        self.assertEqual(self.actions, [("record", False)])

    def test_release_dispatches_with_released_true(self):
        # Push-and-hold recording stops on this edge; a callback that only
        # accepts the action name raises here and the hold never ends.
        self.carbon_lib.event_kind = carbon._K_EVENT_HOTKEY_RELEASED
        self.assertEqual(self._fire(), 0)
        self.assertEqual(self.actions, [("record", True)])

    def test_unknown_hotkey_id_is_ignored(self):
        self.carbon_lib.event_hotkey_id = self.hotkey_id + 999
        self.assertEqual(self.registrar._handle_event(None, object(), None), 0)
        self.assertEqual(self.actions, [])

    def test_unreadable_event_parameter_is_ignored(self):
        self.carbon_lib.parameter_status = -9870  # eventParameterNotFoundErr
        self.assertEqual(self.registrar._handle_event(None, object(), None), 0)
        self.assertEqual(self.actions, [])

    def test_a_raising_callback_still_returns_noerr(self):
        # Returning anything else would make the window server stop delivering.
        self.registrar._on_action = lambda action, released: 1 / 0
        self.assertEqual(self._fire(), 0)


class TestNoFrameworkAvailable(unittest.TestCase):
    """Off macOS ``_load_carbon`` returns None and every call must no-op."""

    def setUp(self):
        patcher = unittest.mock.patch.object(carbon, "_carbon", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registrar = carbon.CarbonHotkeyRegistrar(on_action=lambda a, r: None)

    def test_is_available_is_false(self):
        self.assertFalse(carbon.is_available())

    def test_register_unregister_and_cleanup_are_silent(self):
        self.registrar.register_hotkeys({"record": "ctrl+alt+r"})
        self.registrar.unregister_all()
        self.registrar.cleanup()
        self.assertEqual(self.registrar._id_to_action, {})


class TestVerificationScriptContract(unittest.TestCase):
    """``scripts/verify_carbon_hotkeys.py`` must accept the dispatch arity.

    The registrar grew the ``released`` argument with push-and-hold recording
    and the script was left behind. Its callback then raised inside
    ``_handle_event``, which logs and swallows -- so the manual check printed
    nothing on every press and read as "Carbon hotkeys are broken".
    """

    def test_callback_accepts_action_and_released(self):
        path = ROOT / "scripts" / "verify_carbon_hotkeys.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        callbacks = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "on_action"
        ]
        self.assertEqual(len(callbacks), 1, "expected one on_action callback")

        arguments = callbacks[0].args
        required = len(arguments.args) - len(arguments.defaults)
        self.assertLessEqual(required, 2)
        self.assertGreaterEqual(len(arguments.args), 2)


@unittest.skipUnless(sys.platform == "darwin", "needs the real Carbon framework")
class TestRealFramework(unittest.TestCase):
    """Guards that only a Mac can answer: the framework and the shipped keys."""

    def test_framework_loads(self):
        # ctypes.util.find_library("Carbon") resolving is not guaranteed by the
        # dyld shared cache; without it every hotkey silently falls back to
        # pynput, which needs an Accessibility grant.
        self.assertTrue(carbon.is_available())
        self.assertIsNotNone(carbon._carbon)

    def test_pointer_returning_calls_declare_their_types(self):
        # ctypes truncates an undeclared pointer return to int and crashes.
        self.assertIs(carbon._carbon.GetApplicationEventTarget.restype,
                      ctypes.c_void_p)
        self.assertTrue(carbon._carbon.GetApplicationEventTarget())

    def test_every_shipped_default_hotkey_is_bindable(self):
        from config import config

        unmappable = {}
        for action, hotkey in config.DEFAULT_HOTKEYS.items():
            if not hotkey:
                continue
            _, main_key = carbon.parse_hotkey(hotkey)
            if carbon.keycode_for(main_key) is None:
                unmappable[action] = hotkey
        self.assertEqual(unmappable, {})
