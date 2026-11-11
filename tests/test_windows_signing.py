"""Windows signing configuration, SignTool/Inno arguments, and the signing record.

SignTool and Authenticode only exist on Windows; these tests cover the
decisions around them with injected inspections.
"""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import windows_signing as ws

THUMB = "AB" * 20
OTHER_THUMB = "CD" * 20


@pytest.fixture
def files(tmp_path):
    pfx = tmp_path / "cert.pfx"
    dlib = tmp_path / "Azure.CodeSigning.Dlib.dll"
    metadata = tmp_path / "metadata.json"
    for path in (pfx, dlib, metadata):
        path.write_bytes(b"x")
    return {"pfx": str(pfx), "dlib": str(dlib), "metadata": str(metadata)}


class TestSigningMode:
    def test_nothing_configured_is_unsigned(self):
        assert ws.signing_mode({}) == ws.NONE

    def test_each_method(self, files):
        assert ws.signing_mode({"OPENWHISPER_SIGN_CERT_SHA1": THUMB}) == ws.CERTIFICATE
        assert ws.signing_mode({
            "OPENWHISPER_SIGN_PFX": files["pfx"], "OPENWHISPER_SIGN_PASS": "pw",
        }) == ws.PFX
        assert ws.signing_mode({
            "OPENWHISPER_SIGN_DLIB": files["dlib"],
            "OPENWHISPER_SIGN_METADATA": files["metadata"],
        }) == ws.TRUSTED_SIGNING

    @pytest.mark.parametrize("env", [
        {"OPENWHISPER_SIGN_CERT_SHA1": "not-a-thumbprint"},
        {"OPENWHISPER_SIGN_PASS": "password without a pfx"},
        {"OPENWHISPER_SIGN_PFX": "/nonexistent/cert.pfx"},
        {"OPENWHISPER_SIGN_DLIB": "/nonexistent/dlib.dll"},
    ])
    def test_partial_configuration_fails(self, env):
        with pytest.raises(ws.SigningError):
            ws.signing_mode(env)

    def test_trusted_signing_needs_both_files(self, files):
        with pytest.raises(ws.SigningError, match="METADATA"):
            ws.signing_mode({"OPENWHISPER_SIGN_DLIB": files["dlib"]})

    def test_two_methods_fail(self, files):
        with pytest.raises(ws.SigningError, match="one signing method"):
            ws.signing_mode({
                "OPENWHISPER_SIGN_CERT_SHA1": THUMB,
                "OPENWHISPER_SIGN_DLIB": files["dlib"],
                "OPENWHISPER_SIGN_METADATA": files["metadata"],
            })


class TestArguments:
    def test_every_method_hashes_and_timestamps_with_sha256(self, files):
        envs = {
            ws.CERTIFICATE: {"OPENWHISPER_SIGN_CERT_SHA1": THUMB},
            ws.PFX: {"OPENWHISPER_SIGN_PFX": files["pfx"], "OPENWHISPER_SIGN_PASS": "pw"},
            ws.TRUSTED_SIGNING: {
                "OPENWHISPER_SIGN_DLIB": files["dlib"],
                "OPENWHISPER_SIGN_METADATA": files["metadata"],
            },
        }
        for mode, env in envs.items():
            args = ws.sign_arguments(mode, env)
            assert args[:7] == ["sign", "/fd", "SHA256", "/tr", args[4], "/td", "SHA256"]
            assert args[4].startswith("http")
        assert "/sha1" in ws.sign_arguments(ws.CERTIFICATE, envs[ws.CERTIFICATE])
        trusted = ws.sign_arguments(ws.TRUSTED_SIGNING, envs[ws.TRUSTED_SIGNING])
        assert trusted[4] == ws.TRUSTED_SIGNING_TIMESTAMP_URL
        assert trusted[-4:] == ["/dlib", files["dlib"], "/dmdf", files["metadata"]]

    def test_timestamp_server_can_be_overridden(self):
        env = {"OPENWHISPER_SIGN_CERT_SHA1": THUMB,
               "OPENWHISPER_SIGN_TIMESTAMP_URL": "http://ts.example"}
        assert ws.sign_arguments(ws.CERTIFICATE, env)[4] == "http://ts.example"

    def test_unsigned_build_has_no_sign_arguments(self):
        with pytest.raises(ws.SigningError):
            ws.sign_arguments(ws.NONE, {})
        assert ws.inno_arguments(ws.NONE, None, {}) == []

    def test_inno_command_quotes_and_escapes(self, files):
        env = {"OPENWHISPER_SIGN_PFX": files["pfx"], "OPENWHISPER_SIGN_PASS": "pa$s word"}
        define, tool = ws.inno_arguments(ws.PFX, r"C:\Program Files (x86)\signtool.exe", env)
        assert define == "/DSignToolName=openwhisper"
        assert tool.startswith(r"/Sopenwhisper=$qC:\Program Files (x86)\signtool.exe$q sign ")
        assert "$qpa$$s word$q" in tool
        assert tool.endswith(" $f")


def test_find_signtool_prefers_the_newest_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(ws.shutil, "which", lambda name: None)
    for version in ("10.0.19041.0", "10.0.22621.0", "10.0.9200.0"):
        tool = tmp_path / "Windows Kits" / "10" / "bin" / version / "x64" / "signtool.exe"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"")
    found = ws.find_signtool({"ProgramFiles(x86)": str(tmp_path), "ProgramFiles": str(tmp_path / "none")})
    assert Path(found).parts[-3] == "10.0.22621.0"
    with pytest.raises(ws.SigningError):
        ws.find_signtool({"ProgramFiles(x86)": str(tmp_path / "a"), "ProgramFiles": str(tmp_path / "b")})


def _signed(name, *, status="Valid", thumb=THUMB, timestamped=True):
    return {"name": name, "sha256": "0" * 64, "status": status,
            "signer_subject": "CN=Fiori Labs", "signer_thumbprint": thumb,
            "timestamped": timestamped}


NAMES = ("OpenWhisper.exe", "OpenWhisperUpdater.exe", "OpenWhisper-Setup-2.9.0.exe")
ALL_VERIFIED = {name: True for name in NAMES}
CERT_ENV = {"OPENWHISPER_SIGN_CERT_SHA1": THUMB.lower()}


class TestReport:
    def test_signed_build(self):
        report = ws.build_report(ws.CERTIFICATE, [_signed(n) for n in NAMES],
                                 signtool_verified=ALL_VERIFIED, env=CERT_ENV)
        assert report["signed"] is True and report["method"] == "certificate"
        assert report["signer_thumbprint"] == THUMB

    def test_trusted_signing_method(self):
        report = ws.build_report(ws.TRUSTED_SIGNING, [_signed(n) for n in NAMES],
                                 signtool_verified=ALL_VERIFIED, env={})
        assert report["method"] == "trusted-signing"

    @pytest.mark.parametrize("broken", [
        _signed(NAMES[2], status="NotSigned"),
        _signed(NAMES[2], status="UnknownError"),
        _signed(NAMES[2], timestamped=False),
        _signed(NAMES[2], thumb=OTHER_THUMB),
    ])
    def test_any_bad_file_fails_a_signed_build(self, broken):
        inspections = [_signed(n) for n in NAMES[:2]] + [broken]
        with pytest.raises(ws.SigningError):
            ws.build_report(ws.CERTIFICATE, inspections, signtool_verified=ALL_VERIFIED, env={})

    def test_signtool_must_agree(self):
        with pytest.raises(ws.SigningError):
            ws.build_report(ws.CERTIFICATE, [_signed(n) for n in NAMES],
                            signtool_verified={**ALL_VERIFIED, NAMES[0]: False}, env={})

    def test_signature_must_come_from_the_configured_certificate(self):
        with pytest.raises(ws.SigningError, match="configured certificate"):
            ws.build_report(ws.CERTIFICATE, [_signed(n, thumb=OTHER_THUMB) for n in NAMES],
                            signtool_verified=ALL_VERIFIED, env=CERT_ENV)

    def test_unsigned_build(self):
        report = ws.build_report(ws.NONE, [_signed(n, status="NotSigned") for n in NAMES],
                                 signtool_verified={}, env={})
        assert report["signed"] is False and report["method"] is None

    def test_unsigned_build_with_a_stray_signature_fails(self):
        inspections = [_signed(NAMES[0])] + [_signed(n, status="NotSigned") for n in NAMES[1:]]
        with pytest.raises(ws.SigningError, match="not configured"):
            ws.build_report(ws.NONE, inspections, signtool_verified={}, env={})

    def test_verify_files_hashes_each_file(self, tmp_path):
        paths = []
        for name in NAMES:
            (tmp_path / name).write_bytes(name.encode())
            paths.append(tmp_path / name)
        report = ws.verify_files(
            paths, ws.TRUSTED_SIGNING,
            inspect=lambda path: {k: v for k, v in _signed(path.name).items()
                                  if k not in ("name", "sha256")},
            signtool_verify=lambda path: True,
        )
        assert [f["sha256"] for f in report["files"]] == [
            hashlib.sha256(name.encode()).hexdigest() for name in NAMES]


class TestReleaseMetadata:
    @pytest.fixture
    def release(self, tmp_path):
        directory = tmp_path / "release"
        directory.mkdir()
        (directory / NAMES[2]).write_bytes(b"setup")
        (directory / "OpenWhisper-2.9.0-win64.tar.xz").write_bytes(b"archive")
        return directory

    def _report(self, release, *, signed=True, setup_bytes=b"setup"):
        status = "Valid" if signed else "NotSigned"
        files = [dict(_signed(n, status=status)) for n in NAMES]
        files[2]["sha256"] = hashlib.sha256(setup_bytes).hexdigest()
        return {"schema_version": 1, "signed": signed,
                "method": "trusted-signing" if signed else None,
                "signer_subject": "CN=Fiori Labs" if signed else None,
                "signer_thumbprint": THUMB if signed else None, "files": files}

    def test_signed_release(self, release):
        assert ws.release_metadata(self._report(release), release) == {
            "windows_signed": True,
            "windows_signing": {"method": "trusted-signing", "signer_subject": "CN=Fiori Labs",
                                "signer_thumbprint": THUMB},
        }

    def test_unsigned_release(self, release):
        assert ws.release_metadata(self._report(release, signed=False), release) == {
            "windows_signed": False}

    def test_report_must_describe_this_setup_exe(self, release):
        with pytest.raises(ws.SigningError, match="does not match"):
            ws.release_metadata(self._report(release, setup_bytes=b"other build"), release)

    @pytest.mark.parametrize("change", [
        {"schema_version": 2}, {"signed": "yes"}, {"files": None}, {"method": "magic"},
    ])
    def test_malformed_reports_fail(self, release, change):
        with pytest.raises(ws.SigningError):
            ws.release_metadata({**self._report(release), **change}, release)

    def test_inconsistent_statuses_fail(self, release):
        report = self._report(release)
        report["files"][0]["status"] = "NotSigned"
        with pytest.raises(ws.SigningError, match="inconsistent"):
            ws.release_metadata(report, release)

    def test_cli(self, release, tmp_path, capsys):
        path = tmp_path / ws.REPORT_NAME
        path.write_text(json.dumps(self._report(release, signed=False)), encoding="utf-8")
        assert ws.main(["metadata", "--report", str(path), "--release", str(release)]) == 0
        assert json.loads(capsys.readouterr().out) == {"windows_signed": False}


def test_unsigned_sign_command_is_a_visible_no_op(monkeypatch, capsys, tmp_path):
    for name in ("OPENWHISPER_SIGN_CERT_SHA1", "OPENWHISPER_SIGN_PFX", "OPENWHISPER_SIGN_PASS",
                 "OPENWHISPER_SIGN_DLIB", "OPENWHISPER_SIGN_METADATA"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "OpenWhisper.exe"
    target.write_bytes(b"exe")
    assert ws.main(["sign", str(target)]) == 0
    assert "Not signed" in capsys.readouterr().out
    assert ws.main(["inno-args"]) == 0
    assert capsys.readouterr().out == ""
