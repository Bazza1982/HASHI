# HER official DeepSeek Flash fast debug Superloop

This branch runs the HER/Habit debug campaign only through the official
DeepSeek API with `deepseek-v4-flash`. OpenRouter and Pro are deliberately out
of scope so route duplication and the Flash-before-Pro lock cannot delay fault
discovery.

## Matrix

- provider: `official_deepseek` only;
- model: `deepseek-v4-flash` only;
- modes: `fixed`, `flex`;
- efforts: `low`, `medium`, `high`, `xhigh`, `max`, `max+`;
- 12 `CORE-OFF` cells, each retaining all ten scenario groups and eight
  presentation combinations;
- 12 `HABIT-WIRE`, two high-effort `HABIT-DEEP`, and one high-effort
  `HABIT-FAULT` item.

The resulting live campaign has 27 work items, 120 core scenario groups, and
96 presentation runs. Efforts are executed from low through max+ so a defect
is found at the cheapest useful level before more expensive coverage runs.

## Kept gates

The fast profile still requires a frozen composite HASHI/HER oracle candidate,
exact provider/model receipts, no fallback, CORE-OFF and Habit-on separation,
an exact failing-packet repair/retest cycle, affected Flash revalidation after
shared-runtime repairs, and a final same-candidate gate.

## Removed delays

- OpenRouter is neither required nor an allowed substitute.
- Pro traffic and every Pro unlock/revalidation task are removed.
- A nonterminal failure is classified and acted on immediately; it does not
  automatically create a follow-up wait.
- A wait is valid only for a concrete external blocker such as an active worker,
  a bounded official-route outage, or confirmed funds investigation.
- An idle eligible packet is dispatched immediately. One stagnant observation
  must surface a blocker or validation finding rather than merely moving a
  next-check timestamp.

Only `PASSED` and confirmed official-route `BLOCKED_FUNDS` are terminal. Timeouts,
rate limits, authentication errors, malformed replies, worker failures, and
product defects remain nonterminal repair or bounded-retry work.

## Operation

```bash
python scripts/her_debug_superloop.py instantiate
python scripts/her_debug_superloop.py validate <loop_id>
```

The controller remains `lin_yueru@HASHI2`; Ajiao remains the isolated live
test-and-repair worker. Scheduler auto-advance stays disabled, while the
controller nudge may continue already-authorized campaign packets until the
terminal result.
