# HASHI Releases and Distributions

[Install](INSTALL.md) · [Troubleshooting](TROUBLESHOOTING.md) ·
[Full changelog](https://github.com/Bazza1982/HASHI/blob/main/CHANGELOG.md)

## Current development and published releases

The source candidate is **v4.0.0-alpha.2**, encoded as **4.0.0a2** in Python
metadata. Preparing for Beta does not itself promote the version.

Publication snapshot checked on **2026-09-11**:

| Surface | Observed state |
|---|---|
| Source metadata | v4.0.0-alpha.2 / 4.0.0a2 |
| GitHub newest published pre-release | v4.0.0-alpha.1, an earlier Claw-era snapshot |
| GitHub stable Latest | v2.0.0, a historical release |
| npm latest | hashi-bridge 1.0.1, a legacy package |

This is a dated observation, not a permanent registry catalogue. Query the
registries for today's state:

~~~bash
gh release list --repo Bazza1982/HASHI
npm view hashi-bridge dist-tags --json
npm view hashi-bridge versions --json
~~~

## What Latest means

GitHub excludes drafts and pre-releases from its Latest selection. A newer
alpha or beta can therefore coexist with an older stable Latest. See
[GitHub's release API](https://docs.github.com/en/rest/releases/releases#update-a-release).

Historical releases are identified as legacy in their titles and introductory
notes. Their tags and original historical notes remain intact. Use the
[current README](https://github.com/Bazza1982/HASHI#readme) and the full
[Releases list](https://github.com/Bazza1982/HASHI/releases), rather than
assuming releases/latest contains current development.

The current Alpha candidate has not been declared stable simply to obtain a
Latest badge. A new release should identify the reviewed source commit and
its actual maturity.

## Choose an npm version

The package name is hashi-bridge; hashi is the executable name. A bare
installation selects npm's latest dist-tag, which is independent of GitHub.

After checking which versions exist, replace the placeholder with one
published version:

~~~bash
npm install --global hashi-bridge@<published-version>
~~~

If the publisher has created an alpha or beta dist-tag, that channel may be
selected explicitly. Do not assume a beta tag exists just because Beta work
is being prepared. New pre-releases should be published to their matching
channel; changing latest is a separate release decision.

When replacing a legacy 1.x installation, first locate and back up its
program-local data. Current managed-instance data preservation guarantees do
not retroactively describe every older installer. See
[upgrades](INSTALL.md#stop-remove-upgrade-and-uninstall-safety).

## Source, npm, and portable packages

| Distribution | What it provides |
|---|---|
| Git checkout / GitHub source archive | Source; requires the approved Python environment and dependencies |
| npm hashi-bridge | Program and command entry points; prepares a versioned Python environment using an available approved interpreter |
| Python source distribution / wheel | Extracted Nagare/Flow packages; not the complete HASHI application |
| Portable installer/image | A verified platform-specific program/runtime/dependency bundle with its own capability profile |

A portable installer built using npm inputs must record the exact npm version
and integrity, included runtime/dependencies, platform, and bundle checksum.
npm pack alone does not bundle a base interpreter or create an offline
installer. The
[Windows source builder](https://github.com/Bazza1982/HASHI/blob/main/packaging/portable_windows/README.md)
has a separate source/provenance contract.

## Release history and candidate notes

Full release history lives in
[CHANGELOG.md](https://github.com/Bazza1982/HASHI/blob/main/CHANGELOG.md).
Milestone descriptions record what existed then; older Workbench, Claw,
wrapper-mode, model, and hot-reload descriptions are not current instructions.

The current
[candidate notes](https://github.com/Bazza1982/HASHI/blob/main/docs/RELEASE_NOTES_v4.0.0-alpha.2.md)
describe intended scope and limitations. They are not evidence that a matching
GitHub Release, npm package, or installation bundle exists.

Maintainers use the
[versioning policy](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_VERSIONING_POLICY.md)
and [release checklist](https://github.com/Bazza1982/HASHI/blob/main/docs/RELEASE_CHECKLIST.md)
to record source identity, verification, maturity, and published artifacts.
