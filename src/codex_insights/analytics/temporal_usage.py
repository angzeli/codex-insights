"""Deterministic event-time attribution for normalized token telemetry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from codex_insights.models import UsageVector

VECTOR_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class StoredTokenEvent:
    """One content-free token event loaded from the analytics database."""

    source_ordinal: int
    occurred_at: datetime | None
    cumulative: UsageVector | None
    delta: UsageVector | None


@dataclass(frozen=True, slots=True)
class TemporalContribution:
    """One nonnegative usage increment with an optional event timestamp."""

    occurred_at: datetime | None
    usage: UsageVector


@dataclass(frozen=True, slots=True)
class TemporalAttribution:
    """Explainable temporal attribution for one reconciled session contribution."""

    contributions: tuple[TemporalContribution, ...]
    status: str
    reason: str | None = None

    @property
    def attributed_usage(self) -> UsageVector:
        return sum_vectors(
            item.usage for item in self.contributions if item.occurred_at is not None
        )

    @property
    def unattributed_usage(self) -> UsageVector:
        return sum_vectors(
            item.usage for item in self.contributions if item.occurred_at is None
        )


def attribute_session_usage(
    *,
    semantics: str,
    target: UsageVector,
    inherited_baseline: UsageVector | None,
    events: tuple[StoredTokenEvent, ...],
) -> TemporalAttribution:
    """Attribute one already-reconciled session total without guessing missing evidence."""

    if not _has_known_value(target):
        return TemporalAttribution((), "unavailable", "token_usage_unavailable")
    if not events:
        return _fallback(target, "token_events_unavailable")
    if semantics == "cumulative_total":
        return _attribute_cumulative(target, inherited_baseline, events)
    if semantics == "summed_event_deltas":
        return _attribute_deltas(target, events)
    return _fallback(target, "unsupported_usage_semantics")


def sum_vectors(vectors: Iterable[UsageVector]) -> UsageVector:
    """Sum known components while preserving entirely unknown fields."""

    items = tuple(vectors)
    values: dict[str, int | None] = {}
    for field in VECTOR_FIELDS:
        known = [getattr(item, field) for item in items if getattr(item, field) is not None]
        values[field] = sum(known) if known else None
    return UsageVector(**values)


def _attribute_cumulative(
    target: UsageVector,
    baseline: UsageVector | None,
    events: tuple[StoredTokenEvent, ...],
) -> TemporalAttribution:
    snapshots = tuple(event for event in events if event.cumulative is not None)
    if not snapshots:
        return _fallback(target, "cumulative_snapshots_unavailable")
    if _timestamps_regress(snapshots):
        return _fallback(target, "nonmonotonic_event_timestamps")

    previous_raw: UsageVector | None = None
    previous_adjusted = UsageVector(**{field: 0 for field in VECTOR_FIELDS})
    contributions: list[TemporalContribution] = []
    for event in snapshots:
        current = event.cumulative
        assert current is not None
        if previous_raw is not None and _vector_decreases(current, previous_raw):
            return _fallback(target, "nonmonotonic_cumulative_usage")
        adjusted = _remove_baseline(current, baseline, target)
        increment = _subtract(adjusted, previous_adjusted)
        if increment is None:
            return _fallback(target, "nonmonotonic_adjusted_usage")
        if _has_positive_value(increment):
            contributions.append(TemporalContribution(event.occurred_at, increment))
        previous_raw = current
        previous_adjusted = adjusted

    if not _matches_target(sum_vectors(item.usage for item in contributions), target):
        return _fallback(target, "event_total_mismatch")
    complete = all(item.occurred_at is not None for item in contributions)
    return TemporalAttribution(
        tuple(contributions),
        "complete" if complete else "partial",
        None if complete else "missing_event_timestamp",
    )


def _attribute_deltas(
    target: UsageVector,
    events: tuple[StoredTokenEvent, ...],
) -> TemporalAttribution:
    deltas = tuple(event for event in events if event.delta is not None)
    if not deltas:
        return _fallback(target, "delta_events_unavailable")
    if _timestamps_regress(deltas):
        return _fallback(target, "nonmonotonic_event_timestamps")
    contributions = tuple(
        TemporalContribution(event.occurred_at, _mask_to_target(event.delta, target))
        for event in deltas
        if event.delta is not None
        and _has_positive_value(_mask_to_target(event.delta, target))
    )
    if not _matches_target(sum_vectors(item.usage for item in contributions), target):
        return _fallback(target, "event_total_mismatch")
    complete = all(item.occurred_at is not None for item in contributions)
    return TemporalAttribution(
        contributions,
        "complete" if complete else "partial",
        None if complete else "missing_event_timestamp",
    )


def _remove_baseline(
    current: UsageVector,
    baseline: UsageVector | None,
    target: UsageVector,
) -> UsageVector:
    values: dict[str, int | None] = {}
    for field in VECTOR_FIELDS:
        value = getattr(current, field)
        inherited = getattr(baseline, field) if baseline is not None else 0
        values[field] = None if getattr(target, field) is None else (
            max(value - inherited, 0)
            if value is not None and inherited is not None
            else value
        )
    return UsageVector(**values)


def _mask_to_target(vector: UsageVector, target: UsageVector) -> UsageVector:
    return UsageVector(
        **{
            field: getattr(vector, field) if getattr(target, field) is not None else None
            for field in VECTOR_FIELDS
        }
    )


def _subtract(final: UsageVector, initial: UsageVector) -> UsageVector | None:
    values: dict[str, int | None] = {}
    for field in VECTOR_FIELDS:
        final_value = getattr(final, field)
        initial_value = getattr(initial, field)
        if final_value is not None and initial_value is not None:
            if final_value < initial_value:
                return None
            values[field] = final_value - initial_value
        else:
            values[field] = None
    return UsageVector(**values)


def _vector_decreases(current: UsageVector, previous: UsageVector) -> bool:
    for field in VECTOR_FIELDS:
        current_value = getattr(current, field)
        previous_value = getattr(previous, field)
        if (
            current_value is not None
            and previous_value is not None
            and current_value < previous_value
        ):
            return True
    return False


def _timestamps_regress(events: tuple[StoredTokenEvent, ...]) -> bool:
    known = [event.occurred_at for event in events if event.occurred_at is not None]
    return any(current < previous for previous, current in zip(known, known[1:], strict=False))


def _matches_target(observed: UsageVector, target: UsageVector) -> bool:
    return all(
        getattr(target, field) is None
        or getattr(observed, field) == getattr(target, field)
        for field in VECTOR_FIELDS
    )


def _has_known_value(vector: UsageVector) -> bool:
    return any(getattr(vector, field) is not None for field in VECTOR_FIELDS)


def _has_positive_value(vector: UsageVector) -> bool:
    return any((getattr(vector, field) or 0) > 0 for field in VECTOR_FIELDS)


def _fallback(target: UsageVector, reason: str) -> TemporalAttribution:
    return TemporalAttribution(
        (TemporalContribution(None, target),),
        "fallback",
        reason,
    )
