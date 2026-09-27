from datetime import timedelta

import pytest

from app.state.suppression_store import SuppressionStore
from tests.conftest import T0, FakeClock

KEY = "research:dentists:2026-W17"


@pytest.fixture
def store(clock: FakeClock) -> SuppressionStore:
    return SuppressionStore(clock)


def test_missing_key_is_not_suppressed(store: SuppressionStore) -> None:
    assert not store.is_suppressed(KEY)
    assert store.get(KEY) is None


def test_suppress_marks_key(store: SuppressionStore) -> None:
    record = store.suppress(KEY, reason="sent research digest")

    assert store.is_suppressed(KEY)
    assert record.key == KEY
    assert record.created_at == T0
    assert record.expires_at is None
    assert record.reason == "sent research digest"


def test_repeated_suppression_keeps_one_record_and_original_created_at(store: SuppressionStore, clock: FakeClock) -> None:
    store.suppress(KEY, reason="first")
    clock.advance(60)

    record = store.suppress(KEY, reason="second")

    assert len(store) == 1
    assert record.created_at == T0
    assert record.reason == "second"
    assert store.is_suppressed(KEY)


def test_clear_removes_single_key(store: SuppressionStore) -> None:
    store.suppress(KEY)
    store.suppress("other")

    assert store.clear(KEY) is True
    assert store.clear(KEY) is False
    assert not store.is_suppressed(KEY)
    assert store.is_suppressed("other")


def test_clear_all_removes_every_key(store: SuppressionStore) -> None:
    store.suppress(KEY)
    store.suppress("other")

    store.clear_all()

    assert len(store) == 0
    assert not store.is_suppressed(KEY)
    assert not store.is_suppressed("other")


def test_expiry_against_explicit_now(store: SuppressionStore) -> None:
    expires = T0 + timedelta(hours=1)
    store.suppress(KEY, expires_at=expires)

    assert store.is_suppressed(KEY, now=expires - timedelta(seconds=1))
    assert not store.is_suppressed(KEY, now=expires)
    assert len(store) == 0


def test_expiry_against_store_clock(store: SuppressionStore, clock: FakeClock) -> None:
    store.suppress(KEY, expires_at=T0 + timedelta(minutes=30))

    clock.advance(29 * 60)
    assert store.is_suppressed(KEY)
    clock.advance(60)
    assert not store.is_suppressed(KEY)


def test_resuppressing_expired_key_starts_fresh(store: SuppressionStore, clock: FakeClock) -> None:
    store.suppress(KEY, expires_at=T0 + timedelta(minutes=1))
    clock.advance(120)

    record = store.suppress(KEY)

    assert record.created_at == clock.now
    assert record.expires_at is None
    assert store.is_suppressed(KEY)


def test_empty_key_is_rejected(store: SuppressionStore) -> None:
    with pytest.raises(ValueError):
        store.suppress("")
