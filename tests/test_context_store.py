import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.enums import ContextPutOutcome, ContextScope
from app.models.schemas import VersionedContext
from app.state.context_store import ContextStore
from tests.conftest import FakeClock

DELIVERED = datetime(2026, 4, 26, 9, 45, tzinfo=UTC)


def ctx(
    version: int,
    payload: dict[str, Any] | None = None,
    scope: ContextScope = ContextScope.MERCHANT,
    context_id: str = "m_001",
) -> VersionedContext:
    return VersionedContext(
        scope=scope,
        context_id=context_id,
        version=version,
        payload=payload if payload is not None else {"v": version},
        delivered_at=DELIVERED,
    )


@pytest.fixture
def store(clock: FakeClock) -> ContextStore:
    return ContextStore(clock)


def test_first_insert_is_created(store: ContextStore, clock: FakeClock) -> None:
    result = store.put(ctx(1))

    assert result.outcome is ContextPutOutcome.CREATED
    assert result.accepted
    assert result.previous_version is None
    assert result.record.version == 1
    assert result.record.stored_at == clock.now
    assert result.record.delivered_at == DELIVERED


def test_get_returns_stored_record(store: ContextStore) -> None:
    store.put(ctx(1, {"name": "Dr. Meera"}))

    record = store.get(ContextScope.MERCHANT, "m_001")

    assert record is not None
    assert record.scope is ContextScope.MERCHANT
    assert record.context_id == "m_001"
    assert record.payload == {"name": "Dr. Meera"}


def test_get_missing_returns_none(store: ContextStore) -> None:
    assert store.get(ContextScope.MERCHANT, "unknown") is None
    assert not store.exists(ContextScope.MERCHANT, "unknown")


def test_get_all_filters_by_scope_and_orders_by_id(store: ContextStore) -> None:
    store.put(ctx(1, context_id="m_b"))
    store.put(ctx(1, context_id="m_a"))
    store.put(ctx(1, scope=ContextScope.CATEGORY, context_id="dentists"))

    merchants = store.get_all(ContextScope.MERCHANT)

    assert [r.context_id for r in merchants] == ["m_a", "m_b"]
    assert [r.context_id for r in store.get_all(ContextScope.CATEGORY)] == ["dentists"]
    assert store.get_all(ContextScope.TRIGGER) == []


def test_same_version_is_idempotent(store: ContextStore, clock: FakeClock) -> None:
    first = store.put(ctx(1, {"views": 2410}))
    clock.advance(60)

    repeat = store.put(ctx(1, {"views": 9999}))

    assert repeat.outcome is ContextPutOutcome.DUPLICATE
    assert repeat.accepted
    assert repeat.record == first.record
    assert store.get(ContextScope.MERCHANT, "m_001").payload == {"views": 2410}
    assert len(store) == 1


def test_higher_version_replaces(store: ContextStore, clock: FakeClock) -> None:
    store.put(ctx(1, {"views": 2410}))
    clock.advance(60)

    result = store.put(ctx(2, {"views": 2580}))

    assert result.outcome is ContextPutOutcome.REPLACED
    assert result.previous_version == 1
    record = store.get(ContextScope.MERCHANT, "m_001")
    assert record.version == 2
    assert record.payload == {"views": 2580}
    assert record.stored_at == clock.now
    assert len(store) == 1


def test_higher_version_may_skip_versions(store: ContextStore) -> None:
    store.put(ctx(1))

    assert store.put(ctx(5)).outcome is ContextPutOutcome.REPLACED
    assert store.get(ContextScope.MERCHANT, "m_001").version == 5


def test_lower_version_is_rejected(store: ContextStore) -> None:
    store.put(ctx(3, {"views": 3}))

    result = store.put(ctx(2, {"views": 2}))

    assert result.outcome is ContextPutOutcome.STALE
    assert not result.accepted
    assert result.record.version == 3
    assert store.get(ContextScope.MERCHANT, "m_001").payload == {"views": 3}


def test_version_sequence_from_spec(store: ContextStore) -> None:
    outcomes = [store.put(ctx(v)).outcome for v in (1, 1, 2, 1)]

    assert outcomes == [
        ContextPutOutcome.CREATED,
        ContextPutOutcome.DUPLICATE,
        ContextPutOutcome.REPLACED,
        ContextPutOutcome.STALE,
    ]
    assert store.get(ContextScope.MERCHANT, "m_001").version == 2


def test_same_id_in_different_scopes_is_independent(store: ContextStore) -> None:
    store.put(ctx(1, {"kind": "merchant"}, scope=ContextScope.MERCHANT, context_id="shared"))
    result = store.put(ctx(1, {"kind": "trigger"}, scope=ContextScope.TRIGGER, context_id="shared"))

    assert result.outcome is ContextPutOutcome.CREATED
    assert store.get(ContextScope.MERCHANT, "shared").payload == {"kind": "merchant"}
    assert store.get(ContextScope.TRIGGER, "shared").payload == {"kind": "trigger"}


def test_multiple_ids_version_independently(store: ContextStore) -> None:
    store.put(ctx(3, context_id="m_a"))
    result = store.put(ctx(1, context_id="m_b"))

    assert result.outcome is ContextPutOutcome.CREATED
    assert store.get(ContextScope.MERCHANT, "m_a").version == 3
    assert store.get(ContextScope.MERCHANT, "m_b").version == 1


def test_counts_include_every_scope(store: ContextStore) -> None:
    store.put(ctx(1, context_id="m_a"))
    store.put(ctx(1, context_id="m_b"))
    store.put(ctx(1, scope=ContextScope.CATEGORY, context_id="dentists"))

    assert store.counts() == {
        ContextScope.CATEGORY: 1,
        ContextScope.MERCHANT: 2,
        ContextScope.CUSTOMER: 0,
        ContextScope.TRIGGER: 0,
    }


def test_clear_removes_everything(store: ContextStore) -> None:
    store.put(ctx(1, context_id="m_a"))
    store.put(ctx(1, scope=ContextScope.CATEGORY, context_id="dentists"))

    store.clear()

    assert len(store) == 0
    assert store.get(ContextScope.MERCHANT, "m_a") is None
    assert store.put(ctx(1, context_id="m_a")).outcome is ContextPutOutcome.CREATED


def test_stored_payload_is_isolated_from_caller_mutation(store: ContextStore) -> None:
    payload = {"offers": [{"title": "Dental Cleaning @ ₹299"}]}
    store.put(ctx(1, payload))

    payload["offers"].append({"title": "mutated"})

    assert store.get(ContextScope.MERCHANT, "m_001").payload == {"offers": [{"title": "Dental Cleaning @ ₹299"}]}


def test_concurrent_puts_keep_highest_version(store: ContextStore) -> None:
    versions = list(range(1, 201))
    barrier = threading.Barrier(len(versions))

    def push(version: int) -> None:
        barrier.wait()
        store.put(ctx(version))

    threads = [threading.Thread(target=push, args=(v,)) for v in reversed(versions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    record = store.get(ContextScope.MERCHANT, "m_001")
    assert record.version == 200
    assert record.payload == {"v": 200}
