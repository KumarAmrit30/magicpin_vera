# Phase 3D — Evaluation and Targeted Optimization

Phase 3D measured how Vera's messages are scored, took a baseline, and made three
small wording changes aimed at measured gaps. No decision logic changed.

## 1. Authoritative scoring materials

| Source | What it defines |
|---|---|
| `challenge-brief.md` §8 | The five scored dimensions, 0–10 each: **Specificity**, **Category fit**, **Merchant fit**, **Trigger relevance**, **Engagement compulsion** |
| `challenge-brief.md` §11 | Anti-patterns, including ignoring the language preference, generic copy, and multiple CTAs |
| `challenge-testing-brief.md` FAQ | The rationale is part of what is judged |
| `case-studies.md` cross-case patterns 1–10 | Numbers must come from the contexts (#2), use the owner's name (#3), and the rationale must match the message; a mismatch is penalised (#9) |

`judge_simulator.py` names dimension 4 "TRIGGER RELEVANCE" in its prompt but
stores it as `decision_quality`; the two are the same score.

## 2. What `judge_simulator.py` measures

- **With an LLM:** the prompt asks for the five dimensions. Only the body, CTA and
  rationale are sent, together with the contexts.
- **Without an LLM (StubLLM, as in every local run):** `_fallback_score` counts
  digit runs in the body (`re.findall(r'\d+', body)`). Specificity is
  `min(10, 3 + 2n)`, and the other four dimensions are fixed at 5, so the
  maximum is 30/50.
- It never compares against an expected action, CTA or rationale. The
  `auto_reply`, `intent` and `hostile` scenarios check only the reply action.
- It ticks at the real current time and pushes no customers. On 28 Sep 2026
  only `trg_002` (DCI, 30/50) and `trg_006` (Diwali, 29/50) are emitted. Of the
  other seed triggers, 18 have expired (`no_action`) and 5 are customer triggers
  whose customer was never pushed (`missing_context`).

No local run can measure category fit, merchant fit, trigger relevance or
engagement. Phase 3D therefore uses the harness below to collect mechanical
evidence only, and does not optimize for the fallback digit count.

## 3. Evaluation harness

`scripts/evaluate_vera.py` is a dev-only, read-only tool. The app and tests
never import it. It runs every case through the real app (FastAPI `TestClient`,
fresh state) and records ids, decision fields, body, rationale, template, the
facts used, and whether the decision matches the Phase 2D result computed
directly.

```bash
python scripts/evaluate_vera.py                      # 30 canonical pairs (generated into a temp dir)
python scripts/evaluate_vera.py --set seed           # 25 seed triggers
python scripts/evaluate_vera.py --simulator --now …  # the simulator's full_evaluation inputs
python scripts/evaluate_vera.py --json out.json --compare before.json   # A/B
```

Metrics are mechanical counts, with no combined score and no ranking:
- length, sentences, questions, and whether the CTA is the last sentence;
- digit runs and the simulator fallback specificity;
- numbers not found in any context;
- rendered facts, and grounded evidence that was not rendered;
- the merchant's own figures (grounded and rendered);
- whether a customer body names its moment;
- off-category words;
- whether the body names the person, and whether it cites a source;
- language preferences;
- leaks (ids, snake_case, internal terms, placeholders);
- whether the wire body equals the composer output.

## 4. Baseline

- **Simulator:** `all` PASS; `phase2_short` 30/50; `full_evaluation` 30/50 and 29/50.
- **pytest:** 1657 passed.
- **Canonical 30 (27 emitted):**
  - CTA is the last sentence in 27/27; questions per body {0: 9, 1: 18};
  - names the person in 27/27; no leaks; no unverified numbers;
  - merchant's own figure rendered in 3/4 bodies whose plan grounds one;
  - customer moment named in 6/9;
  - off-category words in 2 bodies.

## 5. Canonical cases

Issue categories are factual observations from the harness. They are not ranked.

| Case | Trigger kind | Action | Scope | CTA | Fallback spec | Issues before | Issues after |
|---|---|---|---|---|---|---|---|
| T01 | active_planning_intent | draft_artifact | merchant | binary_confirm_cancel | 5 | hi language, English body | hi language, English body |
| T02 | active_planning_intent | draft_artifact | merchant | binary_confirm_cancel | 3 | hi language, English body | hi language, English body |
| T03 | appointment_tomorrow | send_customer_reminder | customer | binary_confirm_cancel | 7 | no trigger fact rendered; moment not named | - |
| T04 | appointment_tomorrow | send_customer_reminder | customer | binary_confirm_cancel | 7 | no trigger fact rendered; moment not named; hi language, English body | hi language, English body |
| T05 | category_seasonal | recommend_operational_fix | merchant | binary_yes_no | 10 | hi language, English body | hi language, English body |
| T06 | cde_opportunity | send_insight | merchant | binary_yes_no | 10 | hi language, English body | hi language, English body |
| T07 | chronic_refill_due | send_customer_reminder | customer | binary_confirm_cancel | 10 | not rendered: last visit; hi language, English body | not rendered: last visit; hi language, English body |
| T08 | chronic_refill_due | send_customer_reminder | customer | binary_confirm_cancel | 7 | no trigger fact rendered; moment not named | - |
| T09 | competitor_opened | draft_listing | merchant | binary_yes_no | 9 | not rendered: listing gap; hi language, English body | not rendered: listing gap; hi language, English body |
| T10 | competitor_opened | draft_listing | merchant | binary_yes_no | 3 | no trigger fact rendered; hi language, English body | no trigger fact rendered; hi language, English body |
| T11 | curious_ask_due | ask_merchant | merchant | open_ended | 3 | hi language, English body | hi language, English body |
| T12 | curious_ask_due | draft_artifact | merchant | binary_confirm_cancel | 3 | no trigger fact rendered; hi language, English body | no trigger fact rendered; hi language, English body |
| T13 | customer_lapsed_hard | send_customer_winback | customer | binary_yes_no | 5 | not rendered: months as a member, last visit | not rendered: months as a member, last visit |
| T14 | customer_lapsed_soft | send_customer_winback | customer | binary_yes_no | 7 | no trigger fact rendered; off-category word "session" | no trigger fact rendered |
| T15 | customer_lapsed_soft | send_customer_winback | customer | binary_yes_no | 7 | no trigger fact rendered; off-category word "session"; hi language, English body | no trigger fact rendered; hi language, English body |
| T16 | dormant_with_vera | send_insight | merchant | binary_yes_no | 7 | no trigger fact rendered; hi language, English body | no trigger fact rendered; hi language, English body |
| T17 | dormant_with_vera | send_insight | merchant | binary_yes_no | 7 | no trigger fact rendered; hi language, English body | no trigger fact rendered; hi language, English body |
| T18 | festival_upcoming | ask_merchant | merchant | open_ended | 9 | hi language, English body | hi language, English body |
| T19 | festival_upcoming | no_action | merchant | - | - | not emitted (no_action) | not emitted (no_action) |
| T20 | gbp_unverified | recommend_operational_fix | merchant | binary_yes_no | 5 | not rendered: listing verified; hi language, English body | not rendered: listing verified; hi language, English body |
| T21 | ipl_match_today | draft_campaign | merchant | binary_yes_no | 10 | own figure not rendered; not rendered: match time, delivery orders; hi language, English body | not rendered: match time; hi language, English body |
| T22 | milestone_reached | draft_message | merchant | binary_yes_no | 7 | hi language, English body | hi language, English body |
| T23 | milestone_reached | no_action | merchant | - | - | not emitted (no_action) | not emitted (no_action) |
| T24 | perf_dip | recommend_operational_fix | merchant | binary_yes_no | 5 | hi language, English body | hi language, English body |
| T25 | perf_dip | no_action | merchant | - | - | not emitted (no_action) | not emitted (no_action) |
| T26 | perf_spike | draft_post | merchant | binary_yes_no | 5 | hi language, English body | hi language, English body |
| T27 | perf_spike | send_insight | merchant | none | 10 | no trigger fact rendered; hi language, English body | no trigger fact rendered; hi language, English body |
| T28 | recall_due | send_customer_reminder | customer | binary_confirm_cancel | 10 | not rendered: last visit; hi language, English body | not rendered: last visit; hi language, English body |
| T29 | recall_due | send_customer_reminder | customer | binary_yes_no | 7 | no trigger fact rendered | no trigger fact rendered |
| T30 | regulation_change | send_alert | merchant | binary_yes_no | 10 | hi language, English body | hi language, English body |

"No trigger fact rendered" is mostly a data limit. Generated canonical triggers
carry a placeholder payload (`{"placeholder": true, "metric_or_topic": kind}`), so
there is nothing to render beyond the trigger kind.

## 6. Findings for each dimension

- **Trigger relevance (decision quality):** the 30 canonical decisions match
  Phase 2D, and the simulator never checks decisions. It was left alone.
  Measured gap in the wording: three customer reminders never said what they
  were about. The appointment reminders (T03, T04) and the refill (T08) opened
  with "Your last visit…" and ended "Reply CONFIRM to book your next visit".
- **Specificity:** there are no unverified numbers. The lead budget could
  crowd out every one of the merchant's own figures: T21 dropped "180 delivery
  orders", and seed S18 dropped "240 chronic-Rx customers". Other omissions
  (the S01 research finding, IPL match time, months as a member) need broader
  re-prioritisation.
- **Category fit:** the winback question "Want to book a session?" was gym
  wording, used for a dentist (T14) and a pharmacy (T15).
- **Merchant fit:** every body names the owner or customer. Many merchants and
  customers list `hi`, but bodies are English apart from the customer greeting.
  This is a known anti-pattern (§11) that needs a large change.
- **Engagement compulsion:** there is one CTA in every body, always the last
  sentence, with at most one question. Nothing is pushy, so nothing changed.

## 7. Implemented changes (`app/engine/composer.py`, `app/engine/reply_facts.py`)

1. **Reminders name their moment.**
   - An `appointment_tomorrow` reminder says so. It uses the booked time when
     the plan has one ("Your cleaning appointment is on 27 Apr 2026, 6pm.");
     otherwise "This is a reminder about your appointment tomorrow."
   - A refill without medicine names says "Your refill is due."
   - The CTA says what CONFIRM does: "Reply CONFIRM to keep your appointment."
     or "…to arrange your refill."
   - The only source is the trigger kind (`trigger:kind`) or the plan evidence.
2. **Category-worded winback question.** Gyms: "Want to book a session?".
   Pharmacies: "Want us to help with your next order?". Everyone else: "Want to
   book your next visit?".
3. **The merchant's own figure survives the lead budget.** If the 4-fact /
   320-character budget would drop every grounded merchant count or metric,
   the first one dropped is rendered anyway.

   Follow-on fix in Phase 3C: the supply opener now contains "240 chronic-Rx
   customers". Phase 3C would then answer "How many customers are affected?"
   with that total, which is a wrong answer.
   - Count facts now answer a count question only if they cover its words, or
     are a "matching" cohort when the question says affected/impacted/relevant/eligible.
   - Otherwise the reply says the number is missing and offers the total as
     related: "I don't have that number on record. You have 240 chronic-Rx
     customers on record."
   - `test_count_question_without_a_count_is_not_answered_with_a_guess` now
     asserts this honest answer.

## 8. A/B results (same inputs)

| Set | Bodies changed | Decision fields changed | Metric change |
|---|---|---|---|
| Canonical 30 | 6 (T03, T04, T08, T14, T15, T21) | 0 | own figure 3/4 → 4/4; moment named 6/9 → 9/9; off-category 2 → 0 |
| Seed 25 | 2 (S10 IPL, S18 supply) | 0 | own figure 4/6 → 6/6 |
| Simulator inputs | 0 | 0 | — |

- **Official simulator after the change:** identical to the baseline (`all`
  PASS; 30/50; 30/50 and 29/50). The two scored messages are unchanged.
- **Demo:** output identical to Phase 3C.
- **pytest:** 1674 passed, including `tests/test_composer_optimizations.py`.

## 9. Deliberately not implemented

- **Recomputing "188 days away" at the simulator clock:** the LLM prompt shows
  the payload's `days_until`, so a recomputed value could be flagged as fabricated.
- **Raising the Diwali message's fallback score:** it has no grounded evidence
  left unrendered. Adding numbers would mean adding non-plan facts, which is
  score-chasing.
- **Hindi-English code-mix:** a large wording change, out of scope for a
  targeted phase.
- **Rendering the S01 research finding (38%), IPL match time, months as a
  member, and the "Milestone: 150." phrasing:** these need a broader
  re-prioritisation of Phase 2B evidence importance, which is frozen.
- **T05 duplicated trend lines without units, and ISO dates inside free text
  (T30 / `trg_002`, which the simulator scores):** these are payload text quoted
  verbatim, and rewriting it risks changing meaning.
- **T12 "I can draft it" with no referent, and placeholder triggers with no
  facts:** these are data limits.

## 10. Limitations

- Only the fallback path can be run locally, so four dimensions are unmeasured.
- The harness metrics are proxies. They show what changed, not how a judge
  would score it.
