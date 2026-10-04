"""In-place macOS application updates: verify, swap atomically, roll back.

Windows replaces its install directory through a separate helper executable
(``services.app_update_apply``). On a Mac the downloaded, SHA-256-verified DMG
is mounted read-only and its OpenWhisper.app is checked — bundle identifier,
release version, an intact code signature, and the running copy's Team ID when
it has one — then copied beside the installed bundle.

The new bundle's own executable runs as the helper. Once this process exits it
exchanges the two bundles with one atomic ``renamex_np(RENAME_SWAP)``, starts
the new version with a health token, and keeps the previous bundle until that
version acknowledges a healthy start. Otherwise it swaps them back and reopens
the previous version, leaving a message for it to show.

When the app cannot replace itself — App Translocation, a read-only volume, a
folder or bundle this account does not own — :func:`prepare_in_place_update`
returns the disk image instead, and the caller opens it for a manual install.
"""

from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Callable, List, Optional

from services.update_contract import (
    APP_NAME,
    HEALTH_ARG,
    PARENT_PID_ARG,
    TRANSACTION_ARG,
    normalize_version,
    updates_root,
    validate_transaction_id,
)

logger = logging.getLogger(__name__)

HELPER_ARG = "--macos-update-helper"
RESULT_PREFIX = "macos:"
#: How long the app waits for its helper to start before giving up.
READY_WAIT_S = 60.0
_PARENT_WAIT_S = 120.0
_HEALTH_WAIT_S = 180.0
_POLL_S = 0.2
_COMMAND_TIMEOUT_S = 300
_TRANSACTIONS_DIRNAME = "macos-tx"
_JOURNAL_NAME = "journal.json"
_READY_NAME = "ready"
_HEALTH_NAME = "healthy"
_LOG_NAME = "macos-updater.log"
# <stdio.h>: exchange two paths atomically on APFS and HFS+.
_RENAME_SWAP = 0x00000002
_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"


class State:
    """Durable transaction states, in order."""

    PREPARED = "prepared"
    SWAPPED = "swapped"
    HEALTHY = "healthy"
    ROLLED_BACK = "rolled_back"
    ABANDONED = "abandoned"


class MacUpdateError(Exception):
    """An in-place update cannot go ahead; the message is shown to the user."""


@dataclass
class MacUpdateJournal:
    transaction_id: str
    version: str
    previous_version: str
    #: The installed bundle, which keeps its path across the update.
    app_path: str
    #: The new bundle before the swap; the previous one after it.
    staged_path: str
    bundle_id: str
    executable: str
    health_token: str
    state: str = State.PREPARED
    parent_pid: int = 0
    helper_pid: int = 0


def encode_result(transaction_id: str) -> str:
    return RESULT_PREFIX + validate_transaction_id(transaction_id)


def decode_result(raw: Optional[str]) -> Optional[str]:
    if not raw or not raw.startswith(RESULT_PREFIX):
        return None
    try:
        return validate_transaction_id(raw[len(RESULT_PREFIX):])
    except ValueError:
        return None


def transactions_root(appdata: Optional[str] = None) -> str:
    # Separate from the Windows ``tx`` journals, whose loader rejects these.
    return os.path.join(updates_root(appdata), _TRANSACTIONS_DIRNAME)


def transaction_dir(transaction_id: str, appdata: Optional[str] = None) -> str:
    return os.path.join(transactions_root(appdata), validate_transaction_id(transaction_id))


def save_journal(journal: MacUpdateJournal, appdata: Optional[str] = None) -> None:
    path = os.path.join(transaction_dir(journal.transaction_id, appdata), _JOURNAL_NAME)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(asdict(journal), handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_journal(transaction_id: str, appdata: Optional[str] = None) -> MacUpdateJournal:
    path = os.path.join(transaction_dir(transaction_id, appdata), _JOURNAL_NAME)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    expected = {field.name: field.type for field in fields(MacUpdateJournal)}
    if not isinstance(data, dict) or set(data) != set(expected):
        raise MacUpdateError("The update journal is damaged.")
    for name, kind in expected.items():
        value = data[name]
        if (kind == "int") != (isinstance(value, int) and not isinstance(value, bool)) or (
            kind == "str" and not isinstance(value, str)
        ):
            raise MacUpdateError("The update journal is damaged.")
    journal = MacUpdateJournal(**data)
    if journal.transaction_id != validate_transaction_id(transaction_id):
        raise MacUpdateError("The update journal does not match its folder.")
    return journal


def running_bundle(executable: Optional[str] = None) -> Optional[str]:
    """The ``.app`` bundle holding ``executable`` (this frozen app by default)."""
    if executable is None:
        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            return None
        executable = sys.executable
    macos_dir = os.path.dirname(os.path.realpath(executable))
    contents = os.path.dirname(macos_dir)
    bundle = os.path.dirname(contents)
    if (
        os.path.basename(macos_dir) != "MacOS"
        or os.path.basename(contents) != "Contents"
        or not bundle.endswith(".app")
        or not os.path.isfile(os.path.join(contents, "Info.plist"))
    ):
        return None
    return bundle


def bundle_info(bundle: str) -> dict:
    with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
        info = plistlib.load(handle)
    if not isinstance(info, dict):
        raise ValueError("Info.plist is not a dictionary")
    return info


def in_place_blocker(bundle: Optional[str]) -> Optional[str]:
    """Why this copy cannot replace itself, or None when it can."""
    if bundle is None:
        return "this copy is not an installed app bundle"
    if "/AppTranslocation/" in bundle:
        return "macOS runs this copy from a temporary location (App Translocation)"
    parent = os.path.dirname(bundle)
    try:
        if os.statvfs(parent).f_flag & os.ST_RDONLY:
            return "the app is on a read-only volume"
        owner = os.stat(bundle).st_uid
    except OSError as exc:
        return f"the app's folder cannot be read ({exc.strerror})"
    if owner != os.getuid():
        # Another account's files could be renamed but not removed afterwards.
        return "another account installed this copy"
    if not os.access(parent, os.W_OK | os.X_OK) or not os.access(bundle, os.W_OK):
        return f"this account cannot write to {parent}"
    return None


def _run(command: List[str], timeout: float = _COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )


def signature_intact(bundle: str) -> bool:
    return _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", bundle]).returncode == 0


def team_identifier(bundle: str) -> Optional[str]:
    """The Developer ID team that signed ``bundle``; None when ad-hoc signed."""
    details = _run(["/usr/bin/codesign", "-dv", "--verbose=2", bundle])
    match = re.search(r"^TeamIdentifier=(.+)$", details.stderr or "", re.MULTILINE)
    value = match.group(1).strip() if match else ""
    return None if not value or value == "not set" else value


def verify_candidate(candidate: str, *, version: str, bundle_id: str, team_id: Optional[str]) -> dict:
    """Check that ``candidate`` is this app at ``version`` with a sealed signature."""
    try:
        info = bundle_info(candidate)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise MacUpdateError("The update's app has no valid Info.plist.") from exc
    if info.get("CFBundleIdentifier") != bundle_id:
        raise MacUpdateError("The update is for a different app.")
    if normalize_version(str(info.get("CFBundleShortVersionString", ""))) != normalize_version(version):
        raise MacUpdateError("The update's version does not match the release.")
    executable = info.get("CFBundleExecutable")
    if (
        not isinstance(executable, str)
        or not executable
        or os.path.basename(executable) != executable
        or not os.access(os.path.join(candidate, "Contents", "MacOS", executable), os.X_OK)
    ):
        raise MacUpdateError("The update's app has no executable.")
    if not signature_intact(candidate):
        raise MacUpdateError("The update's code signature is not intact.")
    if team_id and team_identifier(candidate) != team_id:
        raise MacUpdateError("The update is not signed by the same developer as this copy.")
    return info


def _tree_size(root: str) -> int:
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise MacUpdateError("The download was canceled.")


def _attach(image: str, mountpoint: str) -> None:
    result = _run([
        "/usr/bin/hdiutil", "attach", image, "-readonly", "-nobrowse", "-noautoopen",
        "-mountpoint", mountpoint,
    ])
    if result.returncode:
        logger.warning("hdiutil attach failed: %s", (result.stderr or "").strip())
        raise MacUpdateError("The update disk image could not be opened.")


def _detach(mountpoint: str) -> None:
    for extra in ([], ["-force"]):
        try:
            if _run(["/usr/bin/hdiutil", "detach", mountpoint, "-quiet", *extra], timeout=60).returncode == 0:
                return
        except subprocess.TimeoutExpired:
            continue
    logger.warning("Could not detach the update disk image at %s", mountpoint)


def prepare_in_place_update(
    image: str,
    version: str,
    previous_version: str,
    *,
    cancel: Optional[threading.Event] = None,
    appdata: Optional[str] = None,
    bundle: Optional[str] = None,
) -> str:
    """Stage the verified update beside this app.

    Returns:
        ``macos:<transaction id>`` once the new bundle is staged, or ``image``
        unchanged when this copy cannot replace itself and must be updated
        by hand.

    Raises:
        MacUpdateError: The disk image or its app failed verification, or the
            copy could not be staged.
    """
    bundle = bundle if bundle is not None else running_bundle()
    blocker = in_place_blocker(bundle)
    if blocker:
        logger.info("Opening the Mac update for a manual install: %s", blocker)
        return image
    assert bundle is not None
    current = bundle_info(bundle)
    bundle_id = str(current.get("CFBundleIdentifier") or "")
    team_id = team_identifier(bundle)
    transaction_id = secrets.token_hex(16)
    tx = transaction_dir(transaction_id, appdata)
    mountpoint = os.path.join(tx, "mount")
    name = os.path.basename(bundle)[: -len(".app")]
    # Hidden and on the same volume, so the swap is a rename.
    staged = os.path.join(os.path.dirname(bundle), f".{name}-update-{transaction_id}.app")
    os.makedirs(mountpoint)
    try:
        _check_cancel(cancel)
        _attach(image, mountpoint)
        try:
            source = os.path.join(mountpoint, f"{APP_NAME}.app")
            if os.path.islink(source) or not os.path.isdir(source):
                raise MacUpdateError(f"The update disk image does not contain {APP_NAME}.app.")
            info = verify_candidate(source, version=version, bundle_id=bundle_id, team_id=team_id)
            needed = _tree_size(source)
            free = shutil.disk_usage(os.path.dirname(bundle)).free
            if free < needed * 1.1:
                raise MacUpdateError("There is not enough free disk space to install the update.")
            _check_cancel(cancel)
            if _run(["/usr/bin/ditto", source, staged]).returncode:
                raise MacUpdateError("The update could not be copied beside OpenWhisper.")
        finally:
            _detach(mountpoint)
        # Verify the copy itself, not only the image it came from.
        verify_candidate(staged, version=version, bundle_id=bundle_id, team_id=team_id)
        _check_cancel(cancel)
        save_journal(MacUpdateJournal(
            transaction_id=transaction_id,
            version=normalize_version(version),
            previous_version=normalize_version(previous_version),
            app_path=bundle,
            staged_path=staged,
            bundle_id=bundle_id,
            executable=str(info["CFBundleExecutable"]),
            health_token=secrets.token_hex(16),
        ), appdata)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(tx, ignore_errors=True)
        raise
    logger.info("Staged OpenWhisper %s beside %s", version, bundle)
    return encode_result(transaction_id)


def _clean_environment() -> dict:
    # Never hand a PyInstaller bootstrap environment to another bundle.
    from services.app_update_apply import _environment_without_bootstrap

    return _environment_without_bootstrap()


def _launch(executable: str, arguments: List[str], environment: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [executable, *arguments],
        env=environment,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def start_helper(transaction_id: str, appdata: Optional[str] = None) -> subprocess.Popen:
    """Launch the staged bundle's executable as the helper for this transaction."""
    journal = load_journal(transaction_id, appdata)
    if journal.state != State.PREPARED:
        raise MacUpdateError("This update was already started.")
    journal.parent_pid = os.getpid()
    save_journal(journal, appdata)
    executable = os.path.join(journal.staged_path, "Contents", "MacOS", journal.executable)
    process = _launch(executable, [
        HELPER_ARG, TRANSACTION_ARG, journal.transaction_id, PARENT_PID_ARG, str(os.getpid()),
    ], _clean_environment())
    journal.helper_pid = process.pid
    save_journal(journal, appdata)
    return process


def helper_ready(transaction_id: str, appdata: Optional[str] = None) -> bool:
    return os.path.isfile(os.path.join(transaction_dir(transaction_id, appdata), _READY_NAME))


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discard(transaction_id: str, appdata: Optional[str] = None) -> None:
    """Stop a staged update that will not be installed and remove its files."""
    try:
        journal = load_journal(transaction_id, appdata)
    except (OSError, ValueError, MacUpdateError):
        shutil.rmtree(transaction_dir(transaction_id, appdata), ignore_errors=True)
        return
    if journal.state not in (State.PREPARED, State.ABANDONED):
        return  # The helper owns a started swap.
    if journal.helper_pid and _process_alive(journal.helper_pid):
        try:
            os.kill(journal.helper_pid, signal.SIGTERM)
        except OSError:
            pass
    shutil.rmtree(journal.staged_path, ignore_errors=True)
    shutil.rmtree(transaction_dir(transaction_id, appdata), ignore_errors=True)


def write_health_acknowledgement(token: str, appdata: Optional[str] = None, bundle: Optional[str] = None) -> bool:
    """Tell the helper that this freshly installed version started properly."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return False
    bundle = bundle if bundle is not None else running_bundle()
    root = transactions_root(appdata)
    if bundle is None or not os.path.isdir(root):
        return False
    for name in os.listdir(root):
        try:
            journal = load_journal(name, appdata)
        except (OSError, ValueError, MacUpdateError):
            continue
        if (
            secrets.compare_digest(journal.health_token, token)
            and journal.state == State.SWAPPED
            and os.path.realpath(journal.app_path) == os.path.realpath(bundle)
        ):
            path = os.path.join(transaction_dir(name, appdata), _HEALTH_NAME)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            return True
    return False


def prune_transactions(appdata: Optional[str] = None, bundle: Optional[str] = None) -> List[str]:
    """Collect finished, abandoned or interrupted transactions at startup."""
    root = transactions_root(appdata)
    if not os.path.isdir(root):
        return []
    bundle = bundle if bundle is not None else running_bundle()
    removed = []
    for name in sorted(os.listdir(root)):
        try:
            tx = transaction_dir(name, appdata)
        except ValueError:
            continue
        try:
            journal = load_journal(name, appdata)
        except (OSError, ValueError, MacUpdateError):
            if time.time() - os.path.getmtime(tx) > 86400:
                shutil.rmtree(tx, ignore_errors=True)
                removed.append(name)
            continue
        if journal.helper_pid and _process_alive(journal.helper_pid):
            continue  # Still installing.
        if (journal.state == State.PREPARED and not journal.helper_pid
                and time.time() - os.path.getmtime(tx) < 3600):
            continue  # A running copy may be offering it right now.
        if journal.state == State.SWAPPED:
            if bundle is None or os.path.realpath(bundle) != os.path.realpath(journal.app_path):
                continue
            # The helper stopped after the swap, and the new version is the one
            # running now: its staged path holds the previous version.
            logger.info("Finishing the interrupted update to %s", journal.version)
        shutil.rmtree(journal.staged_path, ignore_errors=True)
        shutil.rmtree(tx, ignore_errors=True)
        removed.append(name)
    return removed


def _swapper() -> Callable[[str, str], None]:
    """Resolve the atomic exchange up front; the helper must not load code later."""
    library = ctypes.CDLL(_LIBSYSTEM, use_errno=True)
    renamex = library.renamex_np
    renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex.restype = ctypes.c_int

    def swap(first: str, second: str) -> None:
        if renamex(os.fsencode(first), os.fsencode(second), _RENAME_SWAP) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOTSUP, errno.EINVAL):
            raise OSError(error, os.strerror(error), first)
        # A volume without RENAME_SWAP (exFAT, for one) gets three renames.
        parking = first + ".swap"
        os.rename(first, parking)
        os.rename(second, first)
        os.rename(parking, second)

    return swap


def _setup_logging(appdata: Optional[str]) -> None:
    root = updates_root(appdata)
    os.makedirs(root, exist_ok=True)
    handler = logging.FileHandler(os.path.join(root, _LOG_NAME), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def _argument(argv: List[str], name: str) -> Optional[str]:
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return None


def _wait(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(_POLL_S)
    return condition()


def run_helper(
    argv: List[str],
    appdata: Optional[str] = None,
    *,
    swap: Optional[Callable[[str, str], None]] = None,
    launch: Callable[[str, List[str], dict], subprocess.Popen] = _launch,
    environment: Optional[dict] = None,
    parent_wait_s: float = _PARENT_WAIT_S,
    health_wait_s: float = _HEALTH_WAIT_S,
) -> int:
    """Install a staged update once the app that staged it has quit.

    This process runs from the staged bundle, and the swap moves that bundle's
    files: an import after it would read the other version's code. Everything
    used after the swap is therefore loaded first, and only already-loaded
    functions run afterwards.
    """
    transaction_id = _argument(argv, TRANSACTION_ARG) or ""
    try:
        parent_pid = int(_argument(argv, PARENT_PID_ARG) or "0")
        journal = load_journal(transaction_id, appdata)
    except (OSError, ValueError, MacUpdateError) as exc:
        logger.error("The Mac update helper could not start: %s", exc)
        return 2
    from services.app_update_apply import write_apply_error

    _setup_logging(appdata)
    swap = swap or _swapper()
    environment = environment if environment is not None else _clean_environment()
    tx = transaction_dir(transaction_id, appdata)
    health = os.path.join(tx, _HEALTH_NAME)
    # The same path runs the new version after the swap, and the previous
    # one again after a rollback.
    executable = os.path.join(journal.app_path, "Contents", "MacOS", journal.executable)
    restored_message = (
        f"OpenWhisper {journal.version} did not start correctly, so version "
        f"{journal.previous_version} was restored."
    )

    def save(state: str) -> None:
        journal.state = state
        save_journal(journal, appdata)

    def healthy() -> bool:
        try:
            with open(health, encoding="utf-8") as handle:
                return secrets.compare_digest(handle.read().strip(), journal.health_token)
        except OSError:
            return False

    # Exercise the post-swap path's lazily loaded pieces (codecs, encoders).
    save(journal.state)
    logger.info("Installing OpenWhisper %s over %s", journal.version, journal.previous_version)
    if journal.state != State.PREPARED or parent_pid != journal.parent_pid:
        logger.error("Refusing a transaction in state %s", journal.state)
        return 2
    with open(os.path.join(tx, _READY_NAME), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    if not _wait(lambda: not _process_alive(parent_pid), parent_wait_s):
        logger.warning("OpenWhisper did not quit; the update was not installed")
        save(State.ABANDONED)
        shutil.rmtree(journal.staged_path, ignore_errors=True)
        return 1

    try:
        if bundle_info(journal.app_path).get("CFBundleIdentifier") != journal.bundle_id:
            raise MacUpdateError("The installed app changed while the update waited.")
        swap(journal.app_path, journal.staged_path)
    except (OSError, ValueError, MacUpdateError, plistlib.InvalidFileException) as exc:
        logger.error("Could not swap in the update: %s", exc)
        save(State.ABANDONED)
        shutil.rmtree(journal.staged_path, ignore_errors=True)
        write_apply_error(f"The update to OpenWhisper {journal.version} could not be installed.", appdata)
        launch(executable, [], environment)
        return 1

    # --- From here on, no imports: the bundles have changed places. ---
    save(State.SWAPPED)
    process = None
    try:
        process = launch(executable, [HEALTH_ARG, journal.health_token], environment)
        started = _wait(lambda: healthy() or process.poll() is not None, health_wait_s) and healthy()
    except OSError as exc:
        logger.error("Could not start the new version: %s", exc)
        started = False
    if started:
        save(State.HEALTHY)
        shutil.rmtree(journal.staged_path, ignore_errors=True)
        shutil.rmtree(tx, ignore_errors=True)
        logger.info("OpenWhisper %s is installed", journal.version)
        return 0

    logger.error("OpenWhisper %s did not acknowledge a healthy start; rolling back", journal.version)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
    try:
        swap(journal.app_path, journal.staged_path)
    except OSError as exc:
        logger.error("Could not restore the previous version: %s", exc)
        write_apply_error(
            f"OpenWhisper {journal.version} did not start correctly. Reinstall OpenWhisper "
            "from its releases page.", appdata,
        )
        return 1
    save(State.ROLLED_BACK)
    shutil.rmtree(journal.staged_path, ignore_errors=True)
    write_apply_error(restored_message, appdata)
    launch(executable, [], environment)
    shutil.rmtree(tx, ignore_errors=True)
    return 1
