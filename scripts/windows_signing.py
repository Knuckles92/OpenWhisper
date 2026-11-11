"""Optional Authenticode signing for the Windows build, and its proof.

Signing is configured by environment variables. With none set the build is
unsigned and says so; a partial or conflicting configuration is an error, so a
release never ships unsigned by accident when signing was intended.

Exactly one of:

* ``OPENWHISPER_SIGN_CERT_SHA1``: thumbprint of a code-signing certificate
  whose private key is in the Windows certificate store (an imported PFX, an
  OV/EV hardware token, or a cloud HSM that installs a key storage provider).
* ``OPENWHISPER_SIGN_PFX`` and ``OPENWHISPER_SIGN_PASS``: a PFX file. Local
  builds only; CI imports the PFX and uses the thumbprint instead.
* ``OPENWHISPER_SIGN_DLIB`` and ``OPENWHISPER_SIGN_METADATA``: Azure Trusted
  Signing through its SignTool dlib and the account/profile metadata JSON.
  Credentials come from ``AZURE_TENANT_ID``/``AZURE_CLIENT_ID``/
  ``AZURE_CLIENT_SECRET``.

``OPENWHISPER_SIGN_TIMESTAMP_URL`` overrides the RFC 3161 timestamp server.
Every signature is timestamped, so it stays valid after the certificate
expires.

Subcommands (stdlib only; ``scripts/build_installer.ps1`` and the release
workflow call them)::

    python scripts/windows_signing.py sign FILE...          # signtool sign
    python scripts/windows_signing.py inno-args             # ISCC /S and /D args
    python scripts/windows_signing.py verify --output R FILE...
    python scripts/windows_signing.py metadata --report R --release DIR
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence

NONE = "none"
CERTIFICATE = "certificate"
PFX = "pfx"
TRUSTED_SIGNING = "trusted-signing"

REPORT_NAME = "windows-signing.json"
REPORT_SCHEMA = 1
INNO_SIGN_TOOL = "openwhisper"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
TRUSTED_SIGNING_TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"

_SHA1_RE = re.compile(r"[0-9A-Fa-f]{40}")
_SETUP_RE = re.compile(r"OpenWhisper-Setup-\d+\.\d+\.\d+\.exe")


class SigningError(ValueError):
    """Signing is misconfigured, failed, or its result does not verify."""


def _env(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def signing_mode(env: Mapping[str, str] = os.environ) -> str:
    """Return the configured signing mode, or raise on a partial setup."""
    thumbprint = _env(env, "OPENWHISPER_SIGN_CERT_SHA1")
    pfx = _env(env, "OPENWHISPER_SIGN_PFX")
    dlib = _env(env, "OPENWHISPER_SIGN_DLIB")
    metadata = _env(env, "OPENWHISPER_SIGN_METADATA")
    modes = []
    if thumbprint:
        if not _SHA1_RE.fullmatch(thumbprint):
            raise SigningError("OPENWHISPER_SIGN_CERT_SHA1 is not a 40-digit thumbprint")
        modes.append(CERTIFICATE)
    if pfx or _env(env, "OPENWHISPER_SIGN_PASS"):
        if not pfx or not os.path.isfile(pfx):
            raise SigningError("OPENWHISPER_SIGN_PFX must name an existing PFX file")
        modes.append(PFX)
    if dlib or metadata:
        for name, value in (("OPENWHISPER_SIGN_DLIB", dlib), ("OPENWHISPER_SIGN_METADATA", metadata)):
            if not value or not os.path.isfile(value):
                raise SigningError(f"{name} must name an existing file for Trusted Signing")
        modes.append(TRUSTED_SIGNING)
    if len(modes) > 1:
        raise SigningError(f"Configure one signing method, not {' and '.join(modes)}")
    return modes[0] if modes else NONE


def timestamp_url(mode: str, env: Mapping[str, str] = os.environ) -> str:
    override = _env(env, "OPENWHISPER_SIGN_TIMESTAMP_URL")
    if override:
        return override
    return TRUSTED_SIGNING_TIMESTAMP_URL if mode == TRUSTED_SIGNING else DEFAULT_TIMESTAMP_URL


def sign_arguments(mode: str, env: Mapping[str, str] = os.environ) -> List[str]:
    """SignTool ``sign`` arguments up to, not including, the file names."""
    args = ["sign", "/fd", "SHA256", "/tr", timestamp_url(mode, env), "/td", "SHA256"]
    if mode == CERTIFICATE:
        args += ["/sha1", _env(env, "OPENWHISPER_SIGN_CERT_SHA1")]
    elif mode == PFX:
        args += ["/f", _env(env, "OPENWHISPER_SIGN_PFX"), "/p", env.get("OPENWHISPER_SIGN_PASS", "")]
    elif mode == TRUSTED_SIGNING:
        args += [
            "/dlib", _env(env, "OPENWHISPER_SIGN_DLIB"),
            "/dmdf", _env(env, "OPENWHISPER_SIGN_METADATA"),
        ]
    else:
        raise SigningError("Signing is not configured")
    return args


def _inno_literal(value: str) -> str:
    # Inno Setup expands $f, $q, $p and $$ in a SignTool command.
    return value.replace("$", "$$")


def inno_arguments(
    mode: str, signtool: Optional[str], env: Mapping[str, str] = os.environ
) -> List[str]:
    """ISCC arguments that make Inno sign the uninstaller and the setup exe."""
    if mode == NONE:
        return []
    parts = [f"$q{_inno_literal(signtool or '')}$q"]
    for arg in sign_arguments(mode, env):
        literal = _inno_literal(arg)
        parts.append(f"$q{literal}$q" if " " in arg else literal)
    parts.append("$f")
    return [f"/DSignToolName={INNO_SIGN_TOOL}", f"/S{INNO_SIGN_TOOL}={' '.join(parts)}"]


def find_signtool(env: Mapping[str, str] = os.environ) -> str:
    """Return SignTool from PATH or the newest Windows 10/11 SDK."""
    found = shutil.which("signtool.exe") or shutil.which("signtool")
    if found:
        return found
    roots = [
        env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        env.get("ProgramFiles") or r"C:\Program Files",
    ]
    candidates: List[str] = []
    for root in roots:
        candidates += glob.glob(os.path.join(root, "Windows Kits", "10", "bin", "*", "x64", "signtool.exe"))
    if not candidates:
        raise SigningError("signtool.exe not found; install the Windows SDK")

    def sdk_version(path: str):
        name = Path(path).parent.parent.name
        return tuple(int(part) for part in name.split(".")) if re.fullmatch(r"[\d.]+", name) else ()

    return max(candidates, key=sdk_version)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_report(
    mode: str,
    inspections: Sequence[Dict],
    *,
    signtool_verified: Mapping[str, bool],
    env: Mapping[str, str] = os.environ,
) -> Dict:
    """Decide whether the inspected files are consistently signed.

    Args:
        mode: The configured signing mode.
        inspections: One dict per file: ``name``, ``sha256``, ``status``
            (``Get-AuthenticodeSignature`` status), ``signer_subject``,
            ``signer_thumbprint``, ``timestamped``.
        signtool_verified: ``name -> signtool verify /pa`` succeeded.

    Raises:
        SigningError: Signing was configured and a file is unsigned, invalid,
            untimestamped or signed by another certificate; or signing was
            not configured and a file carries a signature anyway.
    """
    if not inspections:
        raise SigningError("No files to verify")
    if mode == NONE:
        signed = [item["name"] for item in inspections if item.get("status") != "NotSigned"]
        if signed:
            raise SigningError(f"Signing was not configured but these are signed: {', '.join(signed)}")
        return {"schema_version": REPORT_SCHEMA, "signed": False, "method": None, "files": list(inspections)}
    thumbprints = set()
    for item in inspections:
        name = item["name"]
        if item.get("status") != "Valid" or not signtool_verified.get(name):
            raise SigningError(f"{name} does not have a valid signature ({item.get('status')})")
        if not item.get("timestamped"):
            raise SigningError(f"{name} is signed without a timestamp")
        thumbprints.add(item.get("signer_thumbprint"))
    if len(thumbprints) != 1 or None in thumbprints:
        raise SigningError("Files are signed by different certificates")
    if mode == CERTIFICATE:
        configured = _env(env, "OPENWHISPER_SIGN_CERT_SHA1").upper()
        if configured and thumbprints != {configured}:
            raise SigningError("Files are not signed by the configured certificate")
    method = "trusted-signing" if mode == TRUSTED_SIGNING else "certificate"
    return {
        "schema_version": REPORT_SCHEMA,
        "signed": True,
        "method": method,
        "signer_subject": inspections[0].get("signer_subject"),
        "signer_thumbprint": inspections[0].get("signer_thumbprint"),
        "files": list(inspections),
    }


_INSPECT_PS = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:OW_SIGNED_FILE; "
    "[pscustomobject]@{ status = $s.Status.ToString(); "
    "signer_subject = $s.SignerCertificate.Subject; "
    "signer_thumbprint = $s.SignerCertificate.Thumbprint; "
    "timestamped = ($null -ne $s.TimeStamperCertificate) } | ConvertTo-Json -Compress"
)


def _inspect_windows(path: Path) -> Dict:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise SigningError("PowerShell is required to inspect signatures")
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", _INSPECT_PS],
        capture_output=True, text=True, check=True,
        env={**os.environ, "OW_SIGNED_FILE": str(path)},
    )
    return json.loads(completed.stdout)


def _signtool_verify(signtool: Optional[str], path: Path) -> bool:
    if not signtool:
        return False
    return subprocess.run([signtool, "verify", "/pa", "/tw", str(path)]).returncode == 0


def verify_files(
    paths: Iterable[Path],
    mode: str,
    *,
    inspect: Callable[[Path], Dict] = _inspect_windows,
    signtool_verify: Callable[[Path], bool],
) -> Dict:
    inspections = []
    verified = {}
    for path in paths:
        item = {"name": path.name, "sha256": _sha256(path), **inspect(path)}
        inspections.append(item)
        verified[path.name] = mode != NONE and signtool_verify(path)
    return build_report(mode, inspections, signtool_verified=verified)


def release_metadata(report: Mapping, release_dir: Path) -> Dict:
    """Return BUILD-METADATA fields for a verified signing report.

    The report must describe the exact setup exe in the release candidate, so
    a stale report from another build cannot vouch for it.
    """
    if not isinstance(report, Mapping) or report.get("schema_version") != REPORT_SCHEMA:
        raise SigningError("Unknown Windows signing report")
    signed = report.get("signed")
    if type(signed) is not bool:
        raise SigningError("Windows signing report has no signed flag")
    files = report.get("files")
    if not isinstance(files, list):
        raise SigningError("Windows signing report has no files")
    setups = sorted(path for path in release_dir.iterdir() if _SETUP_RE.fullmatch(path.name))
    if len(setups) != 1:
        raise SigningError("Release candidate needs exactly one setup exe")
    setup = setups[0]
    entry = next((item for item in files if isinstance(item, Mapping) and item.get("name") == setup.name), None)
    if entry is None or entry.get("sha256") != _sha256(setup):
        raise SigningError("Windows signing report does not match the setup exe")
    expected_status = "Valid" if signed else "NotSigned"
    if any(not isinstance(item, Mapping) or item.get("status") != expected_status for item in files):
        raise SigningError("Windows signing report is inconsistent")
    if not signed:
        return {"windows_signed": False}
    if report.get("method") not in ("certificate", "trusted-signing"):
        raise SigningError("Windows signing report has an unknown method")
    return {
        "windows_signed": True,
        "windows_signing": {
            "method": report["method"],
            "signer_subject": report.get("signer_subject"),
            "signer_thumbprint": report.get("signer_thumbprint"),
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sign = commands.add_parser("sign", help="sign files with the configured method")
    sign.add_argument("files", nargs="+", type=Path)
    commands.add_parser("inno-args", help="print ISCC signing arguments, one per line")
    verify = commands.add_parser("verify", help="verify signatures and write a report")
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("files", nargs="+", type=Path)
    metadata = commands.add_parser("metadata", help="print BUILD-METADATA signing fields")
    metadata.add_argument("--report", type=Path, required=True)
    metadata.add_argument("--release", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "metadata":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            print(json.dumps(release_metadata(report, args.release)))
            return 0
        mode = signing_mode()
        if args.command == "inno-args":
            for line in inno_arguments(mode, find_signtool() if mode != NONE else None):
                print(line)
            return 0
        if args.command == "sign":
            if mode == NONE:
                print("Not signed: no Windows signing method is configured. "
                      "SmartScreen will warn users on first run.")
                return 0
            command = [find_signtool(), *sign_arguments(mode), *map(str, args.files)]
            if subprocess.run(command).returncode != 0:
                raise SigningError("signtool sign failed")
            return 0
        signtool = find_signtool() if mode != NONE else None
        report = verify_files(
            args.files, mode, signtool_verify=lambda path: _signtool_verify(signtool, path)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        state = f"signed ({report['method']}, {report.get('signer_subject')})" if report["signed"] else "unsigned"
        print(f"Windows executables and installer: {state}")
        return 0
    except (OSError, SigningError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"Windows signing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
