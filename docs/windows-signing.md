# Windows code signing

The **Windows installer** job in `.github/workflows/build-installers.yml` signs
the build when the repository has signing secrets, the same way the macOS job
notarizes when it has Apple's. Without them the build is unsigned, the run
shows a notice saying so, and nothing else changes.

When signing is configured, the build signs with SignTool, SHA-256 digests and
an RFC 3161 timestamp (so signatures stay valid after the certificate expires):

- `OpenWhisper.exe` and `OpenWhisperUpdater.exe`, before the native update
  archive and the installer are packed, so both carry the signed files;
- the uninstaller Inno Setup embeds, and the finished
  `OpenWhisper-Setup-<version>.exe` (Inno's `SignTool` directive).

It then checks the two executables and the setup exe with `signtool verify
/pa` and `Get-AuthenticodeSignature`: each must be valid, chain to a root
Windows trusts, carry a timestamp, and be signed by the same certificate. An
unsigned build is checked the other way: none of them may carry a signature.
The result is `build/windows-signing.json`, which the release bundle ties to the
exact setup exe (by SHA-256) and records in `BUILD-METADATA.json` as
`windows_signed` (and, when true, `windows_signing` with the method and
signer). **Verify candidate and upload draft** prints that status; set its
`require_windows_signed` input to refuse an unsigned candidate once signing is
set up.

## What the owner must add

Add **one** of these two sets under *Settings → Secrets and variables → Actions
→ Repository secrets*. Setting both fails the build; so does setting part of a
set.

### Option A: Azure Trusted Signing (recommended)

Microsoft's managed signing service. Signatures chain to a Microsoft root and build SmartScreen reputation like an
OV certificate. It needs an Azure subscription and identity validation of the
publisher (an organization, or an individual in supported countries).

1. Create a Trusted Signing account and a *Public Trust* certificate profile.
2. Create an app registration (service principal) with a client secret, and
   give it the **Trusted Signing Certificate Profile Signer** role on the
   account.
3. Add these secrets:

| Secret | Value |
| --- | --- |
| `AZURE_TRUSTED_SIGNING_TENANT_ID` | Directory (tenant) ID of the app registration |
| `AZURE_TRUSTED_SIGNING_CLIENT_ID` | Application (client) ID |
| `AZURE_TRUSTED_SIGNING_CLIENT_SECRET` | The client secret's value |
| `AZURE_TRUSTED_SIGNING_ENDPOINT` | The account's region endpoint, e.g. `https://eus.codesigning.azure.net` |
| `AZURE_TRUSTED_SIGNING_ACCOUNT` | The Trusted Signing account name |
| `AZURE_TRUSTED_SIGNING_PROFILE` | The certificate profile name |

The job installs the `Microsoft.Trusted.Signing.Client` NuGet package (version
pinned by `TRUSTED_SIGNING_CLIENT_VERSION` in the workflow) and signs through
its SignTool dlib, timestamping with `http://timestamp.acs.microsoft.com`.

### Option B: an OV or EV code-signing certificate as a PFX

| Secret | Value |
| --- | --- |
| `WINDOWS_CERTIFICATE_PFX` | The `.pfx` file, base64-encoded (`[Convert]::ToBase64String([IO.File]::ReadAllBytes('cert.pfx'))`) |
| `WINDOWS_CERTIFICATE_PASSWORD` | Its password |

The job imports it into the runner's user certificate store, signs by
thumbprint, timestamps with `http://timestamp.digicert.com`, and deletes the
key at the end of the job.

Since June 2023 certificate authorities issue new OV and EV code-signing keys
only on hardware tokens or cloud HSMs, so most new certificates cannot be
exported as a PFX. A cloud HSM whose Windows key storage provider can be
installed on the runner works by setting `OPENWHISPER_SIGN_CERT_SHA1` to the
certificate's thumbprint instead; that needs a workflow change for the
provider's own setup. EV certificates no longer skip SmartScreen's reputation
check, so an OV certificate or Trusted Signing gives the same first-run
experience.

## Local builds

`scripts\build_installer.ps1` reads the same configuration from the
environment (see `scripts/windows_signing.py`): `OPENWHISPER_SIGN_CERT_SHA1`
for a certificate in the store or on a token, `OPENWHISPER_SIGN_PFX` with
`OPENWHISPER_SIGN_PASS`, or `OPENWHISPER_SIGN_DLIB` with
`OPENWHISPER_SIGN_METADATA` for Trusted Signing.
`OPENWHISPER_SIGN_TIMESTAMP_URL` overrides the timestamp server.

## What has not been run

No certificate was available when this was written, so neither signing path
has run end to end. The configuration, argument building, Inno quoting, report
validation and release metadata are covered by `tests/test_windows_signing.py`
and `tests/test_release_qualification.py`. Untested: the PFX import, the
NuGet package version and dlib path, Trusted Signing authentication, Inno's
`/S` command handling, and the PowerShell `Get-AuthenticodeSignature`
inspection on a real signature. The first signed build should be checked by
hand: right-click the setup exe → *Properties → Digital Signatures*, and
`signtool verify /pa /v` on it and on the installed `OpenWhisper.exe`.
