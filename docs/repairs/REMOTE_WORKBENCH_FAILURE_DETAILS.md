# Remote Workbench forwarding failures

Authenticated Remote gateway failures retain their existing status/code and add
a finite cause, phase, acceptance state and correlated public diagnostic reference.
Neither endpoint URLs, tokens, query/body data nor raw exception messages enter
this explanation. Existing signature verification, source evidence and local
Desktop identity preflight stay authoritative.

A refused connection or unresolved name proves that this attempt did not reach
the listener; existing alternate-host selection remains possible. A timeout,
reset or incomplete response after a modifying request is unconfirmed. The relay
stops after that attempt and never treats a missing acknowledgement as permission
to submit the mutation again. A read reports that no change was requested.

Invalid local target configuration is a local preparation failure, distinct from
invalid caller query/path input. Desktop's existing known-preflight/unknown-result
codes and retryability remain compatible. Existing cancellation shielding and
detached completion observation remain intact.

Workbench forwards only the validated public explanation through canonical
Session submission errors and retained message errors. Chat, Desktop, Call and
projection consumers use the same safe presentation contract. The typed unknown
response remains non-replayable.

Regression evidence: actual authenticated test-server routes exercise refused
connections, name resolution, timeouts, resets, invalid target configuration and
response loss after POST; Workbench tests traverse canonical submission, safe
error serialization, client error snapshots and the actual chat message notice.
These fixture checks do not imply runtime adoption or production deployment.
