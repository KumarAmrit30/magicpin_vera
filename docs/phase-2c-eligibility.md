# Phase 2C — Eligibility & Suppression

`app/engine/eligibility.py` decides whether each Phase 2B `DecisionCandidate`
may still be acted on, given the **current** context, the suppression state and
an explicit evaluation time.

```text
DecisionCandidate[]  (Phase 2B)
      │
      ▼
eligibility checks   structure → trigger/scope integrity → evidence → freshness → merchant state → conversation
      │
      ▼
suppression checks   candidate suppression_key → merchant-wide key          (read-only: SuppressionReader.peek)
      │
      ▼
EligibilityResult(candidate, eligible, reasons)[]   same order as the input
      │
      ▼
Phase 2D ranking / winner selection  (not here)
```

- **Eligibility ≠ ranking.** Nothing here scores or orders candidates, or picks one. Similar candidates are not de-duplicated; for example, each per-offer `DRAFT_CAMPAIGN` is evaluated on its own.
- **Evaluation ≠ suppression write/commit.** Evaluation only *reads* suppression, and `SuppressionStore.peek` never mutates the store (unlike `get`, which drops expired records). Recording a key after a send belongs to the later commit phase.
- **Pure.** The entry point is `evaluate_eligibility(candidate, context, suppression, *, now)`. It has no wall clock, does no I/O and mutates nothing; the same inputs always produce the same result. `now` must be timezone-aware and is normally the tick's `now`. `evaluate_candidates` evaluates a batch in input order, and `eligible_candidates` filters the results while keeping that order.
- **Reasons.** Each reason is an `EligibilityReason(code, detail, source)`. Every failed rule is collected, each code at most once, in the declaration order of `EligibilityReasonCode`. `detail` holds only ids, field paths and timestamps, never customer payload values. Reasons are machine-readable and never user-facing.
- **`NO_ACTION` is not a send.** It is checked only for structure, trigger/scope integrity and evidence grounding. It is exempt from the freshness, state, conversation and suppression rules, because staying quiet is always allowed. It must never be written to suppression.

## Rules

Scope column: **M** = merchant-scoped candidates, **C** = customer-scoped candidates, **M+C** = both. "Impl" marks an implementation decision rather than an explicit challenge rule.

| # | Code | Source | Condition (rejects when …) | Scope | Applies to NO_ACTION |
|---|------|--------|----------------------------|-------|---------|
| 1 | `structure_invalid` | Impl: Phase 2A `DecisionCandidate` invariants, re-validated rather than re-implemented | the candidate no longer validates (e.g. it was built with `model_construct`). Reported alone. | M+C | yes |
| 2 | `trigger_mismatch` | Impl | `candidate.trigger_id` ≠ current trigger id | M+C | yes |
| 3 | `trigger_changed` | testing-brief §context versions: "bots should use the latest context" | same trigger id, but its archetype, `suppression_key` or `expires_at` differs from the current trigger version | M+C | yes |
| 4 | `merchant_mismatch` | Impl | `candidate.merchant_id` ≠ context merchant | M+C | yes |
| 5 | `customer_not_in_context` | brief §4.4 (the customer context is required for customer-facing messages) | the candidate names a customer that is absent from, or different from, the context customer. The context already guarantees the customer belongs to the merchant. | M+C | yes |
| 6 | `conversation_mismatch` | Impl | the context conversation belongs to another merchant, or names another customer | M+C | yes |
| 7 | `evidence_missing` | brief §5 constraint 8: "Don't fabricate" | a sendable candidate has no evidence | M+C | no |
| 8 | `evidence_not_grounded` | testing-brief (context injection): "sending stale composition" scores lower, hallucinating scores lowest; brief §8 twist | any evidence item's value (type-strict) no longer matches the current context. Detail lists `source:path`. | M+C | yes |
| 9 | `trigger_expired` | engagement-design: "`expires_at` — after which the trigger is stale" | `now > trigger.expires_at` | M+C | no |
| 10 | `offer_unavailable` | brief §4.2 (offers can be active, paused or expired); §5 constraint 8 ("no fake offers") | `selected_offer_id` is not an active merchant offer any more | M+C | no |
| 11 | `merchant_state_conflict` | Impl: re-checks Phase 2B trigger premises | `gbp_unverified` while `identity.verified` is true; `renewal_due` while `subscription.status` is outside `{active, trial}`; `winback_eligible` while `subscription.status` is `active` | M | no |
| 12 | `conversation_closed` | api-examples 2.6: "must not send any further messages on this conversation_id" | the conversation this candidate would be sent into is `ended` or `completed` | M+C (target thread only) | no |
| 13 | `nudge_limit_reached` | brief §12.5, an extra-credit open challenge: "gracefully exit … after 3 unanswered nudges" | ≥ 3 consecutive bot messages without a reply from the recipient. For merchant scope this counts merchant history plus the merchant thread; for customer scope, only that customer's thread. A recipient turn, or a bot turn tagged as engaged (`merchant_replied`, `intent_*`), resets the count. | M+C | no |
| 14 | `suppressed` | brief §4.3: "`suppression_key` — for dedup"; engagement-design: "dedup to prevent re-sends"; api-examples 2.3: "all have been suppressed" → `[]` | `suppression_key` is active in the store at `now` | M+C | no |
| 15 | `merchant_suppressed` | api-examples 4.3 (hostile): "Suppressing all triggers for this merchant" | key `suppress:merchant:<merchant_id>` is active at `now` | M+C | no |

### Target conversation

`context.conversation` is the thread the decision would continue. A merchant
thread (`customer_id` null) is the target for merchant-scoped candidates; the
customer's own thread is the target for customer-scoped candidates. Rules 12 and
13 look only at the target thread.

A tick that opens a new conversation passes no conversation. That follows
api-examples 2.6, where a hard "no" suppresses the **conversation_id**, not the
merchant.

### Suppression semantics

- **Key:** `candidate.suppression_key` is used verbatim, with no prefixing or normalisation. It is the trigger's key, or `f"{kind}:{trigger_id}"` when the trigger has none (Phase 2B). Every candidate of one trigger shares that key, so one send suppresses all of them.
- **Scope** lives in the key itself. For example, `recall:c_001_priya_for_m001:6mo` covers one customer and `research:dentists:2026-W17` covers a category/week. All 100 expanded-dataset keys are distinct.
- **Merchant-wide key:** `suppress:merchant:<id>` is a separate namespace for the hostile or opt-out case (4.3). No writer exists yet; eligibility only reads it.
- **Read:** `SuppressionReader.peek(key, now)` returns a record only if it is active, i.e. `now < expires_at` or it has no expiry. It never mutates.
- **Write/commit:** not in Phase 2C. The commit phase must record `suppression_key` only for actions actually sent, never for `NO_ACTION` or for evaluated-but-unsent candidates.
- **TTL:** the challenge specifies none for trigger-level dedup. Eligibility honours whatever `expires_at` the writer set and invents no TTL. The "30 days" in 4.3 appears only in an example rationale; choosing it is the writer's decision.

## Time

- Only the trigger's `expires_at` bounds freshness.
- The boundary follows the literal wording "after which": `now == expires_at` is still fresh, and `now > expires_at` is expired. Suppression records use the store's existing rule, where a record at `now == expires_at` is no longer active.
- Payload event times (`deadline_iso`, `due_date`, `wedding_date`, `stock_runs_out_iso`, `match_time_iso`, festival `date`) are not enforced separately. In the dataset every one falls on or before its trigger's `expires_at`, so `expires_at` is the stricter or equal bound, and the package defines staleness only through it.
- No generic freshness windows are added.

## Not implemented (unspecified by the package, so not invented)

| Topic | Why |
|-------|-----|
| Consent / opt-in gating | Customer `consent` and `preferences` fields exist, but no eligibility rule is stated; engagement-design lists consent as an open question. Every customer trigger in the dataset has `opted_in_at`. |
| Customer-state gating (e.g. lapsed-only winback) | No rule is stated, and the dataset contradicts one (`customer_lapsed_soft` triggers point at `active` and `churned` customers). |
| Customer-wide opt-out key | Only the merchant-wide case appears in the package (4.3). Deferred to reply handling (Phase 2F). |
| Frequency caps, cooldowns, quiet hours | Not in the package. The only count rule is §12.5 (rule 13), and it has no time window. |
| Wait/back-off expiry (auto-reply `wait_seconds`) | Conversation state records no wait deadline. The writer may model it later as a suppression record with `expires_at`. |
| Text intent (rejection, hostility, off-topic) | Classifying free text belongs to reply handling (Phase 2F). Eligibility consumes the outcome: conversation state and suppression records. |
| One action per (merchant, conversation) per tick; 20-action cap; body anti-repetition | These are tick-assembly and composition constraints (Phase 2D/2E and Phase 3), not per-candidate eligibility. |
| Category-wide keys shared across merchants | The package does not say whether a key like `research:dentists:2026-W17` dedups per merchant or globally. It is kept verbatim; in the data each key belongs to one trigger. |
