"""Decision candidates and decision plans.

A :class:`DecisionCandidate` is a possible action with normalized scoring
features, before ranking. A :class:`DecisionPlan` is the chosen decision handed
to the Phase 3 composer. Neither contains message text.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.engine.actions import SEND_AS_BY_SCOPE, ActionType, CTAType, DecisionScope, SendAs
from app.engine.archetypes import TriggerArchetype
from app.engine.evidence import Evidence
from app.engine.scoring import MAX_SCORE, UnitInterval

NonEmptyStr = Annotated[str, Field(min_length=1)]
PriorityScore = Annotated[float, Field(strict=True, ge=0.0, le=MAX_SCORE, allow_inf_nan=False)]

PLAN_ID_PREFIX = "plan_"
PLAN_ID_HEX_CHARS = 20


def make_plan_id(
    *,
    merchant_id: str,
    customer_id: str | None,
    trigger_id: str,
    objective: str,
    action: ActionType | str,
    suppression_key: str,
) -> str:
    """Stable id derived only from the decision's identity fields.

    The fields are hashed as a JSON array, so values containing separators
    cannot collide (``("a|b", "c")`` and ``("a", "b|c")`` hash differently).
    """
    identity = [merchant_id, customer_id, trigger_id, objective, str(action), suppression_key]
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{PLAN_ID_PREFIX}{digest[:PLAN_ID_HEX_CHARS]}"


class DecisionCore(BaseModel):
    """Fields shared by candidates and plans, plus target-consistency rules.

    * ``scope=customer`` requires ``customer_id``.
    * Customer-facing actions (``send_customer_*``) require ``scope=customer``.
    * ``scope=customer`` only allows customer-facing actions or ``no_action``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_id: NonEmptyStr
    archetype: TriggerArchetype
    scope: DecisionScope
    merchant_id: NonEmptyStr
    customer_id: NonEmptyStr | None = None
    objective: NonEmptyStr
    action: ActionType
    evidence: tuple[Evidence, ...] = ()
    selected_offer_id: NonEmptyStr | None = None
    suppression_key: NonEmptyStr
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_target_consistency(self) -> Self:
        is_customer_scope = self.scope is DecisionScope.CUSTOMER
        if is_customer_scope and self.customer_id is None:
            raise ValueError("scope=customer requires customer_id")
        if self.action.targets_customer and not is_customer_scope:
            raise ValueError(f"action={self.action} requires scope=customer")
        if is_customer_scope and not (self.action.targets_customer or self.action is ActionType.NO_ACTION):
            raise ValueError(f"action={self.action} cannot target a customer")
        return self


class DecisionCandidate(DecisionCore):
    """A possible action before ranking. The features below are inputs to scoring, not the score."""

    urgency: UnitInterval
    time_pressure: UnitInterval
    merchant_relevance: UnitInterval
    conversation_relevance: UnitInterval
    actionability: UnitInterval
    evidence_strength: UnitInterval
    engagement_potential: UnitInterval


class DecisionPlan(DecisionCore):
    """The decided action for one trigger, ready for Phase 3 composition.

    ``plan_id`` is computed with :func:`make_plan_id` when omitted and verified
    when supplied. ``send_as`` must match the scope (``vera`` for merchant,
    ``merchant_on_behalf`` for customer). A ``no_action`` plan carries no CTA.
    ``confidence`` is deterministic decision certainty, not a probability.
    """

    plan_id: NonEmptyStr
    language_style: NonEmptyStr | None = None
    tone_profile: NonEmptyStr | None = None
    cta_type: CTAType
    send_as: SendAs
    priority_score: PriorityScore
    confidence: UnitInterval
    rationale_facts: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _default_plan_id(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or "plan_id" in data:
            return data
        try:
            plan_id = make_plan_id(
                merchant_id=data["merchant_id"],
                customer_id=data.get("customer_id"),
                trigger_id=data["trigger_id"],
                objective=data["objective"],
                action=data["action"],
                suppression_key=data["suppression_key"],
            )
        except (KeyError, TypeError):
            return data
        return {**data, "plan_id": plan_id}

    @model_validator(mode="after")
    def _check_plan_consistency(self) -> Self:
        expected_id = make_plan_id(
            merchant_id=self.merchant_id,
            customer_id=self.customer_id,
            trigger_id=self.trigger_id,
            objective=self.objective,
            action=self.action,
            suppression_key=self.suppression_key,
        )
        if self.plan_id != expected_id:
            raise ValueError("plan_id does not match the decision identity; derive it with make_plan_id()")
        if self.send_as is not SEND_AS_BY_SCOPE[self.scope]:
            raise ValueError(f"send_as={self.send_as} is inconsistent with scope={self.scope}")
        if self.action is ActionType.NO_ACTION and self.cta_type is not CTAType.NONE:
            raise ValueError("action=no_action requires cta_type=none")
        return self

    @property
    def is_no_action(self) -> bool:
        """True when the decision is to send nothing."""
        return self.action is ActionType.NO_ACTION
