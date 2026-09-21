# HASHI

**Your agents. Your context. Your choice of engine.**

HASHI is an open-source, local-first platform for persistent AI agents.
Bring identity, memory, tools, conversations, and workflows together on
infrastructure you control. Work through a terminal, Telegram, WhatsApp, or
an authenticated client API.

Use HASHI's native **HER v2** engine or connect engines such as Claude Code,
Codex CLI, Gemini CLI, and Grok CLI. HER v2 can route work across supported
model providers while keeping its own durable session.

[Get started](docs/INSTALL.md) · [User guide](docs/USER_GUIDE.md) ·
[Integrations](docs/INTEGRATIONS.md) ·
[Documentation](https://github.com/Bazza1982/HASHI/blob/main/docs/README.md) ·
[Changelog](https://github.com/Bazza1982/HASHI/blob/main/CHANGELOG.md)

## Why HASHI

- **Agents with continuity.** Keep each agent's persona, context, memory, and
  project access together across tasks. Inspect work and recovery records.
- **A choice of engines and models.** Use CLI engines with their own local
  authentication, or configure model providers inside HER v2.
- **Work that extends beyond a reply.** Give agents tools, schedule recurring
  work, track background processes, and coordinate multi-agent workflows.
- **Several ways to stay connected.** Use the built-in terminal UI and messaging
  connectors, or build a client on HASHI's Backend and Persistent Session APIs.
- **Control over deployment and credentials.** Self-host the runtime and keep
  local configuration under your control. External providers and messaging
  services still receive the data needed for the requests you send them.

## What You Can Do

| Task | HASHI capabilities |
|---|---|
| Research and writing | Project Workzones, memory, skills, file and browser tools |
| Coding and local automation | CLI engines or HER v2, scoped tools, background jobs, logs |
| Repeatable multi-step work | Nagare workflows, scheduled jobs, review and recovery records |
| Long-running coordination | Superloop taskboards, explicit waits, HChat across trusted instances |
| Client integrations | Authenticated Backend APIs and an optional model API Gateway |

Capabilities depend on the selected engine, permissions, installed extras,
and connected services. See the [integration guide](docs/INTEGRATIONS.md).

## Project Status

Current source metadata is **v4.0.0-beta.1** (Python: **4.0.0b1**). This is a
Beta release for small-scale public testing, not a general-production release.

The repository, npm registry, and GitHub Releases can be at different points.
A source version does not prove that a matching npm package or installer has
been published. Check the [release and distribution guide](docs/RELEASES.md)
before downloading.

| Area | Current boundary |
|---|---|
| Personal/local use | Primary development path; open for small-scale Beta testing |
| HER v2 | Native Python engine with Direct, Strategic, and Planned execution |
| Team/enterprise governance | Alpha profiles, policy, approvals, audit, and deployment artifacts; production deployment validation remains pending |
| Frontends | Built-in TUI, Telegram, WhatsApp, and client APIs; the independently maintained Workbench v2 is an external client, not bundled with HASHI |
| Platform evidence | Windows-native and WSL/Linux deployment templates are included; macOS releases still need platform acceptance |
| Shared demo | A restricted Demo Connector supports simple operator-hosted online demos; public hosting and capacity validation remain operator gates |
| Portable Windows | A verified USB installation bundle can carry HASHI to a new Windows PC and install it locally with its private runtime |
| Device/browser control | Requires the relevant worker, browser, permissions, and per-platform validation |

Detailed candidate scope is in the
[release notes](https://github.com/Bazza1982/HASHI/blob/main/docs/RELEASE_NOTES_v4.0.0-beta.1.md).
Historical milestones are recorded in
[CHANGELOG.md](https://github.com/Bazza1982/HASHI/blob/main/CHANGELOG.md).

## Installation

Start with the [installation guide](docs/INSTALL.md), which covers source
installs, npm, named instances, and portable distributions.

The npm package is **hashi-bridge** and the command is **hashi**. The package
named **hashi** belongs to another project. Inspect published versions first:

~~~bash
npm view hashi-bridge dist-tags --json
npm view hashi-bridge versions --json
~~~

Choose an actually published version using the guide. An unqualified npm
install uses its registry's latest tag, which may still point to a legacy
release until a matching Beta package is actually published.

Source installs require the approved **CPython 3.12.13** runtime and locked
dependencies. npm additionally requires Node.js and npm; it prepares a
versioned Python environment but does not bundle a base Python interpreter.
CLI engines, API credentials, and optional tool dependencies are configured
separately. Telegram is optional.

Portable distributions have their own bundled capability profiles. The
[Portable Windows builder](https://github.com/Bazza1982/HASHI/blob/main/packaging/portable_windows/README.md)
creates verified USB installation media for quickly installing HASHI and its
private runtime on another Windows PC. HASHI runs from the verified local
installation; an npm tarball alone is not a self-contained offline installer.

## Using HASHI

After setup, choose an agent and give it a task. Add the relevant project
Workzone, then use the live command help for the options available on your
instance.

- [Everyday usage](docs/USER_GUIDE.md): sessions, task controls, memory, skills,
  jobs, voice, and TUI operation.
- [Configuration](docs/CONFIGURATION.md): instance data, credentials, model
  opt-ins, permissions, and local extensions.
- [Integrations](docs/INTEGRATIONS.md): engines, messaging, APIs, browser/device
  tools, and Remote.
- [Troubleshooting](docs/TROUBLESHOOTING.md): installation, connection, logs,
  and recovery.

## Architecture

HASHI has four functional owners:

| Owner | Responsibility |
|---|---|
| PCM — Persona, Context, Memory | Assemble and project an agent's context and memory |
| PAO — Provider-Agnostic Orchestration | Agents, conversations, runs, engine selection, tools, jobs, and coordination |
| HER v2 — HASHI Engine Runtime | Durable engine sessions, execution stages, and model-provider routing |
| Frontend Connectors | User-facing channels and client protocols |

These owners are separate from the engineering layers: Core, Functions,
Platform Configuration, and Instance Configuration. Normal product behavior
lives in replaceable Functions and configuration. See
[ARCHITECTURE.md](https://github.com/Bazza1982/HASHI/blob/main/ARCHITECTURE.md)
for authority and session boundaries.

## Privacy and Deployment

Credentials and authoritative HASHI state are stored locally by default.
That does not make remote inference or messaging offline: review each
provider's data handling and your agent's tool permissions.

The authenticated Backend API and the optional OpenAI-compatible API Gateway
are different services. The Gateway does **not** enforce caller authentication;
keep it private. Its model/tool compatibility is described in the
[API guide](https://github.com/Bazza1982/HASHI/blob/main/docs/API_GUIDE.md).

Back up instance data before upgrades. Tools can modify real files and invoke
external services. Enterprise features remain Alpha and do not imply a
security certification or general production readiness.

## Name and Credits

HASHI (橋) means **bridge** in Japanese.

Conceived and directed by [Barry Li](https://barryli.phd), HASHI grew through
AI-assisted development and cross-model review under human architectural and
operational direction.

[OpenClaw](https://github.com/openclaw/openclaw), Peter Steinberger, and its
contributors provided important early inspiration. HASHI now has its own
orchestration and native HER v2 runtime; the earlier Claw-derived HER v1 is
retired. Historical bridge-u-f names remain in compatibility entry points.

## Contributing and Support

See [CONTRIBUTING.md](https://github.com/Bazza1982/HASHI/blob/main/CONTRIBUTING.md)
and the [roadmap](https://github.com/Bazza1982/HASHI/blob/main/docs/ROADMAP.md).
[GitHub Issues](https://github.com/Bazza1982/HASHI/issues) is the current public
support and feedback channel. Include versions, reproduction steps, and
redacted logs with bug reports.

HASHI is released under the [MIT License](LICENSE). See
[third-party notices](THIRD_PARTY_NOTICES.md) for bundled components.
