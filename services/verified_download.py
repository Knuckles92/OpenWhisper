"""Resumable, SHA-256-verified downloads.

The one download path for application updates (``services.app_update``) and
optional components and speech models (``services.components``). A transfer
lands in ``<destination>.part``, resumes with a checked ``Range`` request,
must match the pinned size and digest, and is fsynced and renamed into place
only once it does. Callers pass the opener, so each keeps its own TLS trust
and redirect rules, and may override any message.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import stat
import threading
import urllib.error
import urllib.request
from types import MappingProxyType
from typing import BinaryIO, Callable, Final, Mapping, Optional, Sequence

from _version import __version__
from services.format_utils import format_size_bytes
from services.verified_files import CHUNK_BYTES

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
NETWORK_TIMEOUT_S: Final[int] = 30

_PART_SUFFIX: Final[str] = ".part"
_PRIVATE_MODE: Final[int] = 0o600
_HTTP_PARTIAL_CONTENT: Final[int] = 206
_HTTP_RANGE_NOT_SATISFIABLE: Final[int] = 416
_CONTENT_RANGE: Final[re.Pattern[str]] = re.compile(r"bytes (\d+)-(\d+)/(\d+)")

# Progress phases, reported as (phase, done bytes, total bytes).
DOWNLOADING: Final[str] = "downloading"
VERIFYING: Final[str] = "verifying"

ProgressCallback = Callable[[str, int, int], None]
# ``opener(url, headers)`` returns an open HTTP response; ``headers`` may be
# None, and the 416 restart calls ``opener(url)``.
Opener = Callable[..., object]

MESSAGES: Final[Mapping[str, str]] = MappingProxyType({
    "invalid_size": "The download has an invalid size.",
    "unsafe_partial": "The download could not be created safely.",
    "partial_not_regular": "The partial download is not a regular file.",
    "partial_unreadable": "The partial download could not be read.",
    "canceled": "The download was canceled.",
    "bad_resume": "The download server returned an invalid resume response.",
    "bad_length": "The download server returned an invalid download size.",
    "unexpected_length": "The download server returned an unexpected download size.",
    "too_much_data": "The download server returned more data than expected.",
    "incomplete": "The download did not complete ({done} of {total}).",
    "integrity": (
        "The download failed its integrity check and was discarded. "
        "Please try again."
    ),
    "finalize": "The verified download could not be finalized. Please retry.",
})


class DownloadError(Exception):
    """A download failed. The message is written for the user."""


class DownloadInterrupted(DownloadError):
    """The transfer stopped short; its prefix stays on disk for a retry to resume."""


class DownloadCanceled(DownloadError):
    """The cancel event was set."""


def open_url(
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    *,
    context,
    handlers: Sequence[urllib.request.BaseHandler] = (),
    user_agent: str = USER_AGENT,
    timeout: float = NETWORK_TIMEOUT_S,
    method: str = "GET",
):
    """Open ``url`` over HTTPS verified by ``context``, plus any ``handlers``.

    ``urllib`` is used because it honors Windows proxy settings and
    enterprise trust roots.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent}, method=method
    )
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = urllib.request.build_opener(
        *handlers, urllib.request.HTTPSHandler(context=context)
    )
    return opener.open(request, timeout=timeout)


def response_header(response: object, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get(name)
        return str(value) if value is not None else None
    getter = getattr(response, "getheader", None)
    if getter is not None:
        value = getter(name)
        return str(value) if value is not None else None
    return None


def discard_regular_file(path: str) -> None:
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            os.unlink(path)
    except OSError:
        pass


def describe_network_error(
    exc: BaseException,
    *,
    server: str = "download server",
    hosts: Sequence[str] = (),
    http_messages: Optional[Mapping[int, str]] = None,
) -> str:
    """Translate a urllib or disk failure into something a user can act on.

    Args:
        exc: The failure raised while opening or reading a URL.
        server: What was being contacted, as in "Could not reach the <server>".
        hosts: Hosts to ask IT to allow when certificate verification fails;
            empty names ``server`` instead.
        http_messages: Replacement copy for specific HTTP status codes.
    """
    # urllib reports a failed handshake as a URLError wrapping the SSL error.
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        getattr(exc, "reason", None), ssl.SSLCertVerificationError
    ):
        if not hosts:
            allow = f"the {server}"
        elif len(hosts) == 1:
            allow = hosts[0]
        else:
            allow = f"{', '.join(hosts[:-1])} and {hosts[-1]}"
        return (
            f"The {server}'s certificate could not be verified. This is "
            "usually caused by network security software that inspects HTTPS "
            f"traffic. Ask your IT team to allow {allow}."
        )
    if isinstance(exc, urllib.error.HTTPError):
        override = (http_messages or {}).get(exc.code)
        return override or f"The {server} returned an error ({exc.code} {exc.reason})."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach the {server} ({exc.reason})."
    return str(exc) or f"Could not reach the {server}."


def _open_partial(part_path: str, text: Mapping[str, str]) -> int:
    """Open or create ``part_path`` for read/write, refusing to follow a symlink."""
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(part_path, flags, _PRIVATE_MODE)
    except OSError as exc:
        raise DownloadError(text["unsafe_partial"]) from exc


def _make_private(path: str, out: BinaryIO) -> None:
    try:
        os.chmod(path, _PRIVATE_MODE, follow_symlinks=False)
    except (NotImplementedError, OSError):
        # Not every platform can chmod without following symlinks; the open
        # descriptor is the same file, so fall back to it where possible.
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(out.fileno(), _PRIVATE_MODE)


def _move_into_place(part_path: str, destination: str) -> None:
    os.replace(part_path, destination)
    try:
        os.chmod(destination, _PRIVATE_MODE, follow_symlinks=False)
    except NotImplementedError:
        os.chmod(destination, _PRIVATE_MODE)
    if os.name != "nt":
        # Persist the rename itself. Windows cannot open a directory to fsync it.
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(os.path.dirname(destination), directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _raise_if_canceled(
    cancel: threading.Event,
    path: str,
    keep_partial_on_cancel: bool,
    text: Mapping[str, str],
) -> None:
    """If ``cancel`` is set, discard ``path`` (unless kept) and raise."""
    if not cancel.is_set():
        return
    if not keep_partial_on_cancel:
        discard_regular_file(path)
    raise DownloadCanceled(text["canceled"])


class _PartialFile:
    """The open ``.part`` file of one download and the SHA-256 of its bytes.

    ``digest`` covers every byte in the file, including any kept from an
    earlier attempt, so the final check is over the whole download.
    """

    def __init__(
        self,
        out: BinaryIO,
        sha256_hex: str,
        size_bytes: int,
        cancel: threading.Event,
        text: Mapping[str, str],
    ) -> None:
        self.out = out
        self.sha256_hex = sha256_hex
        self.size_bytes = size_bytes
        self.cancel = cancel
        self.text = text
        self.resume_from = 0
        self.digest = hashlib.sha256()

    def check_canceled(self) -> None:
        if self.cancel.is_set():
            raise DownloadCanceled(self.text["canceled"])

    def digest_matches(self) -> bool:
        return self.digest.hexdigest() == self.sha256_hex.lower()

    def restart(self) -> None:
        """Drop everything on disk so the download starts again from byte 0."""
        self.resume_from = 0
        self.digest = hashlib.sha256()
        self.out.seek(0)
        self.out.truncate(0)

    def load_existing(self, part_path: str) -> None:
        """Check the file is safe to use and hash what an earlier attempt left."""
        info = os.fstat(self.out.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadError(self.text["partial_not_regular"])
        _make_private(part_path, self.out)

        self.resume_from = info.st_size
        if self.resume_from > self.size_bytes:
            self.restart()

        # Re-hash what is already on disk, so the final comparison covers
        # the whole file and not just the bytes fetched this time.
        self.digest = hashlib.sha256()
        self.out.seek(0)
        remaining = self.resume_from
        while remaining:
            self.check_canceled()
            block = self.out.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise DownloadError(self.text["partial_unreadable"])
            self.digest.update(block)
            remaining -= len(block)

    def fetch_remainder(
        self, url: str, opener: Opener, progress: ProgressCallback
    ) -> None:
        """Download the bytes after ``resume_from`` and append them."""
        response = self._open_response(url, opener)
        with response:
            self._check_response(response)
            self._stream(response, progress)

    def _open_response(self, url: str, opener: Opener):
        headers = (
            {"Range": f"bytes={self.resume_from}-"} if self.resume_from else None
        )
        try:
            return opener(url, headers)
        except urllib.error.HTTPError as exc:
            # The server cannot serve the rest; fetch the whole file instead.
            if exc.code != _HTTP_RANGE_NOT_SATISFIABLE or not self.resume_from:
                raise
            exc.close()
            self.restart()
            return opener(url)

    def _check_response(self, response: object) -> None:
        status = getattr(response, "status", None)
        if self.resume_from and status != _HTTP_PARTIAL_CONTENT:
            # The server ignored Range and is sending the whole
            # file. Restart rather than append a duplicate prefix.
            logger.info("Server ignored Range header; restarting download")
            self.restart()
        elif self.resume_from:
            self._check_content_range(response)
        self._check_content_length(response)

    def _check_content_range(self, response: object) -> None:
        content_range = response_header(response, "Content-Range") or ""
        match = _CONTENT_RANGE.fullmatch(content_range)
        expected = (self.resume_from, self.size_bytes - 1, self.size_bytes)
        if match is None or tuple(int(group) for group in match.groups()) != expected:
            raise DownloadError(self.text["bad_resume"])

    def _check_content_length(self, response: object) -> None:
        content_length = response_header(response, "Content-Length")
        if content_length is None:
            return
        try:
            declared_response_bytes = int(content_length)
        except ValueError as exc:
            raise DownloadError(self.text["bad_length"]) from exc
        if declared_response_bytes != self.size_bytes - self.resume_from:
            raise DownloadError(self.text["unexpected_length"])

    def _stream(self, response, progress: ProgressCallback) -> None:
        self.out.seek(self.resume_from)
        written = self.resume_from
        while True:
            self.check_canceled()
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                return
            if written + len(chunk) > self.size_bytes:
                raise DownloadError(self.text["too_much_data"])
            self.out.write(chunk)
            self.digest.update(chunk)
            written += len(chunk)
            progress(DOWNLOADING, written, self.size_bytes)

    def verify(self, progress: ProgressCallback) -> None:
        """Check the size and digest, then flush the file to disk."""
        self.check_canceled()
        actual_size = self.out.tell()
        if actual_size != self.size_bytes:
            raise DownloadInterrupted(self.text["incomplete"].format(
                done=format_size_bytes(actual_size),
                total=format_size_bytes(self.size_bytes),
            ))
        progress(VERIFYING, actual_size, actual_size)
        self.check_canceled()
        if not self.digest_matches():
            # Discarded by the caller: a retry must not resume from corrupt bytes.
            raise DownloadError(self.text["integrity"])
        self.out.flush()
        os.fsync(self.out.fileno())
        self.check_canceled()


def download_verified(
    url: str,
    sha256_hex: str,
    size_bytes: int,
    destination: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    opener: Opener,
    describe_error: Callable[[Exception], str],
    max_bytes: Optional[int] = None,
    keep_partial_on_cancel: bool = False,
    messages: Optional[Mapping[str, str]] = None,
) -> None:
    """Fetch ``url`` to ``destination`` with bounded resume, exact-size and SHA-256 checks.

    Args:
        url: File URL, opened with ``opener``.
        sha256_hex: Expected SHA-256, hex.
        size_bytes: Exact expected size; must be positive.
        destination: Final path for the verified file.
        progress: Receives ``(phase, done, size_bytes)`` for this file.
        cancel: Checked between chunks and before every commit step.
        opener: Opens the request; see :data:`Opener`.
        describe_error: Turns a network or disk error into a user message.
        max_bytes: Largest ``size_bytes`` accepted, or None for no cap.
        keep_partial_on_cancel: Leave a canceled transfer on disk so the next
            attempt resumes it, rather than discarding it.
        messages: Replacements for any :data:`MESSAGES` entry.

    Raises:
        DownloadCanceled: ``cancel`` was set.
        DownloadInterrupted: The server sent too little; the prefix is kept.
        DownloadError: Any other failure. A partial that cannot be trusted is
            discarded; one cut short by a network error is kept for resume.
    """
    text = {**MESSAGES, **(messages or {})}
    if size_bytes <= 0 or (max_bytes is not None and size_bytes > max_bytes):
        raise DownloadError(text["invalid_size"])
    part_path = destination + _PART_SUFFIX
    fd = _open_partial(part_path, text)

    try:
        with os.fdopen(fd, "r+b") as out:
            partial = _PartialFile(out, sha256_hex, size_bytes, cancel, text)
            partial.load_existing(part_path)
            partial.check_canceled()
            if partial.resume_from == size_bytes and not partial.digest_matches():
                partial.restart()
            if partial.resume_from < size_bytes:
                partial.fetch_remainder(url, opener, progress)
            partial.verify(progress)
    except DownloadInterrupted:
        raise
    except DownloadCanceled:
        if not keep_partial_on_cancel:
            discard_regular_file(part_path)
        raise
    except DownloadError:
        discard_regular_file(part_path)
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(describe_error(exc)) from exc

    _raise_if_canceled(cancel, part_path, keep_partial_on_cancel, text)
    try:
        _move_into_place(part_path, destination)
    except OSError as exc:
        raise DownloadError(text["finalize"]) from exc
    _raise_if_canceled(cancel, destination, keep_partial_on_cancel, text)
