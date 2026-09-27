from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models.domain import (
    CategoryContext,
    CustomerContext,
    MerchantContext,
    TriggerContext,
    parse_context_payload,
    payload_identifier,
)
from app.models.enums import ContextScope, CustomerState, TriggerScope, TriggerSource
from tests.conftest import SAMPLE_PAYLOADS, load_seed_dataset, merchant_payload, requires_dataset, trigger_payload

MODEL_BY_SCOPE = {
    "category": CategoryContext,
    "merchant": MerchantContext,
    "customer": CustomerContext,
    "trigger": TriggerContext,
}


@requires_dataset
@pytest.mark.parametrize("scope", list(MODEL_BY_SCOPE))
def test_every_seed_record_parses(scope: str) -> None:
    items = load_seed_dataset()[scope]

    assert items
    for context_id, payload in items:
        model = parse_context_payload(ContextScope(scope), payload)
        assert isinstance(model, MODEL_BY_SCOPE[scope])
        assert payload_identifier(ContextScope(scope), payload) == context_id


@pytest.mark.parametrize("scope", list(SAMPLE_PAYLOADS))
def test_sample_payloads_parse(scope: str) -> None:
    _, make_payload = SAMPLE_PAYLOADS[scope]

    assert isinstance(parse_context_payload(ContextScope(scope), make_payload()), MODEL_BY_SCOPE[scope])


def test_merchant_fields_are_typed() -> None:
    merchant = MerchantContext.model_validate(merchant_payload())

    assert merchant.identity.owner_first_name == "Meera"
    assert merchant.identity.languages == ["en", "hi"]
    assert merchant.performance.ctr == pytest.approx(0.021)
    assert merchant.performance.delta_7d.calls_pct == pytest.approx(-0.05)
    assert merchant.offers[0].title == "Dental Cleaning @ ₹299"


def test_trigger_fields_are_typed() -> None:
    trigger = TriggerContext.model_validate(trigger_payload())

    assert trigger.scope is TriggerScope.MERCHANT
    assert trigger.source is TriggerSource.EXTERNAL
    assert trigger.expires_at == datetime(2026, 5, 3, tzinfo=UTC)
    assert trigger.payload == {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"}


def test_unknown_fields_are_preserved() -> None:
    merchant = MerchantContext.model_validate({**merchant_payload(), "review_score_2027": 4.9})

    assert merchant.model_extra == {"review_score_2027": 4.9}


def test_conversation_history_from_alias() -> None:
    merchant = MerchantContext.model_validate(
        {**merchant_payload(), "conversation_history": [{"ts": "2026-04-24T10:12:00Z", "from": "vera", "body": "hi"}]}
    )

    assert merchant.conversation_history[0].from_ == "vera"
    assert merchant.conversation_history[0].model_dump(by_alias=True)["from"] == "vera"


def test_customer_state_enum() -> None:
    customer = CustomerContext.model_validate({"customer_id": "c_1", "merchant_id": "m_1", "state": "lapsed_soft"})

    assert customer.state is CustomerState.LAPSED_SOFT


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CategoryContext, {}),
        (MerchantContext, {"merchant_id": "m_1"}),
        (CustomerContext, {"customer_id": "c_1"}),
        (TriggerContext, {"id": "t_1", "scope": "merchant"}),
        (TriggerContext, {**trigger_payload(), "expires_at": "2026-05-03T00:00:00"}),
    ],
)
def test_required_fields_and_types_enforced(model: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
