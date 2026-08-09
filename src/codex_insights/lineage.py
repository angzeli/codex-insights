"""Explainable cross-thread token lineage without raw rollout content."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from codex_insights.models import (
    DeduplicationConfidence,
    DeduplicationStatus,
    DeltaConsistency,
    NormalizedThreadRelationship,
    NormalizedTokenSnapshot,
    TokenLineageAssessment,
    UsageVector,
)

LINEAGE_ALGORITHM_VERSION = "token-lineage-v1"
_VECTOR_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class ThreadTopology:
    """Aggregate diagnostics for an explicit directed thread graph."""

    total_threads: int
    root_threads: int
    child_threads: int
    spawn_edges: int
    valid_edges: int
    maximum_depth: int
    multi_generation_trees: int
    orphan_parent_edges: int
    orphan_child_edges: int
    self_edges: int
    cycle_nodes: frozenset[str]


def assess_token_lineage(
    parent: tuple[NormalizedTokenSnapshot, ...],
    child: tuple[NormalizedTokenSnapshot, ...],
    *,
    cyclic: bool = False,
) -> TokenLineageAssessment:
    """Classify one explicit edge using only exact cumulative-vector evidence."""

    if cyclic:
        return _unaccounted(DeduplicationStatus.CYCLE, "cyclic_spawn_graph")
    if not child:
        return _unaccounted(DeduplicationStatus.UNAVAILABLE, "child_token_data_unavailable")
    if not parent:
        return _unaccounted(DeduplicationStatus.AMBIGUOUS, "parent_token_data_unavailable")

    start, matched = _best_parent_segment(parent, child)
    if matched >= 2:
        baseline = parent[start + matched - 1].cumulative
        incremental = subtract_usage(child[-1].cumulative, baseline)
        if incremental is not None:
            return TokenLineageAssessment(
                status=DeduplicationStatus.INHERITED_PREFIX,
                confidence=DeduplicationConfidence.HIGH,
                evidence_type="exact_cumulative_vector_prefix",
                matched_snapshot_count=matched,
                parent_sequence_start=start,
                inherited_baseline=baseline,
                incremental_usage=incremental,
                delta_consistency=_delta_consistency(child[matched:], incremental),
            )

    first = child[0].cumulative
    parent_final = parent[-1].cumulative
    if first == parent_final:
        incremental = subtract_usage(child[-1].cumulative, first)
        if incremental is not None:
            return TokenLineageAssessment(
                status=DeduplicationStatus.INHERITED_EXACT,
                confidence=DeduplicationConfidence.HIGH,
                evidence_type="child_initial_equals_parent_final",
                matched_snapshot_count=1,
                parent_sequence_start=len(parent) - 1,
                inherited_baseline=first,
                incremental_usage=incremental,
                delta_consistency=_delta_consistency(child[1:], incremental),
            )

    if _is_explicit_zero(first):
        return TokenLineageAssessment(
            status=DeduplicationStatus.INDEPENDENT,
            confidence=DeduplicationConfidence.EXPLICIT,
            evidence_type="explicit_zero_cumulative_baseline",
            matched_snapshot_count=1,
            inherited_baseline=first,
            incremental_usage=child[-1].cumulative,
            delta_consistency=_delta_consistency(child[1:], child[-1].cumulative),
        )

    return _unaccounted(DeduplicationStatus.AMBIGUOUS, "no_exact_ancestor_baseline")


def analyze_thread_topology(
    session_ids: set[str],
    relationships: tuple[NormalizedThreadRelationship, ...],
) -> ThreadTopology:
    """Summarize roots, depth, orphans, and cycles without recursive SQL."""

    edges = [
        (relationship.parent_source_session_id, relationship.child_source_session_id)
        for relationship in relationships
    ]
    valid = [
        (parent, child)
        for parent, child in edges
        if parent in session_ids and child in session_ids
    ]
    children = {child for _, child in valid}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for parent, child in valid:
        outgoing[parent].append(child)
    for descendants in outgoing.values():
        descendants.sort()

    cycle_nodes = _cycle_nodes(session_ids, outgoing)
    maximum_depth = 0
    multi_generation = 0
    for root in sorted(session_ids - children):
        tree_depth = _tree_depth(root, outgoing, cycle_nodes)
        maximum_depth = max(maximum_depth, tree_depth)
        if tree_depth >= 2:
            multi_generation += 1

    return ThreadTopology(
        total_threads=len(session_ids),
        root_threads=len(session_ids - children),
        child_threads=len(children),
        spawn_edges=len(edges),
        valid_edges=len(valid),
        maximum_depth=maximum_depth,
        multi_generation_trees=multi_generation,
        orphan_parent_edges=sum(parent not in session_ids for parent, _ in edges),
        orphan_child_edges=sum(child not in session_ids for _, child in edges),
        self_edges=sum(parent == child for parent, child in edges),
        cycle_nodes=frozenset(cycle_nodes),
    )


def subtract_usage(final: UsageVector, baseline: UsageVector) -> UsageVector | None:
    """Subtract known vector components, rejecting any observed cumulative decrease."""

    values: dict[str, int | None] = {}
    for field in _VECTOR_FIELDS:
        final_value = getattr(final, field)
        baseline_value = getattr(baseline, field)
        if final_value is not None and baseline_value is not None and final_value < baseline_value:
            return None
        values[field] = (
            final_value - baseline_value
            if final_value is not None and baseline_value is not None
            else None
        )
    return UsageVector(**values)


def _best_parent_segment(
    parent: tuple[NormalizedTokenSnapshot, ...],
    child: tuple[NormalizedTokenSnapshot, ...],
) -> tuple[int, int]:
    best_start = 0
    best_length = 0
    for start, parent_snapshot in enumerate(parent):
        if parent_snapshot.cumulative != child[0].cumulative:
            continue
        matched = 0
        for ancestor, descendant in zip(parent[start:], child, strict=False):
            if ancestor.cumulative != descendant.cumulative:
                break
            matched += 1
        if matched > best_length:
            best_start = start
            best_length = matched
    return best_start, best_length


def _delta_consistency(
    snapshots: tuple[NormalizedTokenSnapshot, ...],
    incremental: UsageVector,
) -> DeltaConsistency:
    if not snapshots:
        return DeltaConsistency.UNAVAILABLE
    if any(snapshot.last_turn is None for snapshot in snapshots):
        return DeltaConsistency.UNAVAILABLE
    summed: dict[str, int | None] = {}
    for field in _VECTOR_FIELDS:
        values = [
            getattr(snapshot.last_turn, field)
            for snapshot in snapshots
            if snapshot.last_turn
        ]
        known = [value for value in values if value is not None]
        summed[field] = sum(known) if known else None
    return (
        DeltaConsistency.EXACT
        if UsageVector(**summed) == incremental
        else DeltaConsistency.MISMATCH
    )


def _is_explicit_zero(vector: UsageVector) -> bool:
    return vector.total_tokens == 0 and all(
        value in (0, None) for value in (getattr(vector, field) for field in _VECTOR_FIELDS)
    )


def _unaccounted(status: DeduplicationStatus, evidence: str) -> TokenLineageAssessment:
    return TokenLineageAssessment(
        status=status,
        confidence=DeduplicationConfidence.NONE,
        evidence_type=evidence,
    )


def _cycle_nodes(session_ids: set[str], outgoing: dict[str, list[str]]) -> set[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: set[str] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for child in outgoing.get(node, []):
            if state.get(child, 0) == 0:
                visit(child)
            elif state.get(child) == 1:
                start = stack.index(child)
                cycles.update(stack[start:])
        stack.pop()
        state[node] = 2

    for session_id in sorted(session_ids):
        if state.get(session_id, 0) == 0:
            visit(session_id)
    return cycles


def _tree_depth(
    root: str,
    outgoing: dict[str, list[str]],
    cycle_nodes: set[str],
) -> int:
    maximum = 0
    stack: list[tuple[str, int, frozenset[str]]] = [(root, 0, frozenset())]
    while stack:
        node, depth, ancestors = stack.pop()
        maximum = max(maximum, depth)
        if node in ancestors or node in cycle_nodes:
            continue
        for child in outgoing.get(node, []):
            stack.append((child, depth + 1, ancestors | {node}))
    return maximum
