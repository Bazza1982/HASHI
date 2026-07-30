# Aptenra Local Packaging Fast Superloop

## Purpose

Find a working Aptenra Windows package through short, real feedback cycles:

```text
latest relevant failure
-> smallest direct repair
-> build stable Setup/MSI/CAB
-> visible /usecomputer Setup install
-> launch installed native Aptenra and Workbench shortcuts
-> test five-agent/five-session local basics
-> ordered Stop, visible Repair, and post-Repair dual launch
-> ordered Stop, visible Uninstall, and zero-residue audit
-> success: finish
-> failure: update Journal, uninstall, and repeat
```

The Failure Journal is diagnostic memory. It is not expanded into a historical
checklist and no PFJ entry can delay a new build or installation attempt.

## The four factual rules

1. Source or unpacked tests never count as an installed validation.
2. Every failure updates the Failure Journal immediately.
3. A failed installed candidate is stopped and uninstalled before the next
   round; a never-installed build failure is recorded as no install to remove.
4. Work is limited to the current candidate; the user's environment and the
   original Debug Runtime remain untouched.

These rules are recorded after actual work. They do not authorize media and do
not sit in front of Build or Install.

## Fast round

Each round has four lightweight cards with no dependency or required-evidence
matrix:

1. Read only the latest relevant failure and apply the smallest repair.
2. Build a new stable Setup/MSI/CAB candidate immediately from fixed source
   identities.
3. Install visibly, launch both installed shortcuts, test five agents and
   sessions, then complete Repair and Uninstall.
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

- stable Setup Install, native installed dual launch, clean-user five-agent
  local basics, Repair, post-Repair dual launch, Uninstall, zero residue and
  environment restoration all succeed; or
- round 30 is formally exhausted with evidence preserved.

## Actual result

A successful result requires:

```text
visible /usecomputer installation
-> installed Aptenra shortcut launch
-> installed Workbench shortcut launch
-> five-agent/five-session local-basic observation
-> ordered Stop and visible Repair
-> post-Repair installed dual launch and local basics
-> ordered Stop and visible Uninstall
-> zero candidate residue and environment restoration
```

An MSI return code, static inspection, unpacked run, process, listener, API, or
theory is not a substitute.
