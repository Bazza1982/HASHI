# Presence desktop context

Owner: Frontend Connector projection, backed by the existing PAO route and
SessionStore primary-conversation owner. Engineering layer: replaceable Functions.
This feature changes no Core contract and grants no additional control authority.

Presence's instance choice stays local to Presence. HASHI retains its configured
Presence Agent and the authenticated owner's primary Conversation. The desktop
entry reads those existing owners instead of using a Worker boot Session or
whichever historical conversation a Workbench currently displays.

Two GET adapters expose the same narrow projection:
- Local Presence: /api/agentpresence/desktop-context?instance_id=...
  uses the existing loopback X-AP-Token admission.
- Workbench: /api/v1/agentpresence/desktop-context?instance_id=...
  uses existing authenticated Remote admission and the ordinary owner contract.
  Workbench does not receive or tunnel the local AP credential.

The version-1 result contains ok, desktop_context_version, instance_id, agent_id,
route_revision, session_id and context_generation. The instance must match.
The current personal-profile AP owner must match; unsupported governed profiles
remain unavailable. Missing Agent or a route that changes during resolution
fails closed. A stale route returns 409; it is not silently retried or merged.
Resolving a primary uses SessionStore's existing semantics and runs off the API
event loop, as do route reads. No new Session store, port registry or state writer
is introduced.

Consumers pin this complete target to an expiring local desktop intent. Workbench
revalidates it over Remote, uses its existing conversation-selection API, and
calls the existing independent Phone or direct Call launcher once. Its launchers
and PAO still own admission, microphone/media, permissions and durable call state.
An opened window or a delivery acknowledgement never proves a connected call.

Focused qualification: tests/test_agentpresence_desktop_context.py plus the
existing AP controls and primary-session metadata tests. A native lock fixture
keeps primary resolution pending while an unrelated API response succeeds.
An in-memory blocking mutation fails that test; the corrected thread adapter
passes. The endpoint initially returned 404 before implementation.

FYI: implementation and offline qualification do not prove a running instance
has adopted these Functions, an actual microphone call passed, or production
was deployed. Adoption, actual Phone/Call operation and cold/warm/busy latency
are recorded separately in the integration task.
