"""The user's environment, as opposed to the one OpenWhisper runs in.

OpenWhisper changes its own environment to run: the frozen build's loader
puts its bundled libraries first on ``LD_LIBRARY_PATH`` and ``PATH``, the
source launcher activates the app's virtualenv, the native bootstrap prepends
CUDA and ICU folders to ``PATH``, and the Qt setup sets display variables.
Programs OpenWhisper starts for the user (actions, hooks, agents, terminals,
links) must not inherit any of that: a bundled libreadline crashes bash on
Arch, the app's virtualenv would catch an agent's ``pip install``, and Qt
variables meant for this process restyle other apps.

Code that changes this process's environment records the original value here
(``set_for_app`` / ``prepend_for_app``); ``external_env`` undoes those changes
and the loader's. Stdlib only: the native bootstrap imports it first.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Iterable, Mapping, Optional

_lock = threading.Lock()
#: Variables this process changed for itself: name -> value before the first change
#: (None when it was unset).
_original: dict[str, Optional[str]] = {}

#: Loader search paths a frozen build may have prepended its bundle to.
_LOADER_PATHS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")


def _key(name: str) -> str:
    # os.environ is case-insensitive on Windows and stores upper-case names.
    return name.upper() if sys.platform == "win32" else name


def _remember(name: str) -> None:
    key = _key(name)
    with _lock:
        if key not in _original:
            _original[key] = os.environ.get(name)


def set_for_app(name: str, value: str, *, keep_existing: bool = False) -> None:
    """Set ``name`` for this process; programs it starts get the original back.

    Args:
        keep_existing: Leave a value the user already set alone.
    """
    if keep_existing and name in os.environ:
        return
    _remember(name)
    os.environ[name] = value


def prepend_for_app(name: str, entries: Iterable[str]) -> None:
    """Put ``entries`` first on a path list such as ``PATH``, for this process only."""
    entries = [entry for entry in entries if entry]
    if not entries:
        return
    _remember(name)
    existing = [part for part in os.environ.get(name, "").split(os.pathsep) if part]
    os.environ[name] = os.pathsep.join(
        entries + [part for part in existing if part not in entries]
    )


def _within(path: str, root: str) -> bool:
    try:
        inner = os.path.normcase(os.path.realpath(path))
        outer = os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _drop_within(env: dict, name: str, roots: list[str]) -> None:
    value = env.get(name)
    if value is None or not roots:
        return
    kept = [part for part in value.split(os.pathsep)
            if part and not any(_within(part, root) for root in roots)]
    if kept:
        env[name] = os.pathsep.join(kept)
    else:
        env.pop(name, None)


def _own_virtualenv(env: Mapping[str, str]) -> Optional[str]:
    """The app's own virtualenv when the source launcher activated it, else None."""
    venv = env.get("VIRTUAL_ENV")
    if not venv or getattr(sys, "frozen", False):
        return None
    return venv if _within(sys.prefix, venv) else None


def external_env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment for a program OpenWhisper starts for the user.

    The user's own environment: this process's minus what OpenWhisper changed
    for itself (recorded variables, the frozen loader's paths and bootstrap
    variables, the app's activated virtualenv), plus ``extra``.
    """
    env = dict(os.environ)
    with _lock:
        original = dict(_original)
    for name, value in original.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    if getattr(sys, "frozen", False):
        for name in [n for n in env if n.upper().startswith("_PYI_") or n.upper() == "_MEIPASS2"]:
            env.pop(name, None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        bundle = [root for root in (getattr(sys, "_MEIPASS", ""),
                                    os.path.dirname(sys.executable)) if root]
        for name in _LOADER_PATHS:
            saved = env.pop(f"{name}_ORIG", None)
            if saved is not None:
                env[name] = saved
            else:
                _drop_within(env, name, bundle)
        _drop_within(env, "PATH", bundle[:1])

    venv = _own_virtualenv(env)
    if venv:
        env.pop("VIRTUAL_ENV", None)
        env.pop("VIRTUAL_ENV_PROMPT", None)
        _drop_within(env, "PATH", [venv])
        for name in _LOADER_PATHS:
            _drop_within(env, name, [venv])

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def drop(env: dict, names: Iterable[str]) -> None:
    """Remove ``names`` from ``env``, matching names the way the system does.

    Windows ignores case in variable names, so ``Anthropic_Api_Key`` written in
    a ``.env`` file is the ``ANTHROPIC_API_KEY`` that ``os.environ`` holds.
    """
    unwanted = {_key(name) for name in names}
    for name in [name for name in env if _key(name) in unwanted]:
        del env[name]


def forget() -> None:
    """Drop the recorded originals (tests)."""
    with _lock:
        _original.clear()
