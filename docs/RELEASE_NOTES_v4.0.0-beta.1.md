# HASHI v4.0.0-beta.1 — Beta Release

Release date: 2026-09-21
Status: small-scale public Beta testing

## Release identity

| Surface | Version |
|---|---|
| Public source / npm / Helm label | `4.0.0-beta.1` |
| Python package metadata | `4.0.0b1` |
| Short human description | HASHI 4.0.0 Beta |

The Beta label marks a broader testing milestone. It is not a claim of general
production readiness, a security certification, or proof that every registry
and installer has already published this version.

## Why this is ready for Beta

- **External-client integration.** Extended integration and debugging with the
  independently maintained Workbench v2 client hardened HASHI's Backend API,
  Persistent Session API, conversation continuity, delivery, attachments, and
  lifecycle boundaries. Workbench v2 remains a separate product and is not
  bundled with HASHI.
- **Windows and WSL/Linux deployment.** Parameterized deployment templates now
  cover native Windows and WSL-hosted HASHI source checkouts, including exact
  instance identity, user-scoped startup, separate logs, and fail-closed
  validation.
- **Restricted online demo mode.** The shared Demo Connector provides isolated
  anonymous leases, text-only HER v2 Direct runs, bounded workers and budgets,
  cancellation, cleanup, and a deliberately narrow public event projection.
  It enables an operator to host a simple online demo without turning the demo
  into a second runtime.
- **Portable Windows builder.** HASHI can be carried as verified USB
  installation media and installed quickly on a new Windows PC with its own
  Python runtime and clean local identity. The USB is transfer/install media;
  the verified runtime executes from the destination PC.
- **Runtime hardening.** The Beta line includes sustained fixes across HER v2,
  PCM/PAO conversation binding, cross-channel delivery, media handling,
  Function adoption, Remote recovery, Windows launchers, and release
  qualification.

## Included testing paths

Beta testers can use:

- source installation on the approved Python runtime;
- native Windows or WSL/Linux user-runtime deployment templates;
- the built-in TUI, Telegram, WhatsApp, or authenticated client APIs;
- a compatible external client such as Workbench v2;
- the restricted Demo Connector for an operator-hosted public demonstration;
  and
- the Portable Windows builder for verified USB installation media.

See [Installation](INSTALL.md), [Releases and distributions](RELEASES.md),
[Windows deployment assets](../packaging/windows/README.md),
[Shared Demo Mode](demo-mode/README.md), and
[Portable Windows](../packaging/portable_windows/README.md).

## Beta boundaries

- Public hosting, credentials, DNS/TLS, rate limits, monitoring, and provider
  policy remain the operator's responsibility.
- Demo Mode is intentionally text-only, tool-free, memory-disabled, bounded,
  and disposable. A merged connector is not proof of a live public canary or a
  tested 200-user workload.
- Workbench v2 is integration evidence for HASHI's public client contracts,
  not a HASHI-owned frontend or bundled dependency.
- The Portable Windows image is installation media, not a promise to execute
  HASHI directly from the USB drive.
- Enterprise profiles and deployment artifacts still require environment-
  specific validation and are not production-certified.
- macOS packaging and wider platform acceptance remain open work.
- GitHub Release creation, npm publication, Beta dist-tags, and built artifact
  publication are separate release operations. Source metadata alone does not
  prove that any of them occurred.

## Feedback

Please report Beta issues through
[GitHub Issues](https://github.com/Bazza1982/HASHI/issues) with the HASHI
version, operating environment, reproduction steps, and redacted logs. Keep
credentials, private prompts, conversations, and machine-specific paths out of
public reports.
