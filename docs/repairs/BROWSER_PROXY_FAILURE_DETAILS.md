# Browser proxy failure details

Unexpected proxy failures now carry a finite cause, phase, unknown-action state
and public diagnostic reference. The isolated ToolRegistry reads the bounded
public failure DTO and presents its explanation; it no longer collapses every
HTTP error into the HTTPError class name. Raw exception text, URLs, tokens,
arguments and page content never enter the failure explanation or correlation log.

Input validation and request-scoped bearer checks remain ahead of dispatch.
Known validation messages and timeouts remain specific. Once the owner action
has been dispatched, later ValueError/serialization failures are response failures,
not proof of invalid caller input. Timeouts cancel only the local future and do
not claim that the browser action was cancelled. Missing action acknowledgements
are unconfirmed and must not be replayed automatically.

Fixtures use the actual loopback proxy and isolated ToolRegistry, with synthetic
owner actions only. They check safe cause propagation, matching diagnostic logs,
one dispatch, unchanged pre-dispatch validation, timeout uncertainty and rejection
of malformed error bodies. No real browser action is issued by these fixtures.
