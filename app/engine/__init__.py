"""Decision domain (Phase 2A): decision models and pure planning primitives.

Independent of FastAPI and application state, so it can run directly against
context payloads (e.g. the canonical test pairs) without starting a server.
"""

from app.engine.actions import CUSTOMER_ACTIONS, SEND_AS_BY_SCOPE, ActionType, CTAType, DecisionScope, SendAs
from app.engine.archetypes import TriggerArchetype
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
    "is_grounded",
    "make_plan_id",
    "rank_candidates",
    "resolve_field",
    "score_candidate",
    "validate_score_dimension",
]
