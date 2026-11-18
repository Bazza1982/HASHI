# Managed directory listing continuation

The managed file-list helper enumerates the already-held Windows directory
handle. It returns a bounded page, an explicit coverage status, and a next
cursor when more permitted entries are available. The normal pattern,
include_hidden, and max_results options work for nonrecursive listings;
recursive discovery remains available through file search.

A cursor is authenticated by the current execution owner and bound to the
current Run, the resolved directory and filters. Each page checks the native
directory identity and a fingerprint of the permitted listing. If the directory
changes, the cursor is altered, the Run changes, or the execution owner is
restarted, the result contains no partial page and asks the caller to restart
the listing. A caller can change page size while continuing the same query.

Protected paths and Windows reparse entries are excluded; the cursor never
opens another path or relaxes the existing held-handle policy. Enumeration uses
bounded page memory, although it rescans the held directory for each page to
check revision and order. Very large directories may therefore take longer.

The cursor key is private to the active managed execution and is included only
in its protected helper plan, not in the returned listing. A lost execution
owner cannot resume an old cursor; the caller may begin again. Native temporary
Windows tests cover 501 entries, directory replacement, changed contents,
tampering, cross-Run use, a protected file, and an external symbolic link.
Installed helper qualification and full candidate acceptance are separate.


## Managed content search continuation

The fixed Windows file helper searches UTF-8 content through held, verified
file handles in bounded chunks. A page reads at most 16 MiB, then returns a
continuation at the current byte position; a single large line and a match
split across a chunk or page remain searchable. A result limit can also end a
page. The per-file 1 MiB skip is no longer used for this path.

The cursor is authenticated and bound to the current Run, query, resolved
roots and protected exclusions. The helper rechecks the previously traversed
directory prefix and the current file identity on continuation. If either has
changed, or the cursor is altered or used for another query or Run, it requests
a fresh search. A page never contains the private cursor key or file text in
its cursor. The scan observes the live filesystem and is not an atomic snapshot.

Binary, invalid UTF-8 and inaccessible entries are reported as skipped, with
bounded per-entry reasons; a search with skips does not claim complete coverage.
Protected descendants remain excluded.

Search can opt in to `options.follow_links`. It holds the link itself against
replacement, resolves the target component by component from an already
authorized held root, and holds each link encountered in a chain. Every hop and
its remaining path must stay within an authorized root and outside protected
paths. Returned paths use the searched link name. An unapproved or protected
target, an inaccessible link, or a cycle is reported as skipped. The cursor
checks the held link chain and opened target identities.

A link to an authorized target on another filesystem requires both
`follow_links` and `cross_filesystems`; the cross-filesystem option does not
expand authorization. Native Windows temporary fixtures exercised both options
across two actual local volumes, as well as a fixed helper entry, changed links,
cycles, protected destinations and unapproved targets. These observations do
not qualify an installed L2 helper or establish cross-platform acceptance.
