# Permission policy reads during a native Run barrier

Owner: PAO. Layer: replaceable Functions / Windows execution. Production and Protected Core are unchanged. Source integration approval is separate from L2 release qualification.

Intel's b199 once-contention case produced one file effect, then an unknown result and a temporary recovering response. The normal physical reconciliation recovered the original successful receipt; the operation was never rerun. A diagnostic repeat passed, so that repeat alone does not close the original failure.

A focused regression independently reproduced a lock-order defect in the installed composition. A preflight holds the permission authority transaction while reading current Workzones. Native dispatch/revalidation holds the canonical Run mutex before entering that authority transaction. Sharing the same SessionStore object for those policy reads can make the two paths wait on each other. The original native exception was not captured, so the exact first exception remains unproven; the lock inversion is reproducible on its own.

InstancePolicies now uses a separate SessionStore reader over the same canonical PAO database, instance and attachment configuration. SQLite WAL exposes committed Workzone state without waiting on the in-process Run mutex. No policy snapshot is cached or written to another profile. The authority transaction, real Run fence, policy revision, protected resources, scope ceilings, once consumption and native start rechecks remain in place. A committed Workzone change still invalidates an earlier policy revision.

The regression exercises actual authority evaluation and an actual PAO Run barrier in both L1 and L2. Its timeout is a deadlock watchdog, not a performance target. Red, green, integrated software and native qualification results are kept separately; a software pass is not a service deployment or external frontend pass. See [current WAC closeout](L2_WAC_CLOSEOUT_20261105.md).

## Authority writer coordination

Each PermissionAuthority now admits its own transactions in FIFO order before opening their SQLite connections. A status poll arriving later cannot overtake a waiting revocation. This prevents local threads from exhausting SQLite's external-writer busy timeout behind another local durable commit. The queue covers rollback and trusted-time persistence, supports same-thread nesting, and releases on every exit. Independent authorities retain SQLite serialization and service-generation fencing; no failed transaction is blindly retried.

Native Run barrier -> authority queue -> SQLite remains the dispatch order. Policy reads inside the authority still use the independent canonical PAO reader, never the Run barrier's in-process mutex. Both databases retain FULL rollback-journal commits and matching continuity tokens. Integration watchdogs are separate from permission expiry and native cancellation ordering. See [Windows CI diagnosis and red/green evidence](PR133_WINDOWS_CI_DIAGNOSIS_20261105.md).

## Service responsiveness and cancellation

The async private RPC dispatcher previously called synchronous ledger methods on the service event loop. A real held SQLite commit reproduced starvation of an independent service response. Synchronous methods now use a dedicated executor sized from the two existing endpoint connection capacities. Pipe I/O and disconnect monitoring keep their separate default executor; this adds no request quotas, throttling or arbitrary rate limits. Authenticated peer ContextVars propagate to the method thread. Async management and Run admission/settlement move their synchronous guarded database sections off the I/O loop as well.

Cancellation waits for each submitted synchronous operation to settle before the dispatch releases ownership. A disconnected client cannot turn a still-committing write into a confirmed rollback, and shutdown cannot report clean installation release while that dispatch remains active. The original identity-session guard -> Run fence -> authority transaction ordering is preserved; no await splits a guarded identity/ledger mutation.

A durable commit already in progress still delays transactions behind it, including revocation. FIFO removes overtaking; it does not preempt storage, bypass the identity guard, or establish a hard revocation latency bound during an unbounded disk stall. Native watchdog/physical cleanup acceptance remains separate from these service-loop and transaction-order tests.
