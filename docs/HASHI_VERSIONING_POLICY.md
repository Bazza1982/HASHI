# HASHI Versioning and Distribution Policy

Status: accepted release-governance decision, 2026-09-09.
Initial documentation scope: HASHI1, `main`; applies prospectively to HASHI
product releases, not retroactively to historical tags.
Functional owner: PAO, for cross-module release/adoption operational policy.
Engineering placement: Functions release governance and Platform Configuration
packaging; this decision changes documentation, not Core or runtime behavior.

Parent authorities are [Architecture](../ARCHITECTURE.md),
[Layered Runtime Boundaries](HASHI_LAYERED_RUNTIME_BOUNDARIES.md), and
[Python Runtime Compatibility](HASHI_PYTHON_RUNTIME_COMPATIBILITY.md).
The [Release Checklist](RELEASE_CHECKLIST.md) consumes this policy; release
notes record individual outcomes rather than defining another naming rule.

## 1. One product version, separate distribution identity

The human-facing release label has this shape:

```text
X.Y.Z[-alpha.N|-beta.N|-rc.N][+build.metadata]
```

`X.Y.Z` means **Core generation . functional generation . maintenance revision**.
Each number is a non-negative integer without leading zeroes. `v` is an optional
display/Git-tag prefix, not part of the package version. Pre-release and build
identifiers follow [SemVer syntax](https://semver.org/spec/v2.0.0.html).

This is an architecture-based version policy, not a claim of strict SemVer
compatibility semantics. In particular, an unchanged `X` is not a guarantee
that all Functions, configuration, persisted data, or external APIs remain
compatible. Section 5 defines the separate compatibility evidence.

This policy governs the unified HASHI product line. Independent component,
workflow, protocol, manifest-schema, and Helm-chart versions retain their own
contracts; an application version must not silently replace those versions.

## 2. When to increment each number

| Number | Increment condition | Example |
|---|---|---|
| `X`: Core generation | An explicitly authorized change to Core implementation or its contracts, including a Core defect fix | `4.1.2` to `5.0.0` |
| `Y`: functional generation | With Core unchanged, a significant new capability or major behavior change in PCM, PAO, HER v2, or Frontend Connectors | `4.1.2` to `4.2.0` |
| `Z`: maintenance revision | A defect fix, small optimization, or compatibility maintenance that does not meet the `X` or `Y` criteria | `4.1.2` to `4.1.3` |

Apply these rules to the reviewed release scope, not to every commit:

1. Use the highest applicable increment once per release. Incrementing `X`
   resets `Y` and `Z` to zero; incrementing `Y` resets `Z` to zero.
2. Editing a module does not automatically increase `Y`. A small PAO bug fix
   is a `Z` change; a new PCM capability or HER execution policy may be a `Y`
   change. The release record states the functional owner and the observable
   change that justifies the classification, rather than counting files/lines.
3. Core scope derives solely from
   `orchestrator.runtime_contract.CORE_SOURCE_PATHS`, not a copied list or a
   permanent assumption that Core always contains nine files. An implementation
   change in protected Core belongs to `X`, even if described as a small fix.
4. Pure Core comments/formatting with no implementation or contract change do
   not alone increase `X`. They still require Core-edit authorization and may
   change the exact source fingerprint. A version label never bypasses the
   existing fingerprint, migration, or lifecycle gates.
5. Documentation-only work, tests with no shipped behavior change, and private
   instance choices do not themselves require a product version bump. Reviewed
   platform/packaging code fixes normally use `Z`; a significant shipped
   capability uses `Y` unless a Core change requires `X`.
6. A breaking Functions/API/configuration/data change with unchanged Core
   requires at least `Y` plus explicit migration/compatibility evidence. It must
   not be hidden in a maintenance release merely because Core stayed stable.
7. Ordinary commits do not each consume a release number. Once a release or
   pre-release is published, its tag and artifacts are immutable. Changed
   product code needs a new release/pre-release identifier, never an overwrite.

Changing a number is descriptive, not authorization to edit Core, publish,
restart, or broaden an adoption operation.

## 3. Maturity is independent of Core stability

Pre-release identifiers describe readiness of the whole target release:

| Label | Meaning |
|---|---|
| `4.2.0-alpha.1` | First Alpha pre-release of the target `4.2.0` release |
| `4.2.0-beta.1` | Target scope is sufficiently settled for broader validation |
| `4.2.0-rc.1` | Candidate for final release, subject to the declared release gates |
| `4.2.0` | Formal release whose declared gates and limitations have been reviewed |

For a fixed target, advance the stage counter for a new published candidate;
reset it when advancing to a new stage. Alpha, Beta, RC, and final describe
progression, not an obligation to publish every stage. Removing a pre-release
suffix requires whole-release acceptance, not merely a stable Core. Formal
release status does not certify an untested optional profile or deployment.

Package ecosystems may encode the same release differently. For example,
the existing public label `4.0.0-alpha.2` corresponds to Python `4.0.0a2` under
[Python version rules](https://packaging.python.org/en/latest/specifications/version-specifiers/).
Release preparation must verify equivalent identities across package metadata,
application labels, tags, and release notes; do not create competing product
versions merely to satisfy ecosystem syntax.

## 4. Portable is a distribution, not a fourth version number

| Distribution label | Meaning |
|---|---|
| `4.1.2` | Base HASHI product release |
| `4.1.2+portable` | Portable distribution built from that release baseline |
| `4.1.2+portable.2` | Explicitly numbered Portable build of the same product release |
| `4.2.0-alpha.2+portable` | Portable distribution of that Alpha candidate |

Do not use `4.1.2.p`: it is not the adopted version syntax. Do not use
`4.1.2-portable`: SemVer treats the suffix after `-` as a pre-release, whereas
Portable is a distribution form.

Build metadata after `+` does not participate in SemVer precedence. In
particular, a SemVer comparator does not establish that `+portable.2` is newer
than `+portable.1`. Distribution selection and rebuild updates need an explicit
build identity/revision and verified artifact provenance, not string sorting
or a product-version comparison alone.

Portable builds must identify the exact reviewed source baseline, platform,
architecture, dependency/runtime inputs, and distribution profile. A profile
may intentionally include only a subset of capabilities; the Portable label
must not imply that every source-checkout Engine or tool is bundled. Follow
the existing [Portable Windows builder contract](../packaging/portable_windows/README.md).

A pure repack of unchanged product inputs can retain `X.Y.Z` and receive a new
build identity. A packaging/installer behavior fix or product-code change must
be classified under section 2; build metadata must not conceal it. Never replace
an already published archive or tag in place. A descriptive archive name may
include platform/profile/build revision, but its checksum and manifest remain
the identity evidence.

The `+portable` convention is a distribution/release label. It does not require
putting build/local metadata into every public package registry's version field;
respect each ecosystem's rules and record the mapping to the base release.

## 5. Compatibility and provenance are separate facts

Every release record must include, or reference existing generated evidence for:

| Fact | Evidence to retain |
|---|---|
| Product identity | `X.Y.Z`, maturity stage, reviewed change classification, and ecosystem mappings |
| Source identity | Exact reviewed Git revision/tree; no claim that an uncommitted checkout is a published release |
| Core/runtime identity | Canonical protected-Core digest and runtime/ABI/API fingerprint from the existing runtime-contract owner |
| Functions identity | Qualified generation ID and qualification receipt for each distributed profile |
| Distribution identity | Platform, architecture, profile, build revision/identity, artifact checksum, and dependency/runtime input provenance |
| Compatibility | Supported Core/Functions contracts and configuration/data formats; required migrations, rollback limitations, and validation scope |

Reuse the existing runtime-contract, Function-generation, and packaging
manifests; do not introduce a second Core path list, compatibility catalogue,
state writer, or hand-maintained copy of a generated fingerprint. These are
release-evidence requirements, not a new implemented manifest schema. Missing
evidence must be marked pending, not fabricated.

`X` is not the Core API/Function API wire-protocol number. Python, dependency,
ABI, protected-source, and protocol compatibility remain governed by the
runtime-compatibility specification even when the product label is unchanged.
Sharing `X` or a release string never authorizes hot adoption across a rejected
fingerprint or substitutes for a configuration/data migration check.

Reports distinguish checked-out source, built/qualified artifact, running Core,
shared Functions generation, and per-Agent Worker generation. Different
instances may adopt the same release at different times. A repository version
edit or a successful offline check is not proof of running-version adoption.

## 6. Acceptance, implementation, and rollout boundary

- **Approval:** the user accepted the architecture-based numbering and Portable
  naming proposal on 2026-09-09 and requested documentation in HASHI1.
- **Documentation implementation:** this decision is recorded on HASHI1 `main`,
  with references from the documentation index, release checklist, and Agent
  FYI. Other checkouts must not be claimed updated without separate evidence.
- **Current version:** this documentation change does not rename or promote
  `v4.0.0-alpha.2`, change package versions, or rewrite historical tags. Future
  releases apply the policy relative to a recorded Core/source baseline.
- **Validation:** use the documentation-only scope in
  [Testing Policy](TESTING_POLICY.md): local link checks, `git diff --check`,
  and the protected-Core guard. Record actual results with the change; no
  runtime or package-release acceptance is implied.
- **Automation and live adoption:** this change implements no version-bump
  automation, new build-metadata parser, updater ordering, manifest schema, or
  runtime display. Those need separately scoped implementation and real
  boundary tests before support is claimed. No build, publication, `/fyi`
  refresh, `/reboot`, `/restart`, or shared-process replacement is performed
  merely by accepting this document.
