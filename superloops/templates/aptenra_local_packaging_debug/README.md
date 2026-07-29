# Aptenra Local Packaging Fast Superloop

## Purpose

Find a working Aptenra Windows package through short, real feedback cycles:

```text
latest relevant failure
-> smallest direct repair
-> build MSI/CAB
-> visible /usecomputer install
-> launch installed Aptenra and Workbench
-> test basic functions
-> success: finish
-> failure: update Journal, uninstall, and repeat
```

The Failure Journal is diagnostic memory. It is not expanded into a historical
checklist and no PFJ entry can delay a new build or installation attempt.

## The four factual rules

1. Source or unpacked tests never count as an installed validation.
2. Every failed installation or installed launch updates the Failure Journal.
3. A failed candidate is uninstalled before the next round.
4. Work is limited to the current candidate; the user's environment and the
   original Debug Runtime remain untouched.

These rules are recorded after actual work. They do not authorize media and do
not sit in front of Build or Install.

## Fast round

Each round has four lightweight cards with no dependency or required-evidence
matrix:

1. Read only the latest relevant failure and apply the smallest repair.
2. Build a new candidate immediately from fixed source identities.
3. Install visibly, launch both installed shortcuts, and test basic functions.
4. Record the outcome; on failure update the Journal, uninstall, and continue.

Cheap checks directly related to a changed file are useful diagnostics, but
they are advisory. Do not rerun every historical check before Build or Install.

## Source stability

Every installable candidate still records exact product and packaging commits,
ProductCode, MSI hash, and immutable media directory. A dirty or unresolved
merge is repaired and committed as part of the active build task; it does not
become an external wait.

## Liveness

`scheduler_auto_advance=false` only means the scheduler itself does not edit
task cards. The one-minute idle nudge continuously asks Zelda to take a
concrete engineering action. It never asks for provider secrets and never
recreates a PFJ checklist.

The nudge ends only when:

- visible installation succeeds and both installed shortcuts launch; or
- round 30 is formally exhausted with evidence preserved.

## Actual result

A successful result requires:

```text
visible /usecomputer installation
-> installed Aptenra shortcut launch
-> installed Workbench shortcut launch
-> basic-function observation
```

An MSI return code, static inspection, unpacked run, process, listener, API, or
theory is not a substitute.
