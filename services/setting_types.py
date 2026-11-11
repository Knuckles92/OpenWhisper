"""Typed settings declarations: a key that knows its type, default and validation.

Every member of :class:`services.settings.SettingsKey` is a :class:`Setting`.
A ``Setting`` *is* its JSON key (a ``str`` subclass), so it indexes the
loaded settings mapping, is written to the settings file and compared with
plain strings exactly as the bare key always was. It also carries what a
reader needs:

``SettingsKey.AUTO_PASTE.read(settings)``
    The stored value when it passes the setting's validation, otherwise its
    default. ``settings`` is a mapping from ``settings_manager.load_all_settings()``
    (pass the same snapshot to every read in one operation); ``None`` reads
    the settings file. The return type is the declared one, so a type checker
    sees ``bool`` here, and a misspelt key is an attribute error.

The validation rules are deliberately the ones the resolvers always used:
a bool must be a real ``bool``; a choice must be one of its values; an int is
anything ``int()`` accepts; strings, lists and dicts must be that type. A
value that fails falls back to the default, never raises.

A setting declared ``in_table=False`` has a read default but no entry in
``SETTING_DEFAULTS``. The ``Optional*`` kinds are for settings whose absence
itself means something (``APP_STYLE_TONES``, ``TEXT_TRANSFORMS``, ...): they
read as ``None`` when unset and as an empty container when malformed, and
the owning module decides what None means.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Final, Generic, Mapping, Self, TypeVar

T = TypeVar("T")

#: Stands for a key that is not saved, which is not the same as saved None.
_UNSET: Any = object()


class _Invalid:
    """What ``Setting._convert`` returns for a stored value it rejects."""

    __slots__ = ()


INVALID: Final = _Invalid()


def _load_settings() -> Mapping[str, Any]:
    # settings imports this module; the manager exists by the time anything reads.
    from services.settings import settings_manager

    return settings_manager.load_all_settings()


class Setting(str, Generic[T]):
    """A settings key with its type, default and validation.

    Subclasses override :meth:`parse`. Instances are immutable and compare,
    hash, format and serialise exactly like the plain key string.
    """

    _default: T
    _in_table: bool

    def __new__(cls, key: str, default: T, *, in_table: bool = True) -> Self:
        self = super().__new__(cls, key)
        self._default = default
        self._in_table = in_table
        return self

    # A Setting is a constant: copying it, or a mapping keyed by it, keeps it.
    # Pickling stores the bare key, which is all the settings file holds.
    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        return (str, (str(self),))

    @property
    def key(self) -> str:
        """The bare JSON key."""
        return str(self)

    @property
    def in_table(self) -> bool:
        """Whether the default belongs in ``SETTING_DEFAULTS``."""
        return self._in_table

    @property
    def default(self) -> T:
        """A fresh copy of the default, so a caller may mutate what it reads."""
        return deepcopy(self._default)

    def _convert(self, raw: Any) -> T | _Invalid:
        """The value a stored ``raw`` stands for, or ``INVALID``."""
        raise NotImplementedError

    def parse(self, raw: Any) -> T:
        """The value a stored ``raw`` stands for, or the default when it is not valid."""
        value = self._convert(raw)
        return self.default if isinstance(value, _Invalid) else value

    def read(self, settings: Mapping[str, Any] | None = None) -> T:
        """The stored value when valid, otherwise the default."""
        if settings is None:
            settings = _load_settings()
        raw = settings.get(self, _UNSET)
        if raw is _UNSET:
            return self.default
        return self.parse(raw)

    def stored(self, settings: Mapping[str, Any] | None = None) -> T | None:
        """The stored value when set and valid, otherwise None (never the default).

        For the few readers that treat "not chosen" apart from "chose the
        default", such as legacy fallbacks.
        """
        if settings is None:
            settings = _load_settings()
        raw = settings.get(self, _UNSET)
        if raw is _UNSET:
            return None
        value = self._convert(raw)
        return None if isinstance(value, _Invalid) else value


class BoolSetting(Setting[bool]):
    """On/off: the stored ``bool``; anything else (``"yes"``, ``0``, ``None``) is the default."""

    def _convert(self, raw: Any) -> bool | _Invalid:
        return raw if isinstance(raw, bool) else INVALID


class IntSetting(Setting[int]):
    """A whole number.

    By default whatever ``int()`` accepts (``"12"``, ``3.7``, ``True``), as the
    resolvers always took it; ``strict`` accepts only a JSON integer (never a
    bool). Outside ``valid`` (inclusive ``(low, high)``) the value is the
    default; ``clamp`` instead pulls it into range. Either bound may be None.
    """

    strict: bool
    valid: tuple[int | None, int | None]
    clamp: tuple[int | None, int | None]

    def __new__(
        cls,
        key: str,
        default: int,
        *,
        strict: bool = False,
        valid: tuple[int | None, int | None] = (None, None),
        clamp: tuple[int | None, int | None] = (None, None),
        in_table: bool = True,
    ) -> Self:
        self = super().__new__(cls, key, default, in_table=in_table)
        self.strict = strict
        self.valid = valid
        self.clamp = clamp
        return self

    def _convert(self, raw: Any) -> int | _Invalid:
        if self.strict:
            if type(raw) is not int:
                return INVALID
            value = raw
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return INVALID
        low, high = self.valid
        if (low is not None and value < low) or (high is not None and value > high):
            return INVALID
        low, high = self.clamp
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        return value


class FloatSetting(Setting[float]):
    """A JSON number (not a bool), as a float; anything else is the default."""

    def _convert(self, raw: Any) -> float | _Invalid:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return INVALID
        return float(raw)


class StrSetting(Setting[str]):
    """Text: the stored ``str``, otherwise the default.

    ``normalize`` tidies a stored string (strip, cap) as its resolver always
    did; the default is used as declared.
    """

    normalize: Callable[[str], str] | None

    def __new__(
        cls,
        key: str,
        default: str,
        *,
        normalize: Callable[[str], str] | None = None,
        in_table: bool = True,
    ) -> Self:
        self = super().__new__(cls, key, default, in_table=in_table)
        self.normalize = normalize
        return self

    def _convert(self, raw: Any) -> str | _Invalid:
        if not isinstance(raw, str):
            return INVALID
        return raw if self.normalize is None else self.normalize(raw)


class ChoiceSetting(Setting[str]):
    """One of a fixed set of strings; anything else is the default.

    ``aliases`` maps a name an earlier build saved to its successor, and
    ``normalize`` tidies a stored string (case, spaces) before it is matched.
    """

    choices: tuple[str, ...]
    aliases: Mapping[str, str]
    normalize: Callable[[str], str] | None

    def __new__(
        cls,
        key: str,
        default: str,
        choices: tuple[str, ...],
        *,
        aliases: Mapping[str, str] | None = None,
        normalize: Callable[[str], str] | None = None,
        in_table: bool = True,
    ) -> Self:
        if default not in choices:
            raise ValueError(f"{key}: default {default!r} is not one of its choices")
        self = super().__new__(cls, key, default, in_table=in_table)
        self.choices = tuple(choices)
        self.aliases = dict(aliases or {})
        self.normalize = normalize
        return self

    def _convert(self, raw: Any) -> str | _Invalid:
        if not isinstance(raw, str):
            return INVALID
        if raw in self.aliases:
            return self.aliases[raw]
        if self.normalize is not None:
            raw = self.normalize(raw)
        return raw if raw in self.choices else INVALID


class IntChoiceSetting(Setting[int]):
    """One of a fixed set of ints; anything else is the default.

    By default the stored value goes through ``int()`` first; ``strict``
    accepts only a JSON integer.
    """

    choices: tuple[int, ...]
    strict: bool

    def __new__(
        cls,
        key: str,
        default: int,
        choices: tuple[int, ...],
        *,
        strict: bool = False,
        in_table: bool = True,
    ) -> Self:
        self = super().__new__(cls, key, default, in_table=in_table)
        self.choices = tuple(choices)
        self.strict = strict
        return self

    def _convert(self, raw: Any) -> int | _Invalid:
        if self.strict:
            if type(raw) is not int:
                return INVALID
            value = raw
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return INVALID
        return value if value in self.choices else INVALID


class ListSetting(Setting[list[Any]]):
    """A JSON array; anything else is a fresh copy of the default.

    The items are the owning module's to validate.
    """

    def __new__(cls, key: str, default: list[Any] | None = None, *, in_table: bool = True) -> Self:
        return super().__new__(cls, key, [] if default is None else default, in_table=in_table)

    def _convert(self, raw: Any) -> list[Any] | _Invalid:
        return raw if isinstance(raw, list) else INVALID


class DictSetting(Setting[dict[str, Any]]):
    """A JSON object; anything else is a fresh copy of the default."""

    def __new__(
        cls, key: str, default: dict[str, Any] | None = None, *, in_table: bool = True,
    ) -> Self:
        return super().__new__(cls, key, {} if default is None else default, in_table=in_table)

    def _convert(self, raw: Any) -> dict[str, Any] | _Invalid:
        return raw if isinstance(raw, dict) else INVALID


class OptionalListSetting(Setting[list[Any] | None]):
    """A list whose absence means something (the starters, say).

    Reads None when unset; a stored value that is not a list reads as an
    empty list, as their owners have always treated one.
    """

    def __new__(cls, key: str) -> Self:
        return super().__new__(cls, key, None, in_table=False)

    def _convert(self, raw: Any) -> list[Any] | None | _Invalid:
        return raw if isinstance(raw, list) else []


class OptionalDictSetting(Setting[dict[str, Any] | None]):
    """An object whose absence means something.

    Reads None when unset; a stored value that is not an object reads as an
    empty one.
    """

    def __new__(cls, key: str) -> Self:
        return super().__new__(cls, key, None, in_table=False)

    def _convert(self, raw: Any) -> dict[str, Any] | None | _Invalid:
        return raw if isinstance(raw, dict) else {}


class RawSetting(Setting[object]):
    """A value the owning module parses itself (legacy keys, mixed shapes).

    Reads the stored value unchanged, or ``None`` when unset; typed
    ``object`` so a caller must check before it uses one.
    """

    def __new__(cls, key: str) -> Self:
        return super().__new__(cls, key, None, in_table=False)

    def _convert(self, raw: object) -> object | _Invalid:
        return raw


__all__ = [
    "BoolSetting",
    "ChoiceSetting",
    "DictSetting",
    "FloatSetting",
    "IntChoiceSetting",
    "IntSetting",
    "ListSetting",
    "OptionalDictSetting",
    "OptionalListSetting",
    "RawSetting",
    "Setting",
    "StrSetting",
]
