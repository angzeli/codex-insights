"""Conservative origin attribution for semantic rollout event observations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from codex_insights.models import (
    EventFamily,
    EventProvenanceStatus,
    NormalizedEventObservation,
)

PROVENANCE_ALGORITHM_VERSION = "event-provenance-v1"


@dataclass(frozen=True, slots=True)
class EventProvenanceDecision:
    """Origin decision for one child observation."""

    child_index: int
    status: EventProvenanceStatus
    confidence: str
    evidence_type: str
    parent_index: int | None = None


@dataclass(frozen=True, slots=True)
class EventFamilyAssessment:
    """Privacy-safe replay summary for one event family on one edge."""

    family: EventFamily
    observed_child_events: int
    originated_events: int
    inherited_events: int
    ambiguous_events: int
    unknown_events: int
    status: EventProvenanceStatus
    evidence_type: str


@dataclass(frozen=True, slots=True)
class EventProvenanceAssessment:
    """Ordered event decisions and family summaries for one explicit edge."""

    decisions: tuple[EventProvenanceDecision, ...]
    families: tuple[EventFamilyAssessment, ...]
    global_prefix_length: int = 0
    global_parent_start: int | None = None


def assess_event_provenance(
    parent: tuple[NormalizedEventObservation, ...],
    child: tuple[NormalizedEventObservation, ...],
    *,
    cyclic: bool = False,
) -> EventProvenanceAssessment:
    """Attribute only exact ordered replay; leave weak overlap ambiguous."""

    if not child:
        return EventProvenanceAssessment(decisions=(), families=())
    if cyclic:
        cycle_decisions = tuple(
            EventProvenanceDecision(
                child_index=index,
                status=EventProvenanceStatus.AMBIGUOUS,
                confidence="none",
                evidence_type="cyclic_spawn_graph",
            )
            for index in range(len(child))
        )
        return _assessment(child, cycle_decisions)
    if not parent:
        unknown_decisions = tuple(
            EventProvenanceDecision(
                child_index=index,
                status=EventProvenanceStatus.UNKNOWN,
                confidence="none",
                evidence_type="parent_event_data_unavailable",
            )
            for index in range(len(child))
        )
        return _assessment(child, unknown_decisions)

    resolved: list[EventProvenanceDecision | None] = [None] * len(child)
    global_start, global_length = _best_segment(parent, child)
    if global_length >= 2:
        for child_index in range(global_length):
            resolved[child_index] = EventProvenanceDecision(
                child_index=child_index,
                status=EventProvenanceStatus.INHERITED_PREFIX,
                confidence="high",
                evidence_type="exact_ordered_cross_family_prefix",
                parent_index=global_start + child_index,
            )

    for family in EventFamily:
        parent_indices = [index for index, event in enumerate(parent) if event.family is family]
        child_indices = [index for index, event in enumerate(child) if event.family is family]
        if not parent_indices or not child_indices:
            continue
        family_parent = tuple(parent[index] for index in parent_indices)
        family_child = tuple(child[index] for index in child_indices)
        family_start, family_length = _best_segment(family_parent, family_child)
        if family_length < 2:
            continue
        for family_child_index in range(family_length):
            child_index = child_indices[family_child_index]
            if resolved[child_index] is not None:
                continue
            resolved[child_index] = EventProvenanceDecision(
                child_index=child_index,
                status=EventProvenanceStatus.INHERITED_PREFIX,
                confidence="high",
                evidence_type="exact_ordered_family_prefix",
                parent_index=parent_indices[family_start + family_child_index],
            )

    parent_keys: dict[tuple[EventFamily, str, str], list[int]] = {}
    for parent_index, event in enumerate(parent):
        if event.stable_id_digest is None:
            continue
        key = (event.family, event.fingerprint, event.stable_id_digest)
        parent_keys.setdefault(key, []).append(parent_index)
    for child_index, event in enumerate(child):
        if resolved[child_index] is not None or event.stable_id_digest is None:
            continue
        candidates = parent_keys.get((event.family, event.fingerprint, event.stable_id_digest), [])
        if len(candidates) == 1:
            resolved[child_index] = EventProvenanceDecision(
                child_index=child_index,
                status=EventProvenanceStatus.INHERITED_EXACT,
                confidence="high",
                evidence_type="exact_fingerprint_and_stable_source_id",
                parent_index=candidates[0],
            )

    parent_fingerprints = {(event.family, event.fingerprint) for event in parent}
    parent_stable_ids: dict[tuple[EventFamily, str], set[str | None]] = {}
    for event in parent:
        parent_stable_ids.setdefault((event.family, event.fingerprint), set()).add(
            event.stable_id_digest
        )
    final: list[EventProvenanceDecision] = []
    for child_index, event in enumerate(child):
        decision = resolved[child_index]
        if decision is not None:
            final.append(decision)
        elif (
            (event.family, event.fingerprint) in parent_fingerprints
            and event.stable_id_digest is not None
            and None not in parent_stable_ids[(event.family, event.fingerprint)]
            and event.stable_id_digest
            not in parent_stable_ids[(event.family, event.fingerprint)]
        ):
            final.append(
                EventProvenanceDecision(
                    child_index=child_index,
                    status=EventProvenanceStatus.ORIGIN,
                    confidence="high",
                    evidence_type="distinct_stable_source_id",
                )
            )
        elif (event.family, event.fingerprint) in parent_fingerprints:
            final.append(
                EventProvenanceDecision(
                    child_index=child_index,
                    status=EventProvenanceStatus.AMBIGUOUS,
                    confidence="none",
                    evidence_type="unordered_or_single_fingerprint_overlap",
                )
            )
        else:
            final.append(
                EventProvenanceDecision(
                    child_index=child_index,
                    status=EventProvenanceStatus.ORIGIN,
                    confidence="high",
                    evidence_type="not_observed_in_explicit_parent",
                )
            )
    return _assessment(
        child,
        tuple(final),
        global_prefix_length=global_length,
        global_parent_start=global_start if global_length else None,
    )


def mirrored_user_observations(
    events: tuple[NormalizedEventObservation, ...],
) -> dict[int, int]:
    """Map adjacent wrapper duplicates to the explicit user-message record."""

    duplicates: dict[int, int] = {}
    for index in range(len(events) - 1):
        first = events[index]
        second = events[index + 1]
        if (
            first.family is EventFamily.USER_MESSAGE
            and second.family is EventFamily.USER_MESSAGE
            and second.source_ordinal == first.source_ordinal + 1
            and first.fingerprint == second.fingerprint
            and first.source_payload_type == "message"
            and second.source_payload_type == "user_message"
        ):
            duplicates[index] = index + 1
    return duplicates


def _assessment(
    child: tuple[NormalizedEventObservation, ...],
    decisions: tuple[EventProvenanceDecision, ...],
    *,
    global_prefix_length: int = 0,
    global_parent_start: int | None = None,
) -> EventProvenanceAssessment:
    by_family: dict[EventFamily, Counter[EventProvenanceStatus]] = {}
    for event, decision in zip(child, decisions, strict=True):
        by_family.setdefault(event.family, Counter())[decision.status] += 1
    families: list[EventFamilyAssessment] = []
    for family in EventFamily:
        counts = by_family.get(family)
        if counts is None:
            continue
        inherited = (
            counts[EventProvenanceStatus.INHERITED_EXACT]
            + counts[EventProvenanceStatus.INHERITED_PREFIX]
        )
        if inherited:
            status = EventProvenanceStatus.INHERITED_PREFIX
            evidence = "one_or_more_confident_inherited_events"
        elif counts[EventProvenanceStatus.AMBIGUOUS]:
            status = EventProvenanceStatus.AMBIGUOUS
            evidence = "overlap_without_sufficient_ordering_evidence"
        elif counts[EventProvenanceStatus.UNKNOWN]:
            status = EventProvenanceStatus.UNKNOWN
            evidence = "event_data_unavailable"
        else:
            status = EventProvenanceStatus.ORIGIN
            evidence = "no_parent_replay_detected"
        families.append(
            EventFamilyAssessment(
                family=family,
                observed_child_events=sum(counts.values()),
                originated_events=counts[EventProvenanceStatus.ORIGIN],
                inherited_events=inherited,
                ambiguous_events=counts[EventProvenanceStatus.AMBIGUOUS],
                unknown_events=counts[EventProvenanceStatus.UNKNOWN],
                status=status,
                evidence_type=evidence,
            )
        )
    return EventProvenanceAssessment(
        decisions=decisions,
        families=tuple(families),
        global_prefix_length=global_prefix_length,
        global_parent_start=global_parent_start,
    )


def _best_segment(
    parent: tuple[NormalizedEventObservation, ...],
    child: tuple[NormalizedEventObservation, ...],
) -> tuple[int, int]:
    best_start = 0
    best_length = 0
    for start, parent_event in enumerate(parent):
        if not _same_event(parent_event, child[0]):
            continue
        matched = 0
        for ancestor, descendant in zip(parent[start:], child, strict=False):
            if not _same_event(ancestor, descendant):
                break
            matched += 1
        if matched > best_length:
            best_start = start
            best_length = matched
    return best_start, best_length


def _same_event(
    left: NormalizedEventObservation,
    right: NormalizedEventObservation,
) -> bool:
    return left.family is right.family and left.fingerprint == right.fingerprint
