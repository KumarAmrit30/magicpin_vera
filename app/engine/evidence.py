"""Evidence: a fact taken from supplied context that supports a decision.

Evidence must point at a real field in a real input (a context payload or the
live conversation). :func:`is_grounded` checks that the recorded value is
actually present at that path, which is how fabricated evidence is caught.
"""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.engine.scoring import UnitInterval

FIELD_PATH_PATTERN = r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$"


class EvidenceSource(StrEnum):
    """Which supplied input a piece of evidence was read from."""

    CATEGORY = "category"
    MERCHANT = "merchant"
    CUSTOMER = "customer"
    TRIGGER = "trigger"
    CONVERSATION = "conversation"


class Evidence(BaseModel):
    """A single grounded fact.

    ``field`` is a dotted path into the source (list indices are numeric
    segments, e.g. ``offers.0.title``). ``formatted`` is the human-readable
    rendering Phase 3 may quote; it must describe ``value``, not embellish it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EvidenceSource
    field: Annotated[str, Field(pattern=FIELD_PATH_PATTERN)]
    value: JsonValue
    formatted: Annotated[str, Field(min_length=1)]
    importance: UnitInterval


def resolve_field(data: Any, path: str) -> Any:
    """Follow a dotted ``path`` through nested mappings/sequences.

    Raises:
        KeyError: if any segment does not exist.
    """
    current = data
    for segment in path.split("."):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, str) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise KeyError(path)
    return current


def is_grounded(evidence: Evidence, source_data: Any) -> bool:
    """True if ``evidence.value`` equals the value at ``evidence.field`` in ``source_data``."""
    try:
        actual = resolve_field(source_data, evidence.field)
    except KeyError:
        return False
    return type(actual) is type(evidence.value) and actual == evidence.value
