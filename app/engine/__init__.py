"""Decision domain: decision models, pure planning primitives (Phase 2A) and
trigger classification (Phase 2B). Candidate generation lives in
:mod:`app.engine.candidates`.

Independent of FastAPI and application state, so it can run directly against
context payloads (e.g. the canonical test pairs) without starting a server.
"""

from app.engine.actions import CUSTOMER_ACTIONS, SEND_AS_BY_SCOPE, ActionType, CTAType, DecisionScope, SendAs
from app.engine.archetypes import (
    TRIGGER_KIND_ALIASES,
    TRIGGER_KIND_ARCHETYPES,
    TriggerArchetype,
    canonical_trigger_kind,
    classify_trigger,
    classify_trigger_kind,
)
from app.engine.evidence import Evidence, EvidenceSource, is_grounded, resolve_field
from app.engine.plans import DecisionCandidate, DecisionCore, DecisionPlan, make_plan_id
from app.engine.scoring import (
    MAX_SCORE,
    SCORE_WEIGHTS,
    candidate_sort_key,
    rank_candidates,
    score_candidate,
    validate_score_dimension,
)

__all__ = [
    "CUSTOMER_ACTIONS",
    "MAX_SCORE",
    "SCORE_WEIGHTS",
    "SEND_AS_BY_SCOPE",
    "TRIGGER_KIND_ALIASES",
    "TRIGGER_KIND_ARCHETYPES",
    "ActionType",
    "CTAType",
    "DecisionCandidate",
    "DecisionCore",
    "DecisionPlan",
    "DecisionScope",
    "Evidence",
    "EvidenceSource",
    "SendAs",
    "TriggerArchetype",
    "candidate_sort_key",
    "canonical_trigger_kind",
    "classify_trigger",
    "classify_trigger_kind",
    "is_grounded",
    "make_plan_id",
    "rank_candidates",
    "resolve_field",
    "score_candidate",
    "validate_score_dimension",
]
