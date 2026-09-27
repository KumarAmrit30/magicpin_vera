"""Phase 2C eligibility across the seed matrix, the full expanded dataset and the 30 canonical pairs.

The expanded dataset is generated into a pytest temp dir (see the ``expanded``
fixture); nothing is written into the repository.
"""

import copy
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.engine import ActionType, TRIGGER_KIND_ARCHETYPES
from app.engine.candidates import generate_candidates
from app.engine.eligibility import EligibilityReasonCode, EligibilityResult, evaluate_candidates
from app.state.suppression_store import SuppressionStore
from tests.conftest import SEED_NOW, _seed_index, expanded_context, requires_dataset, seed_context

pytestmark = requires_dataset

A = ActionType
R = EligibilityReasonCode

AFTER_EVERY_EXPIRY = datetime.fromisoformat("2026-12-16T00:00:00+00:00")
"""Later than every seed/expanded ``expires_at`` (max 2026-12-15)."""


def _evaluate_all(data: dict[str, Any], suppression: SuppressionStore, now: datetime) -> dict[str, tuple[EligibilityResult, ...]]:
    results = {}
    for trigger_id in sorted(data["triggers"]):
        ctx = expanded_context(data, trigger_id)
        results[trigger_id] = evaluate_candidates(generate_candidates(ctx), ctx, suppression, now=now)
    return results


def _flat(results: dict[str, tuple[EligibilityResult, ...]]) -> list[EligibilityResult]:
    return [r for batch in results.values() for r in batch]


def test_every_seed_candidate_evaluates_and_is_eligible_when_fresh() -> None:
    evaluated = 0
    for trigger_id in sorted(_seed_index()["trigger"]):
        ctx = seed_context(trigger_id)
        batch = generate_candidates(ctx)
        results = evaluate_candidates(batch, ctx, SuppressionStore(), now=SEED_NOW)

        assert [r.candidate for r in results] == batch, trigger_id
        assert all(r.eligible for r in results), (trigger_id, [r.reason_codes for r in results])
        evaluated += len(results)
    assert evaluated > 25


def test_expanded_dataset_evaluates_without_exceptions(expanded: dict[str, Any]) -> None:
    store = SuppressionStore()
    results = _evaluate_all(expanded, store, SEED_NOW)
    flat = _flat(results)

    assert len(results) == 100
    assert all(results.values())
    assert all(r.eligible for r in flat), Counter(c for r in flat for c in r.reason_codes)
    assert len(store) == 0


def test_every_kind_keeps_an_eligible_candidate(expanded: dict[str, Any]) -> None:
    results = _evaluate_all(expanded, SuppressionStore(), SEED_NOW)
    kinds = {expanded["triggers"][tid]["kind"] for tid, batch in results.items() if any(r.eligible for r in batch)}

    assert kinds == set(TRIGGER_KIND_ARCHETYPES)


def test_expanded_dataset_after_every_expiry(expanded: dict[str, Any]) -> None:
    flat = _flat(_evaluate_all(expanded, SuppressionStore(), AFTER_EVERY_EXPIRY))

    for r in flat:
        if r.candidate.action is A.NO_ACTION:
            assert r.eligible
        else:
            assert r.reason_codes == (R.TRIGGER_EXPIRED,)


def test_expanded_dataset_with_every_trigger_key_suppressed(expanded: dict[str, Any]) -> None:
    store = SuppressionStore(clock=lambda: SEED_NOW)
    for trigger_id in expanded["triggers"]:
        store.suppress(expanded_context(expanded, trigger_id).suppression_key, reason="sent")
    size = len(store)

    flat = _flat(_evaluate_all(expanded, store, SEED_NOW))

    for r in flat:
        assert r.eligible if r.candidate.action is A.NO_ACTION else r.reason_codes == (R.SUPPRESSED,)
    assert len(store) == size == 100


def test_expanded_evaluation_is_deterministic_and_does_not_mutate_inputs(expanded: dict[str, Any]) -> None:
    snapshot = copy.deepcopy(expanded)

    first = _evaluate_all(expanded, SuppressionStore(), SEED_NOW + timedelta(days=30))
    second = _evaluate_all(expanded, SuppressionStore(), SEED_NOW + timedelta(days=30))

    assert first == second
    assert expanded == snapshot


def test_all_canonical_pairs_keep_an_eligible_candidate(expanded: dict[str, Any]) -> None:
    pairs = expanded["pairs"]
    assert len(pairs) == 30

    for pair in pairs:
        ctx = expanded_context(expanded, pair["trigger_id"])
        results = evaluate_candidates(generate_candidates(ctx), ctx, SuppressionStore(), now=SEED_NOW)
        sendable = [r for r in results if r.candidate.action is not A.NO_ACTION]

        assert (ctx.merchant_id, ctx.customer_id) == (pair["merchant_id"], pair["customer_id"])
        assert any(r.eligible for r in results), pair["test_id"]
        assert not sendable or any(r.eligible for r in sendable), pair["test_id"]
