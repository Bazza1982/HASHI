# HASHI4 startup qualification timing, 2026-10-04

Scope: HASHI4 local checkout, PAO Functions startup diagnostics. This is offline
evidence, not a live adoption or a measured production restart.

The 09:59:52–10:03:24 production startup took about 3m32s. Existing logs showed
about 1m55s between Core process creation and shared Function process creation,
but had no qualification subphase durations. The Functions release now carries
wall-clock start/end times and monotonic durations through the unchanged Core
bootstrap envelope, then writes them to the shared bridge startup log. No
protected Core file changed.

An isolated qualification of the committed checkout in a temporary instance
home completed in 84.1s. Its top-level phase timings were:

| Phase | Duration |
| --- | ---: |
| Initial source manifest | 17.6s |
| Isolated candidate probe | 22.4s |
| Final source manifest | 17.4s |
| Final generation verification | 17.8s |
| Artifact materialization | 8.5s |

Inside the candidate probe, its own manifest took 17.1s and imports/contract
validation took 4.3s. These inner values are part of the 22.4s probe total and
must not be added to it again. Repeated full source-manifest work dominated
this offline run. It is a strong candidate for the observed cold-start delay,
but the historical production boot cannot be assigned the same phase durations
retroactively.

The follow-up is to read the next live startup record before changing the
qualification algorithm. Any performance change must preserve exact source and
asset verification, Git commit checks, and the isolated probe boundary.
