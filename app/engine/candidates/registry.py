"""Archetype -> generator registry and the candidate-generation entry point."""

import logging
from collections.abc import Mapping
from types import MappingProxyType

from app.engine.archetypes import TriggerArchetype, classify_trigger
from app.engine.candidates import competitive, customer, intent, operations, opportunity, performance, safety
from app.engine.candidates.base import CandidateGenerator
from app.engine.candidates.context import CandidateGenerationContext
from app.engine.plans import DecisionCandidate

logger = logging.getLogger(__name__)

GENERATORS: Mapping[TriggerArchetype, CandidateGenerator] = MappingProxyType(
    {
        generator.archetype: generator
        for generator in (
            safety.GENERATOR,
            intent.GENERATOR,
            customer.GENERATOR,
            performance.GENERATOR,
            opportunity.GENERATOR,
            competitive.GENERATOR,
            operations.GENERATOR,
        )
    }
)


def generate_candidates(
    context: CandidateGenerationContext,
    generators: Mapping[TriggerArchetype, CandidateGenerator] = GENERATORS,
) -> list[DecisionCandidate]:
    """Grounded candidates for the context's trigger, in a stable (unranked) order.

    Unmapped trigger kinds yield no candidates. Any candidate without evidence,
    or whose evidence does not match the context, is dropped.
    """
    archetype = classify_trigger(context.trigger)
    if archetype is None:
        logger.info("No archetype for trigger kind %r (trigger %s); no candidates", context.kind, context.trigger_id)
        return []

    candidates: list[DecisionCandidate] = []
    for candidate in generators[archetype].generate(context):
        if candidate.archetype is not archetype or candidate.trigger_id != context.trigger_id:
            logger.warning("Dropping %s candidate that does not belong to trigger %s", candidate.action, context.trigger_id)
            continue
        if not candidate.evidence or not all(context.is_grounded(item) for item in candidate.evidence):
            logger.warning("Dropping ungrounded %s candidate for trigger %s", candidate.action, context.trigger_id)
            continue
        candidates.append(candidate)
    return candidates
