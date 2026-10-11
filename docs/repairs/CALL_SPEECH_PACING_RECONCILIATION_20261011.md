# Call speech pacing reconciliation — 2026-10-11

Status: implementation and offline checks pass on `debug-call`; running
adoption and physical acceptance are separate receipts.

Owner: Frontend Connector Call Function

Layer: replaceable Layer 2 Function; Protected Core unchanged

## Observed failure

The physical HASHI3 trace showed that public commentary was reaching Call and
that Workbench's one-segment lookahead was active. Long gaps caused by Agent
tool work were legitimate because no next answer text existed. Three other gaps
were local defects: the next public sentence was already in the Call snapshot,
but playback paused for about 2.34, 2.75, and 1.55 seconds while a separate TTS
request completed.

## Regression lineage

- `8defd5e9` repaired speech gaps by keeping a short phrase with following text
  so the currently playing audio could cover synthesis of the next chunk.
- `649b1ced` correctly added complete-sentence preview streaming, but replaced
  the repaired segmentation with one TTS item for every complete sentence.
- The result retained streaming correctness but lost the earlier pacing
  guarantee. This was a semantic overwrite, so merging an old branch wholesale
  would also restore obsolete behavior.

## Current rule

- A sufficiently substantial complete sentence is stable and may stream before
  the Agent run is terminal.
- Adjacent short complete sentences coalesce into one bounded natural speech
  chunk.
- A short incomplete or terminal tail is never discarded; terminal rendering
  flushes it.
- Every chunk emitted from a preview remains an exact prefix of the chunks
  derived from the final answer.
- Commentary remains public speech; private reasoning remains excluded.

The minimum natural chunk is an internal presentation bound, not a response
length rule. It does not shorten or rewrite the Agent answer.

## Regression gates

The old assertions that required one synthesis item per sentence were replaced
with observable behavior checks for short-sentence coalescing, early streaming
of a substantial sentence, terminal-tail delivery, preview/final prefix
stability, commentary delivery, stage-direction cleaning, and bounded
pathological output.

The focused red reproduction failed both new short-sentence cases against the
previous implementation. After the change, the complete Frontend Call suite
passes 150/150 and the Protected Core check passes. The physical receipt must
measure only playback gaps whose next segment was already present before the
current audio ended; it must exclude time in which the Agent was still working
and no next public segment existed.

## Release boundary

HASHI3 adoption, physical microphone/camera/speaker acceptance, exact source
identity, and any later HASHI4 load are recorded outside this source document.
An older live result cannot be used for a newer source tree.
