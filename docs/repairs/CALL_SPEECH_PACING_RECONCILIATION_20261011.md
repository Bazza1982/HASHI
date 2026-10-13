# Call speech pacing reconciliation — 2026-10-11

Status: implementation, focused checks, and HASHI3 physical acceptance pass on
`debug-call`; exact candidate identity and any HASHI4 load remain separate
receipts.

Owner: Frontend Connector Call Function

Layer: replaceable Layer 2 Function; Protected Core unchanged

## Observed failure

The physical HASHI3 trace showed that public commentary was reaching Call and
that Workbench's one-segment lookahead was active. Long gaps caused by Agent
tool work were legitimate because no next answer text existed. Three other gaps
were local defects: the next public sentence was already in the Call snapshot,
but playback paused for about 2.34, 2.75, and 1.55 seconds while a separate TTS
request completed.

A follow-up physical receipt exposed the remaining cross-language defect. The
59-character English reply `Fashion is on mark. Camera is stable. All test is
complete.` was split after its first seven spoken words. Both chunks were known
before playback, but synthesis of the 21-character tail took about 2.31 seconds
and produced a measured 2.192-second local playback gap. Character count had
overestimated the playable duration of the first English chunk.

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
- The coalescing threshold estimates playable duration: compact-script
  characters, space-delimited spoken words, and punctuation pauses contribute
  separately. It does not treat raw character count as language-neutral time.
- A short incomplete or terminal tail is never discarded; terminal rendering
  flushes it.
- Every chunk emitted from a preview remains an exact prefix of the chunks
  derived from the final answer.
- Commentary remains public speech; private reasoning remains excluded.

The minimum natural chunk is an internal presentation bound, not a response
length rule. It does not shorten or rewrite the Agent answer.

## Inter-chunk wait correction

A later physical trace found one remaining 5.284-second silence. The next final
chunk was already public, but it arrived late in the preceding commentary audio
and its selected-voice TTS took about 4.4 seconds. Browser polling and concurrent
camera/snapshot traffic extended the wait. The one-segment lookahead was loaded
and worked whenever enough audio lead existed; widening lookahead or weakening
the 1.5-second gate would therefore hide the failure rather than fix it.

Workbench now prefetches the three already-approved localized filler assets for
the active turn. When a known next speech asset is still unavailable after a
bounded 250 ms grace period, those prepared clips may play in order to bridge
the silent interval. The real answer interrupts the active clip as soon as it
becomes ready. This bridge does not enter PAO rows, Call captions, Session
history, or the answer text, and it does not permit more than one answer segment
of lookahead.

## Regression gates

The old assertions that required one synthesis item per sentence were replaced
with observable behavior checks for short-sentence coalescing, early streaming
of a substantial sentence, terminal-tail delivery, preview/final prefix
stability, commentary delivery, stage-direction cleaning, and bounded
pathological output.

The focused red reproduction failed the exact English physical reply against
the previous implementation, in addition to the earlier short-sentence red
cases. After the duration-aware correction, the HASHI3 Frontend Call suite
passes 154/154 and the Workbench focused Call suite passes 93/93. The Protected
Core check passes. The physical receipt must
measure only playback gaps whose next segment was already present before the
current audio ended; it must exclude time in which the Agent was still working
and no next public segment existed. Workbench also covers a slow known next
sentence with deterministic red/green tests: prepared filler playback begins,
continues to the next prepared clip when needed, creates no transcript row, and
yields immediately to the ready FIFO answer. The latest physical HASHI3 run
passes all 18 media, presentation, speech, commentary, control, and cleanup
gates.

## Release boundary

HASHI3 adoption, physical microphone/camera/speaker acceptance, exact source
identity, and any later HASHI4 load are recorded outside this source document.
An older live result cannot be used for a newer source tree.
