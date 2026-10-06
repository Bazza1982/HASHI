# Nightly restart, frontend and Session execution repairs

## Scope and authority

The user authorized implementation and testing in HASHI3 and the test Workbench
on 2026-10-06, including their operational restarts. HASHI4 adoption is separate.
Core is immutable. PAO owns lifecycle and Session scheduling in Functions; the
Frontend Connector and external Workbench consume authoritative projections.

## Qualification work

The old qualification path parsed the same source closure for initial discovery,
the independent probe, final discovery and generation verification. Three local
baseline qualifications took 64.829, 69.026 and 61.470 seconds. These are
qualification times, not total reboot or disconnection durations.

Build each source AST once per manifest and use it for compilation, lazy-import
discovery and ordering. Discard the AST after deriving dependencies, avoiding a
process-wide cache. If the independent interpreter accepted exactly the initial
module set and digest, final verification compares every source byte and the
freshly discovered asset set. An expanded module set still rebuilds the graph.
The independent import/contract probe, Git membership/commit gates, runtime
fingerprints, immutable artifact verification and READY/rollback boundaries stay.

Focused source/asset mutation, lazy imports, compilation, asset membership and
commit-gate checks passed: 16 passed, 3 deferred source-qualified checks. Live
adoption and measured improvement are pending. No speed claim follows from
eliminating work alone.

## Delivery requirements

Reboot start messages must be readable from PAO's durable Session projection
before cutover, even while Worker command routes are fenced. All authenticated
Workbench clients discover the same owner-scoped lifecycle regardless of origin.
Visible chat presentation and banner presentation are distinct evidence.
Success requires a terminal authoritative receipt and restored connectivity,
then dismisses after four visible seconds; failure and unknown persist.
Theme colors, typography, spacing and motion use existing theme semantics.

Remote or unconfirmed shared filesystems default to uploading actual bytes.
Destination connection, instance, Agent, Session and generation remain frozen
through capability checks, attachment staging/commit and one Run admission.

Desktop startup configuration belongs to the installation state, respects an
explicit opt-out and binds an authenticated physical desktop independently of
the selected chat. Initialization grants no control lease or background capture.

Same-Agent concurrency means overlapping actual execution in two persistent
Sessions using the same engine/model, with FIFO inside each Session. Source,
focused validation, running adoption and real frontend evidence are separate.
