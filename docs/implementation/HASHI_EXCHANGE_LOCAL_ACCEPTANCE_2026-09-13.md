# HASHI Exchange local acceptance

Date: 2026-09-13 (Australia/Sydney)

HASHI base: `19e330f985aaf6d5523fd82a3bcc8a4256b536c0`

Exchange revision: `9f6d7933d77483062595f6757fc9709540344db0`

Implementation branch: `feature/exchange-integration-20260913`

## Scope and approval

The user approved local stages A and B1-B9 after confirming that Exchange is an
independent repository and service. DNS, Cloudflare, Tunnel, Hub integration,
public operation, live HASHI restart/adoption, push, and pull-request creation
were explicitly left outside this pass.

Development used an isolated linked worktree. The existing HASHI2 source tree
and the HASHI4 running source tree were not edited or restarted. No credential
or message body is recorded in this document.

## A: independent Exchange evidence

- Native Windows CPython 3.12.13 and `websockets` 16.0.
- Exchange repository tests: 84 passed; two TLS cases skipped because OpenSSL
  was unavailable. Certificate verification was not disabled.
- Demo: bidirectional delivery true, receiver-reopen dedup true,
  cross-user denial true, HASHI runtime connected false, Hub connected false.
  The false integration values were the honest pre-B boundary.
- A local authority was initialised without overwrite. Its directory ACL was
  reduced to the current user and SYSTEM, and the ignored lab state was not
  committed.
- The listener was verified on loopback port 8787 only.
- Fault checks verified a new epoch after server restart, active credential
  revocation closing the connection with code 1008, and refusal of unreadable,
  missing, overwrite, and invalid-limit authority inputs.
- No Exchange service was left running, and the temporary home credential was
  revoked after the exercise.

## B1-B9 acceptance matrix

| Stage | Implemented evidence |
|---|---|
| B1 | Recorded the real HChat send/receive/reply, Remote 2.0 ACK/correlation, private-proof, Workbench, and SessionStore/PAO admission chains in `BASELINE.md`. |
| B2 | Added strict typed local, group, and public address parsing. Dotted failures cannot fall back to LAN; full Exchange identity tuples remain intact. |
| B3 | Added one optional outbound-only Exchange transport to Remote assembly with v1 capability negotiation, explicit publication, negotiated limits, redirect refusal, and no generic proxy surface. |
| B4 | Added default-disabled instance schema, WSS/loopback-WS validation, secret references, local-secret resolution, explicit active-Agent publication, and section-only revision CAS. |
| B5 | Added HMAC-sealed connector evidence, recipient tuple/epoch binding, a non-public verified-principal constructor, and a dedicated accept/schedule API. Ordinary JSON and legacy text cannot self-assert Exchange identity. |
| B6 | Refused all non-empty private/resource proof fields at HChat, local API, outbound codec, and inbound codec boundaries. |
| B7 | Put the inbox ledger in the authoritative SessionStore SQLite file; commit precedes ACK, scheduling follows ACK, retry is idempotent, and receiver restart reconstructs the original queued Run. |
| B8 | Preserved exact reply tuple/conversation/request correlation, suppressed reply loops, retained correlation beyond delivery expiry, used exact-frame retry, and verified Exchange and receiver-process restart recovery. |
| B9 | Preserved local/LAN/Remote 2.0 routes, isolated public routing, expanded groups per recipient, enforced each recipient's policy, and verified live unpublish plus PAO's immediate acceptance block. |

## Cross-repository observable acceptance

The integration test starts a real `hashi_exchange` `LabServer`, two separately
configured HASHI roots, two HASHI Workbench/PAO runtimes, and two outbound
Exchange transports. The observed trace was:

```json
{"answer":"Deterministic HASHI acceptance response.","deduplicated":true,"reply_conversation":"conversation_acceptance_1","reply_run":"completed","reply_sender":"reviewer@server.barrytianli","request_receipt":"accepted","server_run":"completed","server_sender":"planner@home.barrytianli"}
```

The scenario verifies an actual Session Message and Run on both HASHI sides,
not a ProbeClient result. It also verifies that an accepted one-second delivery
can complete after expiry, a hidden recipient rejects without a new Run, a
fresh Exchange process causes reauthentication/new epoch/republish, and a fresh
receiving HASHI process requeues the same persisted Run rather than creating a
second one.

## Test evidence and known baseline

Focused red/green reasons were recorded for the defects found during review:

- the former correlation row accepted changed content after its retry frame
  was purged; the new digest-retention test rejects that reuse;
- a merely prepared outbound row formerly authorized an inbound reply; the new
  accepted-state test denies it;
- a scheduled queued Run formerly disappeared with the receiver's memory
  queue; the process-restart test now observes the original IDs in the new
  queue and only one Session Message;
- a public-looking legacy text header formerly reached the public send helper;
  the trust-boundary test now observes no send;
- the WebSocket client formerly followed an HTTP redirect from the configured
  endpoint; the redirect test observes only the configured request;
- a negotiated smaller frame limit formerly reached the socket; the limit test
  now rejects it before transmission; and
- a scheduled inbox row formerly retained a duplicate body indefinitely; the
  lifecycle test now observes redaction after the Run leaves `queued`.

- Protected-Core guard: passed; no protected source changed.
- Changed-file Ruff gate (`E4`, `E7`, `E9`, `F`): passed.
- Final Core gate: 643 passed in 83.62 seconds.
- Initial new Exchange component set: 115 passed, one third-party Starlette
  deprecation warning.
- Final focused integration and direct-consumer set: 296 passed, with one
  third-party Starlette deprecation warning.
- Independent Exchange/HASHI end-to-end set: 3 passed, with no skipped
  scenario after installing the sibling repository into the development
  environment.
- Offline product suite: 4320 passed, 165 deselected, 8 failed. The same exact
  eight nodes also failed on a temporary clean `origin/main` worktree (28 other
  selected cases passed), proving they predate and are independent of this
  branch. They concern AGENT_FYI size, one provider error-code assertion, four
  date-sensitive model-capability cache assertions, one Superloop fixture, and
  one existing Agent-management assertion.

This is offline source verification, not live adoption by a running HASHI
instance and not an Internet acceptance test.
