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
from typing import Callable, Final, Mapping, Optional, Sequence

from _version import __version__
from services.format_utils import format_size_bytes
from services.verified_files import CHUNK_BYTES

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = f"OpenWhisper/{__version__}"
NETWORK_TIMEOUT_S: Final[int] = 30

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
):
    """Open ``url`` over HTTPS verified by ``context``, plus any ``handlers``.

    ``urllib`` is used because it honors Windows proxy settings and
    enterprise trust roots.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
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

    def canceled() -> DownloadCanceled:
        return DownloadCanceled(text["canceled"])

    if size_bytes <= 0 or (max_bytes is not None and size_bytes > max_bytes):
        raise DownloadError(text["invalid_size"])
    part_path = destination + ".part"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(part_path, flags, 0o600)
    except OSError as exc:
        raise DownloadError(text["unsafe_partial"]) from exc

    try:
        with os.fdopen(fd, "r+b") as out:
            info = os.fstat(out.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DownloadError(text["partial_not_regular"])
            try:
                os.chmod(part_path, 0o600, follow_symlinks=False)
            except (NotImplementedError, OSError):
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(out.fileno(), 0o600)

            resume_from = info.st_size
            if resume_from > size_bytes:
                out.seek(0)
                out.truncate(0)
                resume_from = 0

            # Re-hash what is already on disk, so the final comparison covers
            # the whole file and not just the bytes fetched this time.
            digest = hashlib.sha256()
            out.seek(0)
            remaining = resume_from
            while remaining:
                if cancel.is_set():
                    raise canceled()
                block = out.read(min(CHUNK_BYTES, remaining))
                if not block:
                    raise DownloadError(text["partial_unreadable"])
                digest.update(block)
                remaining -= len(block)

            if cancel.is_set():
                raise canceled()
            if resume_from == size_bytes and digest.hexdigest() != sha256_hex.lower():
                out.seek(0)
                out.truncate(0)
                resume_from = 0
                digest = hashlib.sha256()
            if resume_from < size_bytes:
                headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
                try:
                    response = opener(url, headers)
                except urllib.error.HTTPError as exc:
                    if exc.code != 416 or not resume_from:
                        raise
                    exc.close()
                    resume_from = 0
                    digest = hashlib.sha256()
                    out.seek(0)
                    out.truncate(0)
                    response = opener(url)
                with response:
                    status = getattr(response, "status", None)
                    if resume_from and status != 206:
                        # The server ignored Range and is sending the whole
                        # file. Restart rather than append a duplicate prefix.
                        logger.info("Server ignored Range header; restarting download")
                        resume_from = 0
                        digest = hashlib.sha256()
                        out.seek(0)
                        out.truncate(0)
                    elif resume_from:
                        content_range = response_header(response, "Content-Range") or ""
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                        if (
                            match is None
                            or int(match.group(1)) != resume_from
                            or int(match.group(2)) != size_bytes - 1
                            or int(match.group(3)) != size_bytes
                        ):
                            raise DownloadError(text["bad_resume"])

                    expected_response_bytes = size_bytes - resume_from
                    content_length = response_header(response, "Content-Length")
                    if content_length is not None:
                        try:
                            declared_response_bytes = int(content_length)
                        except ValueError as exc:
                            raise DownloadError(text["bad_length"]) from exc
                        if declared_response_bytes != expected_response_bytes:
                            raise DownloadError(text["unexpected_length"])

                    out.seek(resume_from)
                    written = resume_from
                    while True:
                        if cancel.is_set():
                            raise canceled()
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        if written + len(chunk) > size_bytes:
                            raise DownloadError(text["too_much_data"])
                        out.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        progress(DOWNLOADING, written, size_bytes)

            if cancel.is_set():
                raise canceled()
            actual_size = out.tell()
            if actual_size != size_bytes:
                raise DownloadInterrupted(text["incomplete"].format(
                    done=format_size_bytes(actual_size),
                    total=format_size_bytes(size_bytes),
                ))
            progress(VERIFYING, actual_size, actual_size)
            if cancel.is_set():
                raise canceled()
            if digest.hexdigest() != sha256_hex.lower():
                # Discarded below: a retry must not resume from corrupt bytes.
                raise DownloadError(text["integrity"])
            out.flush()
            os.fsync(out.fileno())
            if cancel.is_set():
                raise canceled()
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

    if cancel.is_set():
        if not keep_partial_on_cancel:
            discard_regular_file(part_path)
        raise canceled()
    try:
        os.replace(part_path, destination)
        try:
            os.chmod(destination, 0o600, follow_symlinks=False)
        except NotImplementedError:
            os.chmod(destination, 0o600)
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(os.path.dirname(destination), directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise DownloadError(text["finalize"]) from exc
    if cancel.is_set():
        if not keep_partial_on_cancel:
            discard_regular_file(destination)
        raise canceled()
