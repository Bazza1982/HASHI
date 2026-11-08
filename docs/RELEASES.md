# HASHI Releases and Distributions

[Install](INSTALL.md) · [Troubleshooting](TROUBLESHOOTING.md) ·
[Full changelog](https://github.com/Bazza1982/HASHI/blob/main/CHANGELOG.md)

## Current development and published releases

The current development source is the **v5.0.0-alpha.1** Core/environment
separation candidate, encoded as **5.0.0a1** in Python metadata. See
[the v5 upgrade notes](RELEASE_NOTES_v5.0.0-alpha.1.md) for why the Core major
changed, other included changes and migration. It is not a stable production
release. Merging the candidate to `main` publishes source only.

Publication snapshot checked on **2026-11-07**:

| Surface | Observed state |
|---|---|
| Development source metadata | v5.0.0-alpha.1 / 5.0.0a1; candidate, not a published distribution |
| GitHub newest published pre-release | [v4.0.0-beta.1](https://github.com/Bazza1982/HASHI/releases/tag/v4.0.0-beta.1) |
| GitHub Beta installation package | [hashi-bridge-4.0.0-beta.1.tgz](https://github.com/Bazza1982/HASHI/releases/download/v4.0.0-beta.1/hashi-bridge-4.0.0-beta.1.tgz) |
| GitHub stable Latest | v2.0.0, a historical release |
| npm latest | hashi-bridge 1.0.1, a legacy package |

This is a dated observation, not a permanent registry catalogue. Query the
registries for today's state:

~~~bash
gh release list --repo Bazza1982/HASHI
npm view hashi-bridge dist-tags --json
npm view hashi-bridge versions --json
~~~

The published **v4 Beta** installation package remains available below. It
contains v4, not the v5 source candidate. No v5 GitHub Release or npm version
was present in the publication snapshot:

~~~bash
npm install --global https://github.com/Bazza1982/HASHI/releases/download/v4.0.0-beta.1/hashi-bridge-4.0.0-beta.1.tgz
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

Neither the published v4 Beta nor the v5 source candidate is declared stable
to obtain a Latest badge. A release must identify its exact reviewed source
commit and actual maturity.

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

The [v5 notes](RELEASE_NOTES_v5.0.0-alpha.1.md) describe the current source
candidate and its limits. The historical
[v4 Beta notes](https://github.com/Bazza1982/HASHI/blob/main/docs/RELEASE_NOTES_v4.0.0-beta.1.md)
describe the published GitHub pre-release and npm-format installation archive.
That GitHub asset does not imply an npm-registry publication or a v5 package.

Maintainers use the
[versioning policy](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_VERSIONING_POLICY.md)
and [release checklist](https://github.com/Bazza1982/HASHI/blob/main/docs/RELEASE_CHECKLIST.md)
to record source identity, verification, maturity, and published artifacts.
