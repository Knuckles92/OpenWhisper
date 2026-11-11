# Beta releases

A release can soak with volunteers before every installed copy is offered it.
Users opt in with **Settings → App → General → Updates → Get beta updates**;
everyone else is offered only regular releases, exactly as before.

## How it works

- A beta is an ordinary release published on GitHub as a **prerelease**. Its
  tag is a plain `vMAJOR.MINOR.PATCH` like any other, and its assets are the
  same verified candidate the release workflows produce. Promoting it to a
  regular release flips one flag; nothing is rebuilt and every digest stays
  the same.
- Copies on the regular track read `/releases/latest`, which GitHub never
  points at a prerelease.
- Copies on the beta track read the 30 most recent releases and are offered
  the one with the highest version, beta or regular. Drafts and tags with a
  suffix (`v2.9.0-rc.1`) are ignored. A prerelease's assets pass the same
  name, URL, digest, size and redirect checks as a regular release's.
- When GitHub's API is rate-limited, the app falls back to the API-free
  latest-release redirect, which only knows regular releases. A beta copy is
  then offered regular releases only until the API answers again.
- **Leaving the beta track never downgrades.** A copy running beta 2.9.0
  after the user turns beta updates off reports a development build until a
  regular release with the same or a higher version is published. Once 2.9.0
  is promoted it is up to date; 2.9.1 is offered as usual.
- Turning beta updates on or off makes the next automatic check due straight
  away, so the offer in the title bar follows the choice. Any rate-limit
  cooldown still applies.

## Publishing a beta

1. Bump `_version.py` and the changelog as for any release, tag
   `vX.Y.Z`, and create its draft release.
2. Build and qualify as usual: **Build native installers** with
   `release_tag=vX.Y.Z`, then **Verify candidate and upload draft** (see
   [Release health](release-health.md#verify-a-candidate-before-uploading-a-release-draft)).
   Nothing in the build is beta-specific.
3. Publish the draft as a prerelease, keeping the previous regular release
   as GitHub's latest:

   ```sh
   gh release edit vX.Y.Z --draft=false --prerelease --latest=false
   gh api repos/Knuckles92/OpenWhisper/releases/latest --jq .tag_name   # still the previous release
   ```

   Or, on the draft's page, tick **Set as a pre-release** and publish.

## Promoting it

When the beta has soaked long enough:

```sh
gh release edit vX.Y.Z --prerelease=false --latest
gh api repos/Knuckles92/OpenWhisper/releases/latest --jq .tag_name   # now vX.Y.Z
```

Every copy is then offered vX.Y.Z; beta testers already on it are up to date.

## When a beta is bad

Do not promote it, and never replace a published release's assets or reuse
its tag. Fix forward: publish vX.Y.(Z+1) as a new prerelease (or straight to
regular). It supersedes the bad beta for beta testers because its version is
higher, and copies that left the beta track get it once it is regular. The
bad prerelease can stay published or be deleted; nothing offers it once a
higher version exists.

A fix released only on the regular line with a *lower* version than the
current beta (2.8.10 while 2.9.0 is in beta) does not reach beta testers.
Ship the fix in the next beta too, or promote.

## Soaking the beta path locally

`scripts/serve_update_feed.py --prerelease` serves a local build as a
prerelease on the loopback feed. Only a copy with **Get beta updates** on may
install it; any other copy is told about the version but offered no
download.
