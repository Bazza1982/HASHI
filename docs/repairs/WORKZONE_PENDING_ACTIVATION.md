# Workzone pending activation

The overview previously used only saved_revision > active_revision. Adding a
Workzone and removing it again left the saved set empty at revision 2 and the
worker's active set empty at revision 0, so the UI displayed a permanent pending
indicator despite no remaining configuration change.

When the runtime or its metadata provides an authoritative active snapshot, the
read-only overview now compares slot IDs, normalized paths, enabled flags and
labels. Revision and timestamp differences alone do not imply a pending scope
change. When the active snapshot is unavailable, the conservative revision-based
indicator remains.

Both actual revisions remain unchanged. This projection neither adopts settings
nor alters runtime or admitted Run snapshots, file permissions, directories or
management authorization.

Validation: test_workzone_pending_projection covers empty reverts, same settings
with newer timestamps, real path/label/enabled/removal changes, metadata snapshots
and unknown active state. The actual management HTTP test adds and removes a
Workzone, then reads the overview twice and verifies persisted revision 2, active
revision 0, no pending indicator and preserved directory content. Existing Run
snapshot tests remain in the required CI recipe.
