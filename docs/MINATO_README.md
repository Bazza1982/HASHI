# Minato — AI-Assisted Project Management

Architecture classification: lightweight PAO project/phase vocabulary and
integration layer under [HASHI System Architecture](../ARCHITECTURE.md).

## Overview

Minato is HASHI's project-management vocabulary and integration layer. Projects
contain Shimanto phases, phases can use Nagare workflows, and workflows produce
artefacts. HASHI agents can carry this context across local and cross-instance
work without depending on a particular user interface.

```text
Project (Minato)
  └─ Phase (Shimanto)
       └─ Workflow (Nagare)
            └─ Artefacts
```

Minato is deliberately lightweight: it provides shared project context,
auditable agent activity, and a common vocabulary rather than imposing a rigid
task-tracking process. It is not currently a full orchestration Engine and is
not equivalent to Nagare or Superloop.

## HASHI Responsibilities

HASHI owns the execution-side pieces:

- the Nagare workflow engine and HASHI adapter
- agent queues, transcripts, scheduling, and HChat delivery
- parsing the `MINATO CONTEXT` envelope in agent traffic
- per-agent project chat logging through `ProjectChatLogger`
- Backend API and authenticated Hashi Remote endpoints used by external clients

The context envelope is:

```text
[MINATO CONTEXT]
minato active project: My Project Name
shimanto phases: Phase 1, Phase 2
nagare workflows: workflow-name-1, workflow-name-2
scope: brief description of task scope
[END CONTEXT]
```

The receiving runtime extracts the metadata before normal agent processing.
HChat preserves the envelope when work crosses HASHI instances.

## Retired Workbench Boundary

Workbench has retired. Its project UI, Node server, local UI data, Minato MCP
presentation layer, launchers, and UI tests are no longer part of HASHI.
Minato's HASHI-side context parsing, project chat logging, Nagare execution,
Backend API, and HChat behavior remain supported independently of that retired
interface.

## Nagare

Nagare workflows are YAML-defined directed acyclic graphs. The HASHI side
provides dependency ordering, parallel execution where allowed, pre-flight
questions, named artefact flow, atomic state persistence, resume support, and
multi-agent dispatch. See [NAGARE_FLOW_SYSTEM.md](NAGARE_FLOW_SYSTEM.md) for
the complete technical reference.

## Artefacts and External Systems

HASHI handles filesystem and Nagare execution artefacts. Independent clients
may add richer project artefacts or connect systems such as KASUMI and Obsidian
without changing HASHI's core boundary. Their storage, synchronization, and UI
contracts belong to those systems.

## Source Map

```text
orchestrator/project_chat_logger.py  per-agent project chat log
orchestrator/workbench_api.py        local Backend API (compatibility name)
remote/api/server.py                 authenticated Hashi Remote API
nagare/                              standalone workflow engine
flow/                                legacy flow compatibility and HASHI adapter
docs/NAGARE_FLOW_SYSTEM.md           Nagare reference
```

The retired Workbench is intentionally not represented as a subdirectory of
HASHI.
