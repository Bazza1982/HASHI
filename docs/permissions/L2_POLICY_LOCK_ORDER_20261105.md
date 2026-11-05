# Permission policy reads during a native Run barrier

Owner: PAO. Layer: replaceable Functions / Windows execution. Production and Protected Core are unchanged. Source integration approval is separate from L2 release qualification.

Intel's b199 once-contention case produced one file effect, then an unknown result and a temporary recovering response. The normal physical reconciliation recovered the original successful receipt; the operation was never rerun. A diagnostic repeat passed, so that repeat alone does not close the original failure.

A focused regression independently reproduced a lock-order defect in the installed composition. A preflight holds the permission authority transaction while reading current Workzones. Native dispatch/revalidation holds the canonical Run mutex before entering that authority transaction. Sharing the same SessionStore object for those policy reads can make the two paths wait on each other. The original native exception was not captured, so the exact first exception remains unproven; the lock inversion is reproducible on its own.

InstancePolicies now uses a separate SessionStore reader over the same canonical PAO database, instance and attachment configuration. SQLite WAL exposes committed Workzone state without waiting on the in-process Run mutex. No policy snapshot is cached or written to another profile. The authority transaction, real Run fence, policy revision, protected resources, scope ceilings, once consumption and native start rechecks remain in place. A committed Workzone change still invalidates an earlier policy revision.

The regression exercises actual authority evaluation and an actual PAO Run barrier in both L1 and L2. Its timeout is a deadlock watchdog, not a performance target. Red, green, integrated software and native qualification results are kept separately; a software pass is not a service deployment or external frontend pass. See [current WAC closeout](L2_WAC_CLOSEOUT_20261105.md).
