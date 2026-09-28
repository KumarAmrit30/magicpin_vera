# Phase 3 — Message Composer

`app/engine/composer.py` turns an already-selected `DecisionPlan` into the WhatsApp message that realizes it. The tick planner calls it once per emitted plan. Everything upstream of the selected plan is unchanged from Phases 2A–2E.

## 1. Contract

```python
compose(plan: DecisionPlan, context: CandidateGenerationContext) -> ComposedMessage
```

| Field | Meaning |
|---|---|
| `body` | The message text |
| `cta` | Wire `CtaType`, via `WIRE_CTA[plan.cta_type]` |
| `send_as` | `plan.send_as`, unchanged |
| `template_name` | `vera_<action>_v1` |
| `template_params` | The values inserted into the body, in order of appearance (all strings) |
| `template` | The body with `{{n}}` in place of each parameter. `reads_as(message) == body` |
| `facts_used` | `source:field` of every context value the body renders |

- **Input.** The selected plan plus the same context Phase 2B–2D decided on (the planner keeps it on the emitted `_Planned` entry). The composer sees no candidates, scores or rankings.
- **Rejections.** `NO_ACTION`, or a context for a different trigger, merchant or customer, raises `CompositionError` (a `ValueError`). The planner still refuses `NO_ACTION` plans before composing.
- **Rationale.** `TickAction.rationale` is still built by the planner from the plan, in the Phase 2E format.

## 2. Contract sources

- `challenge-brief.md` §5:
  - The first outbound uses a template with positional params.
  - A single primary CTA.
  - Don't fabricate.
  - Match the category voice.
  - `merchant_on_behalf` messages are the customer-facing message.
- `challenge-brief.md` §10–11 and `examples/case-studies.md`:
  - Specificity, owner first name, source citation for research and compliance.
  - The CTA lands in the last sentence.
  - No preambles, no promotional tone.
- `challenge-testing-brief.md` §2.2: the `template_name` / `template_params: list[str]` shape.
- `category.voice`: `salutation_examples` and `vocab_taboo`.

## 3. Grounding

Facts come only from:

1. **The plan's evidence**, most important first, kept only if `is_grounded(evidence, context.source_data(source))` still holds.
2. **Context identity:**
   - `merchant.identity.owner_first_name` and `merchant.identity.name`
   - `customer.identity.name`
   - `category.voice.salutation_examples`
   - `category.digest[n].source`, for the digest item a rendered fact came from

Phase 2B formats each evidence item as `"<label>: <value>"` from a fixed label vocabulary. The composer handles labels as follows:

- **Hidden labels** are never rendered. These are internal codes and routing hints (trigger kind, merchant signal, customer state, cohort/segment ids, preferred-slot codes, sentiment) and conversation quotes, which would be verbatim repeats.
- **Known labels** use a phrase table, e.g. `compliance deadline` → `Deadline: {}.`
- **Change metrics** (`calls change (7d): -30%`) become `Your calls are down 30% over the last 7 days.`
- **Known listing-gap codes** become plain phrases. Unknown codes are dropped.
- **Unknown labels** fall back to `Label: value.`

Value formatting changes presentation only:

- ISO dates become `15 Dec 2026`, and datetimes `2 May 2026, 7pm`.
- Single snake_case tokens become words.
- Slot lists become `A or B`.
- Numbers keep Phase 2B's rendering.

Missing values are omitted, and the message falls back to a less specific sentence. For example, a confirmation reminder without slots says `Reply CONFIRM to book your next visit.` A fact is also skipped if:

- it contains a category taboo phrase;
- it contains a `?`, which would add a second question;
- it duplicates an earlier sentence.

The lead is capped at 4 facts / ~320 characters. The first fact is always kept.
If the cap would drop every grounded merchant figure (the merchant's own counts
and metrics), the first figure it dropped is rendered anyway (`You've had 180
delivery orders in the last 30 days.`).

## 4. Structure and voice

**Merchant-facing (`vera`):**

- **Salutation:** the first category salutation filled from the merchant identity (`Dr. Meera`, `Hi Suresh`), followed by `—`. It is omitted if the name is missing.
- **Lead facts**, with a digest source citation when one applies.
- **Proposal**, action-specific and always an offer, never a claim that something was done:
  - `I can draft a campaign around your “<offer>” offer.`
  - `The fixes I'd start with: …`
  - `Suggestion: focus on retaining your <n> active members for now.`
  - `SEND_INSIGHT`, `SEND_ALERT` and `ASK_MERCHANT` have no proposal; the facts are the message.
- **The CTA.**

**Customer-facing (`merchant_on_behalf`):**

- It opens with `Hi|Namaste <name>, <business> here.`
  - `Namaste` is used when the customer's `language_pref` is `hi` or `hi-en mix`.
  - For `Name (parent: X)`, it greets X and says `Name's …`.
  - Placeholder names like `(walk-in, no profile)` get no name.
- Only customer-facing facts are allowed: due dates, services, the last visit or refill, medicines, trial and wedding dates, the next step, the active offer and open slots.
- There is no Vera persona and no merchant-internal metric.
- Reminders name their moment:
  - An `appointment_tomorrow` reminder gives the booked time when the plan has
    one (`Your cleaning appointment is on 27 Apr 2026, 6pm.`), otherwise
    `This is a reminder about your appointment tomorrow.`
  - A refill without medicine names says `Your refill is due.`
  - The CTA says what CONFIRM does: `Reply CONFIRM to keep your appointment.` /
    `Reply CONFIRM to arrange your refill.`
- The winback question is worded for the category:
  - gyms: `Want to book a session?`
  - pharmacies: `Want us to help with your next order?`
  - everyone else: `Want to book your next visit?`

**Category vocabulary:** customers are `patients` for dentists, `members` for gyms, `clients` for salons, and `customers` otherwise.

**Archetype tone** follows from the evidence each archetype carries:

- Safety and compliance: regulation, deadline and source, with no exclamation.
- Performance: signed change with direction.
- Competitive: competitor, distance, and what reviews praise.
- Customer timing: dates and slots.
- Operations: subscription and review facts.
- Market opportunity: event, date and source.
- Active intent: the merchant's own plan or question.

## 5. CTA mapping

The mapping is the Phase 2E one; no new wire values were added.

| Planning `CTAType` | Wire `CtaType` | Body ending |
|---|---|---|
| `yes_no` | `binary_yes_no` | Exactly one question, the last sentence (`Want me to draft it?`) |
| `open_ended` | `open_ended` | Exactly one question, the last sentence (`What are you planning for Diwali?`) |
| `confirmation` | `binary_confirm_cancel` | `Reply CONFIRM to …`, no question mark |
| `none` | `none` | No question and no `Reply CONFIRM` |

## 6. Template names and params

- **Names:** `vera_<action.value>_v1`, following the Phase 2E `vera_<action>_v0` placeholder convention and the testing brief's `vera_research_digest_v1` style.
- **Params:** exactly the values substituted into the body, e.g. `["Meera", "3-month fluoride varnish recall…", "124", "JIDA Oct 2026, p.14"]`. They never contain trigger ids, suppression keys, plan ids, scores or rationale strings.

## 7. Determinism

There is no clock, randomness, UUID, LLM or I/O. Output depends only on the plan and the context payloads; `context.now` is not read. Evidence order is the plan's stable importance order.

## 8. Tick integration

Only the realization layer changed in `app/engine/planner.py`:

- The emitted `_Planned` entry keeps its `CandidateGenerationContext`.
- `_assemble` returns the selected `_Planned` entries instead of bare plans. Ordering, de-duplication and the cap are unchanged.
- `tick_action(plan, conversation_id, context)` builds `body`, `template_name`, `template_params`, `cta` and `send_as` from `compose`. All other fields, and the rationale, are unchanged.
- `WIRE_CTA` moved to the composer and is re-exported by the planner. `PLACEHOLDER_BODY_PREFIX` / `PLACEHOLDER_TEMPLATE_VERSION` were removed.

Plans, plan ids, suppression keys, conversation allocation and commits are identical to Phase 2E. This is verified over the 30 canonical pairs.

## 9. Reply scope

`/v1/reply` bodies are not `DecisionPlan`-based, so this composer does not word them. Phase 3B does, in `app/engine/reply_composer.py`; see [`phase-3b-reply-composer.md`](phase-3b-reply-composer.md).

## 10. Known limitations

- English only apart from the greeting; Hindi-English code-mix wording is deferred.
- `language_style` and `tone_profile` are `None` on every plan and are not used.
- Phrases for labels outside the table fall back to `Label: value.`, which is grounded but plain.
- Merchant-facing plans built on customer triggers (non-winning candidates) render customer facts as labelled values without naming the customer.
- Long digest summaries are quoted as written; the lead budget keeps later facts short but never truncates a sentence.
