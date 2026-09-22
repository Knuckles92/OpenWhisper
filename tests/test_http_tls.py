import http.client
import ssl
import threading
import time
import urllib.request

from services import http_tls
from services.http_tls import new_verified_context, verified_context


def test_bundled_certificates_work_without_build_machine_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing.pem'))
    monkeypatch.setenv('SSL_CERT_DIR', str(tmp_path / 'missing-directory'))
    # Build a fresh one: the shared context may predate these variables.
    for context in (new_verified_context(), verified_context()):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        assert context.get_ca_certs()


def test_shared_context_is_built_once(monkeypatch):
    built = []
    monkeypatch.setattr(http_tls, "_shared_context", None)
    monkeypatch.setattr(http_tls, "new_verified_context",
                        lambda: built.append(object()) or built[-1])
    first = verified_context()
    assert verified_context() is first
    assert built == [first]


def test_concurrent_first_use_builds_once(monkeypatch):
    built = []

    def slow_build():
        time.sleep(0.05)  # widen the race window
        built.append(object())
        return built[-1]

    monkeypatch.setattr(http_tls, "_shared_context", None)
    monkeypatch.setattr(http_tls, "new_verified_context", slow_build)
    barrier = threading.Barrier(8)
    seen = []

    def worker():
        barrier.wait()
        seen.append(verified_context())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(built) == 1
    assert len(seen) == 8 and all(context is built[0] for context in seen)


def _settings(context):
    return (context.check_hostname, context.verify_mode, context.verify_flags,
            context.options, context.minimum_version, context.maximum_version,
            len(context.get_ca_certs()))


def test_stdlib_https_plumbing_leaves_the_shared_context_alone():
    # urlopen (TypeSafe proxy path, components), HTTPSHandler (app_update)
    # and HTTPSConnection (TypeSafe pool) all receive the shared context.
    context = verified_context()
    before = _settings(context)
    urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    http.client.HTTPSConnection("example.invalid", 443, timeout=1, context=context)
    assert _settings(context) == before
