import itertools
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.engine import DecisionCandidate, candidate_sort_key, rank_candidates, score_candidate, validate_score_dimension
from app.engine import scoring
from tests.conftest import CANDIDATE_FEATURES, candidate_fields

EXPIRY = datetime(2026, 5, 3, tzinfo=UTC)


def candidate(**overrides: Any) -> DecisionCandidate:
    return DecisionCandidate(**candidate_fields(**overrides))


def all_features(value: float) -> dict[str, float]:
    return dict.fromkeys(CANDIDATE_FEATURES, value)


# --------------------------------------------------------------------------- #
# Weights
# --------------------------------------------------------------------------- #


def test_weight_constants_are_exact() -> None:
    assert scoring.URGENCY_WEIGHT == 25.0
    assert scoring.TIME_PRESSURE_WEIGHT == 15.0
    assert scoring.MERCHANT_RELEVANCE_WEIGHT == 20.0
    assert scoring.CONVERSATION_RELEVANCE_WEIGHT == 15.0
    assert scoring.ACTIONABILITY_WEIGHT == 10.0
    assert scoring.EVIDENCE_STRENGTH_WEIGHT == 10.0
    assert scoring.ENGAGEMENT_WEIGHT == 5.0


def test_weights_sum_to_max_score() -> None:
    assert sum(weight for _, weight in scoring.SCORE_WEIGHTS) == scoring.MAX_SCORE == 100.0


def test_weights_cover_exactly_the_candidate_features() -> None:
    assert tuple(name for name, _ in scoring.SCORE_WEIGHTS) == CANDIDATE_FEATURES


# --------------------------------------------------------------------------- #
# score_candidate arithmetic
# --------------------------------------------------------------------------- #


def test_all_features_one_scores_100() -> None:
    assert score_candidate(candidate(**all_features(1.0))) == 100.0


def test_all_features_zero_scores_0() -> None:
    assert score_candidate(candidate(**all_features(0.0))) == 0.0


@pytest.mark.parametrize(("feature", "weight"), scoring.SCORE_WEIGHTS)
def test_single_feature_contributes_its_weight(feature: str, weight: float) -> None:
    assert score_candidate(candidate(**{**all_features(0.0), feature: 1.0})) == weight


def test_mixed_features_exact_score() -> None:
    c = candidate(
        urgency=0.8,                # 20.0
        time_pressure=0.5,          #  7.5
        merchant_relevance=0.75,    # 15.0
        conversation_relevance=0.2, #  3.0
        actionability=1.0,          # 10.0
        evidence_strength=0.3,      #  3.0
        engagement_potential=0.6,   #  3.0
    )

    assert score_candidate(c) == 61.5


@pytest.mark.parametrize(("value", "expected"), [(0.1, 10.0), (0.3, 30.0), (0.7, 70.0), (0.5, 50.0)])
def test_uniform_decimal_features_score_exactly(value: float, expected: float) -> None:
    assert score_candidate(candidate(**all_features(value))) == expected


def test_score_is_pure_and_repeatable() -> None:
    c = candidate(urgency=0.9, time_pressure=0.1, merchant_relevance=0.33)

    scores = {score_candidate(c) for _ in range(100)}

    assert len(scores) == 1
    assert score_candidate(candidate(urgency=0.9, time_pressure=0.1, merchant_relevance=0.33)) in scores


def test_score_ignores_non_feature_fields() -> None:
    base = candidate()
    other = candidate(
        trigger_id="trg_zzz",
        merchant_id="m_999",
        objective="other",
        action="draft_post",
        suppression_key="other:key",
        selected_offer_id="o_1",
        expires_at=EXPIRY,
    )

    assert score_candidate(base) == score_candidate(other) == 50.0


def test_score_rejects_invalid_features_that_bypassed_validation() -> None:
    corrupt = DecisionCandidate.model_construct(**{**candidate_fields(), "urgency": 1.7})

    with pytest.raises(ValueError, match="urgency"):
        score_candidate(corrupt)


# --------------------------------------------------------------------------- #
# validate_score_dimension
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (1, 1.0), (0.0, 0.0), (1.0, 1.0), (0.42, 0.42)])
def test_validate_score_dimension_accepts(value: float, expected: float) -> None:
    result = validate_score_dimension(value)

    assert result == expected
    assert type(result) is float


@pytest.mark.parametrize("value", [-0.0001, 1.0001, 1.7, -1, float("nan"), float("inf"), float("-inf")])
def test_validate_score_dimension_rejects_out_of_range(value: float) -> None:
    with pytest.raises(ValueError):
        validate_score_dimension(value)


@pytest.mark.parametrize("value", [True, False, "0.5", None, [0.5]])
def test_validate_score_dimension_rejects_non_numbers(value: Any) -> None:
    with pytest.raises(TypeError):
        validate_score_dimension(value)


def test_validate_score_dimension_names_the_feature() -> None:
    with pytest.raises(ValueError, match="time_pressure"):
        validate_score_dimension(2.0, "time_pressure")


# --------------------------------------------------------------------------- #
# Ranking / tie-breaking
# --------------------------------------------------------------------------- #


def best(*candidates: DecisionCandidate) -> DecisionCandidate:
    return rank_candidates(candidates)[0]


def test_higher_score_wins_over_higher_urgency() -> None:
    high_score = candidate(urgency=0.6, **{f: 1.0 for f in CANDIDATE_FEATURES if f != "urgency"})
    high_urgency = candidate(urgency=1.0)

    assert score_candidate(high_score) > score_candidate(high_urgency)
    assert best(high_urgency, high_score) is high_score


def test_same_score_higher_urgency_wins() -> None:
    urgent = candidate(trigger_id="trg_b", urgency=1.0, merchant_relevance=0.5)
    relevant = candidate(trigger_id="trg_a", urgency=0.8, merchant_relevance=0.75)

    assert score_candidate(urgent) == score_candidate(relevant)
    assert best(relevant, urgent) is urgent


def test_same_urgency_stronger_evidence_wins() -> None:
    strong = candidate(trigger_id="trg_b", evidence_strength=1.0, actionability=0.5)
    actionable = candidate(trigger_id="trg_a", evidence_strength=0.5, actionability=1.0)

    assert score_candidate(strong) == score_candidate(actionable)
    assert best(actionable, strong) is strong


def test_same_evidence_stronger_conversation_relevance_wins() -> None:
    conversational = candidate(trigger_id="trg_b", conversation_relevance=1.0, time_pressure=0.5)
    pressing = candidate(trigger_id="trg_a", conversation_relevance=0.5, time_pressure=1.0)

    assert score_candidate(conversational) == score_candidate(pressing)
    assert best(pressing, conversational) is conversational


def test_same_everything_earlier_expiry_wins() -> None:
    sooner = candidate(trigger_id="trg_b", expires_at=EXPIRY)
    later = candidate(trigger_id="trg_a", expires_at=EXPIRY + timedelta(hours=1))

    assert best(later, sooner) is sooner


def test_expiry_beats_no_expiry() -> None:
    expiring = candidate(trigger_id="trg_b", expires_at=datetime(2099, 1, 1, tzinfo=UTC))
    open_ended = candidate(trigger_id="trg_a", expires_at=None)

    assert best(open_ended, expiring) is expiring


def test_expiry_comparison_respects_timezones() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    utc_10 = candidate(trigger_id="trg_a", expires_at=datetime(2026, 5, 3, 10, 0, tzinfo=UTC))
    ist_1500 = candidate(trigger_id="trg_b", expires_at=datetime(2026, 5, 3, 15, 0, tzinfo=ist))  # 09:30 UTC

    assert best(utc_10, ist_1500) is ist_1500


def test_expiry_ordering_does_not_depend_on_current_time() -> None:
    long_expired = candidate(trigger_id="trg_b", expires_at=datetime(2001, 1, 1, tzinfo=UTC))
    future = candidate(trigger_id="trg_a", expires_at=datetime(2099, 1, 1, tzinfo=UTC))

    assert best(future, long_expired) is long_expired


@pytest.mark.parametrize("expires_at", [None, EXPIRY])
def test_no_expiry_difference_lexical_trigger_id_wins(expires_at: datetime | None) -> None:
    a = candidate(trigger_id="trg_a", expires_at=expires_at)
    b = candidate(trigger_id="trg_b", expires_at=expires_at)

    assert best(b, a) is a


def test_same_trigger_ordering_is_total() -> None:
    draft = candidate(action="draft_post")
    insight = candidate(action="send_insight")

    assert candidate_sort_key(draft) != candidate_sort_key(insight)
    assert rank_candidates([insight, draft]) == rank_candidates([draft, insight]) == [draft, insight]


def test_ranking_is_independent_of_input_order() -> None:
    pool = [
        candidate(trigger_id="trg_c", urgency=0.9),
        candidate(trigger_id="trg_a"),
        candidate(trigger_id="trg_b"),
        candidate(trigger_id="trg_d", expires_at=EXPIRY),
        candidate(trigger_id="trg_e", evidence_strength=0.9, actionability=0.1),
        candidate(trigger_id="trg_f", urgency=0.1),
    ]
    expected = ["trg_c", "trg_e", "trg_d", "trg_a", "trg_b", "trg_f"]

    for permutation in itertools.permutations(pool):
        assert [c.trigger_id for c in rank_candidates(permutation)] == expected


def test_sort_key_is_deterministic() -> None:
    c = candidate(expires_at=EXPIRY, customer_id="c_1", action="ask_merchant")

    assert candidate_sort_key(c) == candidate_sort_key(candidate(expires_at=EXPIRY, customer_id="c_1", action="ask_merchant"))
