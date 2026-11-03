# Existing Session reads and HTTP responsiveness

An existing default Session was resolved by create_session, which began an
IMMEDIATE write transaction before checking whether a row already existed.
Every Agent roster refresh could therefore queue behind unrelated SQLite writes.
ensure_default_session now performs a pure independent WAL read first. Missing
rows still use the original transactional creation and race recheck. The first
read of existing primary bindings likewise does not acquire the process writer
lock; explicit establishment retains its transaction and competing-binding check.
Owner, instance, Agent and default filters remain unchanged.

Agent roster metadata and individual overview disk/worker reads now run through
asyncio.to_thread after owner authorization. The async HTTP loop remains available
for unrelated reads. This changes Functions-side read scheduling, not Core,
runtime authority, configuration adoption, Session binding or Run execution.

Regression tests use a temporary database with a real external writer and a local
writer waiting for SQLite. Existing default/primary reads must finish before
either writer is released. Concurrent first use still creates exactly one default
per owner/Agent. Actual aiohttp requests verify that a deliberately waiting
projection does not stall an independent HTTP request. These controlled contention
checks are not measurements of production load.

Before the Functions changes, three read-contention checks and both independent
HTTP checks failed. The Session repository and primary binding regression suite
passed 86 tests; related overview, management, owner and architecture checks
passed 28 tests. Final whole-candidate and live NP qualification are separate.
