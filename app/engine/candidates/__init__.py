"""Candidate generation (Phase 2B): trigger archetype -> grounded DecisionCandidates.

One generator per archetype (``safety``, ``intent``, ``customer``,
``performance``, ``opportunity``, ``competitive``, ``operations``), dispatched
through :data:`GENERATORS`. Generators only propose; eligibility, ranking and
winner selection happen later.
"""

from app.engine.candidates.base import ArchetypeGenerator, CandidateGenerator, Proposal
from app.engine.candidates.context import CandidateGenerationContext, ConversationTurnView
from app.engine.candidates.registry import GENERATORS, generate_candidates

__all__ = [
    "GENERATORS",
    "ArchetypeGenerator",
    "CandidateGenerationContext",
    "CandidateGenerator",
    "ConversationTurnView",
    "Proposal",
    "generate_candidates",
]
