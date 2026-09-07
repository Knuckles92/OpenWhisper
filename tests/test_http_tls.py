import ssl

from services.http_tls import verified_context


def test_bundled_certificates_work_without_build_machine_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing.pem'))
    monkeypatch.setenv('SSL_CERT_DIR', str(tmp_path / 'missing-directory'))
    context = verified_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.get_ca_certs()
