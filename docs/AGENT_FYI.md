# HASHI Agent FYI

<!-- hashi-fyi-catalogue:v2 -->

## Purpose and architecture
HASHI connects configured Agents, Engines and user channels while preserving conversation, context and execution evidence. This entry leads through domain guides to complete authoritative documents.

## Ownership
PCM owns Persona, Context and Memory sources and projection. PAO owns Agent lifecycle, Sessions, Runs, Workzones, routing and tool permission admission. HERV3 owns its Engine Session, Turns and Model Provider/tool loop. Frontend Connectors own authenticated transport, presentation and receipts. Compute supplies external services and does not own HASHI state.
These owners differ from deployment layers: Core, replaceable Functions, platform configuration and instance configuration. See [architecture](../ARCHITECTURE.md).

## Universal principles
- Choose one authoritative owner and derive views from it.
- Product changes belong in Functions or configuration. Protected Core needs explicit authorization for a major migration and independent review.
- Preserve identities, revisions, existing work and secrets. Unknown writes are not safe to replay.
- Verify behavior and the actual deployed owner. Documentation grants no permission.

## Domain catalogue
| Domain | Description | When to read | Guide |
|---|---|---|---|
| engineering | Source, configuration and evidence | Changing code, persistence, packaging or releases | [Engineering](agent-guide/engineering.md) |
| lifecycle | Agent and instance operations | Creating, stopping, deleting, rebooting, moving or cloning | [Lifecycle](agent-guide/lifecycle.md) |
| pcm | Authority and context projection | Changing PCM, FYI, HCC, memory or context handoff | [PCM](agent-guide/pcm.md) |
| messaging | Conversations and delivery | Changing Sessions, messages, questions or Remote protocols | [Messaging](agent-guide/messaging.md) |
| voice | Speech, Call, Phone and Presence | Changing voice, translation, playback or live interaction | [Voice](agent-guide/voice.md) |
| compute | Models and external services | Selecting models, translation, TTS, transcription, vision or fallback | [Compute](agent-guide/compute.md) |
| permissions | Authorization and privacy | Investigating access, governance or uncertain outcomes | [Permissions](agent-guide/permissions.md) |
| files-and-media | Scoped files and media | Changing attachments, extraction or file discovery | [Files and media](agent-guide/files-and-media.md) |

## Find and read
Read the relevant guide, then its owning contract. With read-only `documentation_read`, pass the link and `from_path` to resolve it from the source document. Use an outline to select a section. Follow explicit continuation before treating its conditions and exceptions as complete.
Use authorized repository readers for source/tests or when the scoped reader is unavailable. Report inaccessible references instead of inventing content or authority.
General chat needs no detailed guides. Do not expand every link or reload the catalogue every turn. Retain path/section/revision references across compaction and backend handoff so relevant material can be reread.

## Evidence and authority
Design, source implementation, packaged bytes, runtime adoption and live acceptance are separate evidence. Catalogue/common-guidance revision does not certify linked documents; retrieved sources have their own revisions.
Current user instructions and the existing authority hierarchy prevail. Report loading failures without breaking ordinary conversation. Historical records describe their stated scope and version; they grant no current authority or approval.
