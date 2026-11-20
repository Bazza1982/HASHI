# Pure Idiot Test (PIT) v1.0 — 大傻逼测试

**Status:** Authorised project quality standard, proposed in documentation PRs.  
**Owner:** Product owner (user); implementation and independent review agents execute the protocol.  
**Applies to:** Bazza1982/HASHI and Bazza1982/hashi-workbench-v2; shared features must be evaluated end-to-end across both.  
**Defined:** 20 November 2026.  
**Canonical trigger:** **"run PIT test"**, **"运行 PIT"**, **"PIT review"**.  
**Principle:** **Default path is the product. Green tests are not proof of a usable feature.**

PIT is a recurring, independent **product reasonableness and delivery** review, not a substitute for security, correctness, or functional regression tests. It challenges unnecessary engineering machinery, false success, default-disabled delivered features, over-defensive behaviour, and tests that defend history rather than actual user value. Findings require evidence and a simpler alternative, not just a rude label. The name and taxonomy are the product owner's.

## 1. Fixed taxonomy

| ID | Chinese name | English name | Detection question |
| --- | --- | --- | --- |
| PIT-01 | 傻逼开关 | Idiot Switch | Was a requested and implemented normal feature hidden behind an unnecessary gate or a default-off flag? |
| PIT-02 | 傻逼测试 | Idiot Test | Does the test pass while the default real user scenario is broken, or actively enforce a bad legacy behaviour? |
| PIT-03 | 傻逼文案 | Idiot Wording | Is user-facing copy unnatural, dominated by prohibitions/negations, internal jargon, or AI-style defensive explanations instead of clear actions and outcomes? |
| PIT-04 | 傻逼逻辑 | Idiot Logic | Would an independent second look reject the assumptions or steps as unnecessarily irrational? |
| PIT-05 | 傻逼安全 | Idiot Security | Are protective checks disproportionate to the actual risk, repeatedly blocking an authorised everyday action? |
| PIT-06 | 傻逼修复 | Idiot Fix | Does fixing A repeatedly break B, reintroduce A, or add patches rather than remove the common root cause? |
| PIT-07 | 傻逼回执 | Idiot Success | Does Ready/Connected/Completed/Saved/Passed appear when the action has not actually happened or cannot be used? |
| PIT-08 | 傻逼性能 | Idiot Performance | Are there needless waits, polling loops, locks, expensive calls, unnecessary redraws, or large payloads in routine use? |
| PIT-09 | 傻逼配置 | Idiot Configuration | Is a user's saved configuration ignored, silently stripped, overwritten, or contradicted by the effective runtime? |
| PIT-10 | 傻逼恢复 | Idiot Recovery | Is a recoverable condition turned into a permanent or non-retryable failure, forced restart, or repeated setup? |
| PIT-11 | 傻逼复杂度 | Idiot Complexity | Is complexity disproportionate to the behaviour, with overlapping owners, duplicated state, unnecessary abstractions or too many steps? |
| PIT-12 | 傻逼交付 | Idiot Delivery | Was code merged and tests green while real installation, default activation, wiring, availability, or live verification was never completed? |

Assign **one primary category** and optional secondary categories; do not count a single root cause as 12 unrelated defects. **PIT-13 / Idiot PIT** is a *self-check*, not an automatic 13th failure: the PIT procedure must never itself become the bureaucracy it was invented to detect.

## 2. The five mandatory questions

1. **Would a normal user expect this?** What would the user naturally try first?
2. **Does it actually help the user?** What observable value does this branch, gate, test, timeout, or message create?
3. **Is there a simpler, more obvious solution?** If so, what concrete behaviour would replace it?
4. **Are we protecting the user or protecting the code?** Are historical constraints/green tests serving real correctness?
5. **If this were removed, what would actually get worse?** Name a concrete privacy, safety, data-loss, cost or reliability regression; otherwise do not invent one.

These are critical thinking prompts, not a compulsory approval ceremony. A justified feature flag, permission boundary, cache retention limit, or test is not a PIT failure merely because it exists.

## 3. Three complementary modes

### PIT-S — Static Idiot Scan (source and tests)

Inspect main and relevant PR heads, default configuration and onboarding, flags, enablement callers, unused/redundant features, tests that enable the feature only in fixtures, assertions freezing historical/incorrect defaults, user-visible wording, swallowed errors, hardcoded timeouts/caps, silent config mutation, retry decisions, repeated polling, persistent resources, and previous related repairs. Trace *definition → wiring → default configuration → actual call site → observable result*.

Use targeted search as a discovery tool; **every confirmed finding must cite exact current source and a meaningful call path**. A text match or an occurrence of False/disabled is only a candidate, never proof.

### PIT-U — User Reality Test (real default behaviour)

With an unmodified fresh default user/agent/installation, attempt the normal user action without injecting test-only enablement. Verify visible state and durable outcome across service/API/UI/restart when applicable. Include meaningful negative and recovery cases: Chinese-language content, narrow desktop viewport, stale/offline connection, waiting capacity, file deletion to recycle bin, large output/draft, restart/cancellation, and background/foreground transitions where relevant.

Measure user latency, resources and retries when claiming a performance defect. Compare baseline vs candidate; do not invent performance numbers. **If live desktop, local files, private settings or deployment access are unavailable, report "PIT-U NOT RUN" with the exact missing evidence. Never turn static inference into a claimed live pass.** Do not change production state to test without explicit authorisation.

### PIT-R — Reasonableness Review (independent challenge)

Challenge the original requirement assumptions, number of steps, scope of the protective boundary, meaningful user path, alternative design, maintenance cost, false successes, and second-order regressions. Review historical bug/PR clusters to distinguish a repeatedly patched symptom from the shared root cause. Prefer a small coherent simplification to another layer of rules. Seek counterevidence for every severe assertion.

## 4. Invocation contract (this is what "run PIT test" means)

- **Bare "run PIT test":** Run a cross-repository PIT audit of **HASHI + Workbench** using their current default branches; include active/new feature PRs that materially affect normal use, if accessible. Perform PIT-S and PIT-R comprehensively, PIT-U to the extent the environment allows. Compare with previous PIT findings and reopened regressions. Return one unified prioritised repair table plus uncovered test/production gaps.
- **"run PIT test on [repo / PR / function]":** Focus on that scope, then trace the interacting default-path and other repository contracts if implicated. Do not pretend a PR diff alone proves adoption.
- **For every new user-requested function:** Run a **focused PIT Quick** at design/handover and PR review. Ask whether the function is directly available by default, whether UI says the truth, whether it works for ordinary users, and whether the new constraints are proportionate. One useful negative test is enough when appropriate; do not create dozens of ceremonial tests.
- **"full PIT":** Expand discovery across both repositories and historic findings, with default-installation scenarios and cross-repository end-to-end acceptance wherever available.
- Any unavailable source, active version, runtime, machine, or test harness is marked as **unverified**, not guessed. The phrase "PIT passed" must always state scope and modes actually tested.

## 5. Severity and proof

| Priority | Meaning | Expected handling |
| --- | --- | --- |
| P0 | Core function unusable, user data lost/corrupted, false completion, or critical everyday path blocked | Fix first; cannot claim relevant feature delivered until verified |
| P1 | Clearly unreasonable friction, degraded reliability, needless costs/waits, broken defaults or common recoverability | Prioritise next focused change with realistic acceptance |
| P2 | Unnecessary complexity, awkward language or lower-impact UX issue | Simplify when the relevant owner is modified; avoid sweeping refactors |

**Evidence state is independent of severity:** Confirmed-by-source / Reproduced-live / Suspected / Not-run / Fixed-in-branch / Merged / Verified-in-runtime. A source-level certainty is not a production deployment claim.

## 6. Required repair-table row

For each issue, capture:

- **PIT ID; repo; category; priority; evidence state.**
- **Normal user intention → observed default behaviour → expected intuitive behaviour.**
- **Exact source permalink / lines**, config, tests and effective call path; where available, a reproducible observed result.
- **Why PIT**: which assumption, gate, false status, or extra work fails the five-question test.
- **Minimal root-cause fix** and the existing correct constraints it must preserve.
- **PIT regression check:** one default-path positive case, failure/recovery or counterexample as applicable, plus observable receipt.
- **Related previous PR/fix**, owner, candidate PR, and **deployment verification status**.

Group multiple symptoms into one underlying repair when justified. Sort by user impact, not by insult or number of code matches. Produce **one consolidated table** for both repositories, with a concise executive summary and a short "unverified" section.

## 7. Non-negotiable acceptance conditions

A finding is **Verified** only when its ordinary/default user path works, red/green evidence demonstrates the fault is gone, nearby behaviour remains sound, and real installation evidence confirms adoption when applicable. **CI green, a helper called with enabled=True, a merged PR, and a successful simulated call are not sufficient on their own.** Clearly separate built, tested, merged, deployed, and user-verified.

For a new normal capability explicitly requested by the owner, **prefer usable-by-default** and documented disablement over a hidden default-off switch. A control must justify a material user choice or real risk; it cannot be added simply because an engineer is afraid of changing an old test.

For simple user actions (e.g. moving a file to recycle bin), default UX should be simple and prompt; retain only necessary authorisation, path validation and recoverable data-protection boundaries. Never require a ceremonial chain of tests/confirmations just because tests are easy to add. Avoid duplicating confirmation after the user has already authorised the same bounded action.

Tests must assert meaningful outcomes rather than brittle strings, fixed hashes, mock-only success, or arbitrary limits unless those are actually part of an explicit product contract. Where a historical test enforces a PIT failure, **update/remove the wrong assertion and replace it with a user-behaviour contract**; do not work around it by default-disabling the intended feature.

## 8. Safety and truthfulness guardrails

PIT is **not** permission to remove meaningful privacy controls, workspace boundaries, authentication, authorisation, confirmation for irreversible/destructive operations, restrictions on sending data to a new provider, or consent for extra paid calls. The task is to make these boundaries **proportionate, observable, minimally intrusive and usable once authorised**, not to bypass them.

Do not call every opt-in idiotic: external integrations, remote egress, recurring background activity, paid providers and high-risk actions can legitimately default off. Explain the exact user/risk reason, whether it can be simplified, and how the user discovers and enables it.

Do not silently substitute a model's confidence for observed runtime evidence. Do not make an independent PIT review self-certifying: the reviewer must challenge implementation assumptions and should not be the author of the same change when practical.

## 9. PIT should be light, not another gate factory

- **No mandatory new CI matrix, expensive recurring cloud job, global build block, security override, or extra "PIT enabled" feature switch is created by this standard.**
- Apply focused PIT Quick in the new-feature workflow; run full PIT on explicit request or periodically when the owner chooses.
- Run PIT static review with existing repo search/test tooling. Add executable tests only where they protect a real previously failed user scenario.
- Each additional check must be justified by the fifth question. **The PIT itself must never become the idiocy it was designed to detect.**
- Preserve HASHI protected Core boundaries and Workbench's ownership and authentication contracts. Documentation PR does not authorise deployment or runtime migration.

## 10. Examples / known regression seeds (not claimed live verification)

- Default-disabled tool runtime or memory lookup that tests only with explicit enablement → PIT-01 / PIT-02 / PIT-12.
- Pure-Chinese text yielding a zero semantic embedding and memory relevance without a meaningful threshold → PIT-04 / PIT-02.
- A 1366×580 desktop viewport routed to phone navigation → PIT-04 / PIT-02.
- An 8 MiB React Thinking block protected by a test but not by evidence of rendering responsiveness → PIT-08 / PIT-02.
- A draft persistence function returns success after ignoring a failed storage write → PIT-07 / PIT-10.
- A configured setting silently removed by normalisation → PIT-09.
- A long queue timeout turned into a non-retryable failure for ordinary capacity contention → PIT-08 / PIT-10.
- Recurrent startup/restart/polling repairs without an end-to-end default-usage acceptance test → PIT-06 / PIT-12.

**Reusable short handover sentence for coding agents:** "Apply PIT Quick (docs/PURE_IDIOT_TEST.md): verify default real-user value, remove unjustified switches/gates, challenge meaningless tests and unnatural UI copy, and supply one observable ordinary-path acceptance check. Never declare a feature delivered solely because CI passed."
