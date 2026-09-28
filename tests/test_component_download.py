"""Component and model downloads through the shared verified downloader."""
import hashlib
import io
import ssl
import threading
import urllib.error

import pytest

from services import components
from services.components import ComponentCanceled, ComponentError

BODY = b"component-payload"
URL = "https://files.pythonhosted.org/packages/example.whl"


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def fetch(tmp_path, progress=None, cancel=None, **kwargs):
    destination = tmp_path / "example.whl"
    components._download_verified(
        URL, hashlib.sha256(BODY).hexdigest(), len(BODY), str(destination),
        progress or (lambda *args: None), cancel or threading.Event(), **kwargs,
    )
    return destination


def test_progress_is_reported_across_all_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(components, "_open", lambda *args: Response(BODY))
    seen = []
    fetch(tmp_path, lambda *event: seen.append(event), offset_base=100, grand_total=500)
    assert seen[0] == ("downloading", 100 + len(BODY), 500)
    assert seen[-1] == ("verifying", 100 + len(BODY), 500)


def test_cancel_keeps_the_partial_for_resume(tmp_path, monkeypatch):
    cancel = threading.Event()

    class CancelAfterFirstRead(Response):
        def read(self, size=-1):
            chunk = super().read(8)
            cancel.set()
            return chunk

    monkeypatch.setattr(components, "_open", lambda *args: CancelAfterFirstRead(BODY))
    with pytest.raises(ComponentCanceled):
        fetch(tmp_path, cancel=cancel)
    assert (tmp_path / "example.whl.part").read_bytes() == BODY[:8]

    resumed = Response(BODY[8:], 206, {"Content-Range": f"bytes 8-{len(BODY) - 1}/{len(BODY)}"})
    calls = []
    monkeypatch.setattr(components, "_open", lambda *args: calls.append(args) or resumed)
    assert fetch(tmp_path).read_bytes() == BODY
    assert calls[0][1] == {"Range": "bytes=8-"}


def test_a_resume_with_the_wrong_range_is_discarded(tmp_path, monkeypatch):
    (tmp_path / "example.whl.part").write_bytes(BODY[:8])
    monkeypatch.setattr(
        components, "_open",
        lambda *args: Response(BODY[8:], 206, {"Content-Range": "bytes 0-9/17"}),
    )
    with pytest.raises(ComponentError, match="invalid resume response"):
        fetch(tmp_path)
    assert not (tmp_path / "example.whl.part").exists()


def test_a_corrupt_download_is_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(components, "_open", lambda *args: Response(b"x" * len(BODY)))
    with pytest.raises(ComponentError, match="integrity check"):
        fetch(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_network_failure_keeps_the_partial(tmp_path, monkeypatch):
    (tmp_path / "example.whl.part").write_bytes(BODY[:8])

    def offline(*args):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(components, "_open", offline)
    with pytest.raises(ComponentError, match="Could not reach the download server"):
        fetch(tmp_path)
    assert (tmp_path / "example.whl.part").read_bytes() == BODY[:8]


def test_certificate_failure_names_the_host_being_contacted(tmp_path, monkeypatch):
    """Payloads come from several hosts, so the message must name this one."""
    def intercepted(*args):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("self-signed"))

    monkeypatch.setattr(components, "_open", intercepted)
    with pytest.raises(ComponentError) as caught:
        fetch(tmp_path)
    message = str(caught.value)
    assert "certificate could not be verified" in message
    assert "allow files.pythonhosted.org." in message


def test_certificate_message_for_a_model_host():
    exc = urllib.error.URLError(ssl.SSLCertVerificationError("self-signed"))
    message = components._describe_network_error(
        exc, "https://huggingface.co/nvidia/model/resolve/main/model.gguf"
    )
    assert "allow huggingface.co." in message
    assert "pythonhosted" not in message
