# Selected-instance CLI lifecycle

PAO owns selected-instance lifecycle commands. The CLI and platform process
inspection implement this contract in Functions; protected Core is unchanged.

## Selected identity

A registered selection supplies `HASHI_INSTANCE_ID`, `BRIDGE_HOME` and the
program root to both launch and onboarding children. Inherited identity from
another instance must not override that selection. The configured identity must
match the selected record before launch, and every readiness observation must
still match it. A mismatched observation fails without claiming success or
killing the observed process. The existing health probe also verifies the API's
actual instance identity and configured port.

## Graceful stop completion

The command retains its verified local API, idle-Agent, queue, background-job
and transfer preflight. Before sending authenticated shutdown it captures the
Core PID, reported Worker PIDs and their descendants, including process creation
identity. Shutdown acceptance is not completion. Success requires all captured
process identities to have exited, both API/Gateway TCP listeners to have gone,
and the instance's operating-system lock to have been released. A live replacement
PID also prevents success. Unknown ownership or inspection failure cannot report
success; timeout reports incomplete stop without forced termination. An already
stopped observation is subject to the same resource check. A persistent lock file
is permitted when no operating-system lock remains held.

## Qualification

Owner regressions use real child processes, real listening sockets and an
orphaned descendant, plus mismatched readiness observations. These checks do not
claim adoption by an installed launcher or shutdown of a running user instance.
