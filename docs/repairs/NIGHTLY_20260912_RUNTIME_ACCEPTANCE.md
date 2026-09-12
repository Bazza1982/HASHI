# Nightly repair: H1/H3 runtime acceptance

Date: 2026-09-12 AEST

Branch: `repair/nightly-batch-20260911`

Candidate head: `45582279546874e46124a2b7f32858cc59daacd5`

Scope: the 27-item batch frozen on 2026-09-11 at 12:02 AEST. Later items
HN-20260911-006 and HN-20260911-007 are not part of this acceptance.

## Deployment boundary

- HASHI1 and native Windows HASHI3 use the candidate Functions generation.
  Their Core process identities stayed unchanged during Functions adoption.
- The HASHI1 and HASHI3 Remote sidecars were restarted once so the additive
  `tui_proxy_v1` operations were advertised. This was not a Core restart.
- The protected-Core guard passed against
  `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`.
- HASHI2 was not replaced or restarted by this acceptance. Its source/adoption
  status remains a separate gate because that live Agent is the executing
  session and its repository contains unrelated local work.

## Focused platform evidence

- Native Windows configuration/TUI selection: 290 passed, 8 skipped, with two
  third-party warnings.
- Native Windows selected feature suites: Provider/HER and metadata 15 passed;
  attachment/local speech 11 passed; PCM routing 16 passed.
- HASHI1 declarative configuration owners and direct consumers: 298 passed,
  with one third-party warning.
- GitHub Architecture Boundaries and Windows Runtime Contract jobs passed on
  this candidate. No global local suite was run.

## Live Provider and metadata evidence

- A bounded real OpenRouter HER call completed with the expected exact reply.
- Direct DeepSeek calls completed on HASHI1 and native HASHI3 with Provider
  thinking usage present.
- A controlled HASHI1 DeepSeek malformed-tool canary started from a real
  thinking/tool response, rejected the malformed batch before side effects,
  preserved `reasoning_content` on the repair turn, executed the corrected
  synthetic tool exactly once, and returned the expected exact reply. The
  incident used one of the three shared local-recovery positions and wrote one
  private 0600 forensic record whose parser state was `invalid_json`.
- A real public OpenRouter catalogue lookup resolved `codex-cli` model
  `openai/gpt-5.4` to one exact canonical identity. Price and capability facts
  shared that identity, cached rereads were stable, and provider billing stayed
  distinct from the OpenRouter reference estimate.

## Live routing and Frontend Connector evidence

- An H1-launched TUI switched atomically to HASHI3. Health identity, Agent
  directory, per-instance capability state, peer-owned log tail, and return to
  the launch instance were verified without mixing generations.
- A real HASHI3 chat completed even though that instance does not advertise the
  optional Persistent Session API. Chat success was not rendered as a Run
  status failure.
- One marked HASHI1 TUI reply was delivered through the planned Telegram mirror
  and has a durable Connector delivery outcome. No channel identifier or token
  is copied into this document.
- One marked HChat round trip between HASHI1 and HASHI3 completed with a single
  terminal response and no acknowledgement loop.
- A real TUI `/instance HASHI3` then `/to all` submission froze exactly the one
  active HASHI3 target. HASHI3 returned `H3_SCOPE_BROADCAST_OK`; HASHI1 had zero
  matching submissions and Telegram had zero delivery events for that Run.
- A 37-byte attachment was snapshotted on HASHI1 and sent as authenticated
  bytes to HASHI3. It waited behind pre-existing Agent work, then completed
  naturally with `H3_ATTACHMENT_ROUTE_OK`; it was never resubmitted.
- Direct HASHI1 and proxied HASHI3 voice-state reads returned the four existing
  semantic profiles. Local speech generation/playback lifecycle is covered by
  simulated player tests; no sound was played and no Telegram send was made.
- WhatsApp route ownership, frozen-route checks and receipt mapping passed the
  focused simulation suite. The account is logged out, so actual WhatsApp
  delivery remains user acceptance and is not claimed here.

## Instance-local Memory/Wiki scanner

The ignored instance-local scanner matches the reviewed repair digest. A
read-only production preflight built plans for all configured local instances
without changing the consolidated database size or modification time. Earlier
isolated scanner atomicity tests passed; the existing duplicate inventory was
reported read-only and no historical row was deleted.

## Remaining adoption gates

Source implementation and HASHI1/HASHI3 runtime acceptance are complete for the
frozen batch. HASHI2 source reconciliation and Functions adoption, physical
voice listening, and real WhatsApp delivery are deliberately distinct and must
not be inferred from this document.
