"""Escaped, self-contained HTML rendering for the offline dashboard."""

# ruff: noqa: E501

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from codex_insights.analytics.dashboard import DashboardData
from codex_insights.visual_assets import load_visual_stylesheet


def render_dashboard(data: DashboardData) -> str:
    """Render a network-free dashboard without embedding private content."""

    payload = data.to_dict()
    overview = _mapping(payload["overview"])
    filters = _mapping(payload["filters"])
    quality = _mapping(payload["data_quality"])
    tools = _mapping(payload["tools"])
    tasks = _mapping(payload["tasks"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Codex Insights · Offline dashboard</title>
<style>
{load_visual_stylesheet()}
</style>
</head>
<body class="ci-dashboard"><main class="ci-shell">
<header class="ci-page-header"><div><div class="eyebrow">Local analytics instrument</div><h1>Codex Insights</h1>
<p class="muted">A privacy-aware, read-only view of normalized local Codex telemetry.</p>
<div class="filters">{_filter_chips(filters)}</div></div>
<div class="stamp">dashboard { _escape(payload['schema_version']) }<br>
version {_escape(payload['application_version'])}<br>{_escape(payload['generated_at'])}<br>
timezone {_escape(payload['timezone'])}</div></header>
{_overview_section(_mapping(payload['overview_views']), overview, quality)}
{_activity_section(_sequence(payload['activity']))}
{_repository_section(_sequence(payload['repositories']))}
{_model_section(_sequence(payload['models']))}
{_tools_section(tools)}
{_tasks_section(tasks)}
{_outcomes_section(_mapping(payload['outcomes']))}
{_git_section(_mapping(payload['git']))}
{_interesting_section(_sequence(payload['interesting_sessions']))}
{_quality_section(quality)}
<section><div class="section-head"><h2>Methodology</h2></div><p class="method">
Additive totals use reconciled local token contributions. Daily and weekly token activity assigns
nonnegative cumulative increments to token-event time; unprovable timing remains explicit. Session
medians and p90 values use observed per-rollout cumulative totals. Prompt, command, task, Git, and outcome metrics preserve
origin and confidence; missing or ambiguous evidence remains explicit. This document contains no
rollout transcript, prompt body, command text, tool output, hidden reasoning, remote asset,
analytics beacon, or tracking code. Local token telemetry is not guaranteed to equal server-side
billing, quota, or UI accounting.</p></section>
</main></body></html>
"""


def _filter_chips(filters: Mapping[str, object]) -> str:
    labels = {
        "since": "since",
        "until": "until",
        "repository": "repo",
        "model": "model",
        "task_action": "task",
        "task_domain": "domain",
    }
    active = [
        f'<span class="filter">{_escape(labels[key])}: {_escape(value)}</span>'
        for key, value in filters.items()
        if value is not None
    ]
    return "".join(active) if active else '<span class="filter">all indexed activity</span>'


def _overview_section(
    views: Mapping[str, object],
    overall: Mapping[str, object],
    quality: Mapping[str, object],
) -> str:
    rendered_views = "".join(
        f'<div class="overview-view view-{_escape(key)}">'
        f'<div class="muted">{_escape(_mapping(value).get("label"))}</div>'
        f'<div class="primary">{_primary_metrics(_mapping(value))}</div></div>'
        for key, value in (("daily", views.get("daily")), ("weekly", views.get("weekly")), ("overall", views.get("overall")))
    )
    compact = "".join(
        f'<span>{_escape(label)} <strong>{_escape(_number(_mapping(views.get(key)).get("reconciled_tokens")))}</strong></span>'
        for key, label in (("daily", "Today"), ("weekly", "This week"), ("overall", "All time"))
    )
    return (
        '<div class="overview-switch" role="radiogroup" aria-label="Overview period">'
        '<input class="control-input" type="radio" name="overview" id="overview-daily">'
        '<input class="control-input" type="radio" name="overview" id="overview-weekly">'
        '<input class="control-input" type="radio" name="overview" id="overview-overall" checked>'
        '<div class="controls">'
        '<label class="control-label" for="overview-daily">Daily</label>'
        '<label class="control-label" for="overview-weekly">Weekly</label>'
        '<label class="control-label" for="overview-overall">Overall</label></div>'
        f'<div class="overview-views">{rendered_views}</div>'
        f'<div class="secondary">{compact}</div>'
        f'<div class="secondary">{_secondary_metrics(overall, quality)}</div></div>'
    )


def _primary_metrics(overview: Mapping[str, object]) -> str:
    coverage = _mapping(overview.get("token_coverage"))
    rows = (
        ("Indexed sessions", _integer(overview.get("sessions"))),
        ("Session/token activity days", _integer(overview.get("active_days"))),
        ("Repositories", _integer(overview.get("repositories"))),
        ("Reconciled tokens", _number(overview.get("reconciled_tokens"))),
        ("Token coverage", _fraction(coverage.get("fraction"))),
    )
    return "".join(
        '<div class="metric"><div class="metric-label">'
        + _escape(label)
        + '</div><div class="metric-value">'
        + _escape(value)
        + "</div></div>"
        for label, value in rows
    )


def _secondary_metrics(
    overview: Mapping[str, object], quality: Mapping[str, object]
) -> str:
    values = (
        ("Observed median/session", _number(overview.get("observed_median_tokens_per_session"))),
        ("Observed p90/session", _number(overview.get("observed_p90_tokens_per_session"))),
        ("Confirmed commits", _integer(overview.get("high_confidence_commits"))),
        (
            "Strongly evidenced outcomes",
            _fraction(overview.get("strongly_evidenced_outcome_rate")),
        ),
        ("Child threads", _integer(quality.get("child_threads"))),
    )
    return "".join(
        f"<span>{_escape(label)} <strong>{_escape(value)}</strong></span>"
        for label, value in values
    )


def _activity_section(rows: list[dict[str, object]]) -> str:
    views = "".join(
        f'<div class="activity-order sort-{criterion}">{_activity_order(rows, criterion)}</div>'
        for criterion in ("date", "sessions", "tokens")
    )
    return _section(
        "Activity",
        "Tokens use event time; sessions use logical-session start time.",
        '<div class="activity-sort" role="radiogroup" aria-label="Activity sort">'
        '<input class="control-input" type="radio" name="activity-sort" id="sort-date" checked>'
        '<input class="control-input" type="radio" name="activity-sort" id="sort-sessions">'
        '<input class="control-input" type="radio" name="activity-sort" id="sort-tokens">'
        '<div class="controls">'
        '<span class="muted">Sort</span>'
        '<label class="control-label" for="sort-date">Date</label>'
        '<label class="control-label" for="sort-sessions">Sessions</label>'
        '<label class="control-label" for="sort-tokens">Tokens</label></div>'
        f'<div class="activity-orders">{views}</div></div>',
    )


def _activity_order(rows: list[dict[str, object]], criterion: str) -> str:
    ordered = sorted(rows, key=lambda row: _activity_sort_key(row, criterion))
    sessions = [
        (_label(row), _integer_value(_nested(row, "metrics", "session_count")))
        for row in ordered
    ]
    tokens = [
        (_label(row), _integer_value(_nested(row, "metrics", "total_tokens")))
        for row in ordered
    ]
    table_rows = tuple(
        (
            _label(row),
            _integer(_nested(row, "metrics", "session_count")),
            _number(_nested(row, "metrics", "total_tokens")),
            _coverage(_nested(row, "metrics", "coverage")),
        )
        for row in ordered
    )
    return (
        '<div class="grid-2"><div class="panel"><h3>Sessions started per day</h3>'
        + _bar_rows(sessions, alternative=False)
        + '</div><div class="panel"><h3>Reconciled tokens by event day</h3>'
        + _bar_rows(tokens, alternative=True)
        + "</div></div>"
        + _table(
            ("Day", "Sessions", "Reconciled tokens", "Coverage"),
            table_rows,
            caption=f"Daily activity sorted by {criterion}",
        )
    )


def _activity_sort_key(row: Mapping[str, object], criterion: str) -> tuple[object, ...]:
    label = _label(row)
    date_key = (label == "Unattributed time", label)
    sessions = _integer_value(_nested(row, "metrics", "session_count"))
    token_value = _nested(row, "metrics", "total_tokens")
    tokens = _integer_value(token_value)
    if criterion == "sessions":
        return (-sessions, *date_key)
    if criterion == "tokens":
        return (token_value is None, -tokens, *date_key)
    return date_key


def _repository_section(rows: list[dict[str, object]]) -> str:
    table_rows = tuple(
        (
            _short(row.get("label"), 52),
            _integer(_nested(row, "metrics", "session_count")),
            _number(_nested(row, "metrics", "total_tokens")),
            _integer(row.get("originated_commands")),
            _integer(row.get("high_confidence_commits")),
            _short(row.get("dominant_task"), 24),
            _distribution(row.get("outcomes")),
        )
        for row in rows
    )
    return _section(
        "Repository activity",
        "Attribution uses normalized repository identities; outside-Git sessions remain visible.",
        _table(
            ("Repository", "Sessions", "Tokens", "Commands", "HIGH commits", "Task", "Outcomes"),
            table_rows,
            caption="Repository activity",
        ),
    )


def _model_section(rows: list[dict[str, object]]) -> str:
    table_rows = tuple(
        (
            _short(row.get("label"), 42),
            _integer(_nested(row, "metrics", "session_count")),
            _number(_nested(row, "metrics", "total_tokens")),
            _number(_nested(row, "metrics", "median_tokens_per_session")),
            _number(_nested(row, "metrics", "p90_tokens_per_session")),
            _integer(row.get("originated_commands")),
        )
        for row in rows
    )
    return _section(
        "Model activity",
        "This is usage telemetry, not a model-quality leaderboard.",
        _table(
            ("Model", "Sessions", "Tokens", "Observed median", "Observed p90", "Commands"),
            table_rows,
            caption="Model activity",
        ),
    )


def _tools_section(tools: Mapping[str, object]) -> str:
    categories = [
        (str(row.get("key", "unknown")), _integer_value(row.get("count")))
        for row in _sequence(tools.get("categories"))
    ]
    executables = [
        (str(row.get("key", "unknown")), _integer_value(row.get("count")))
        for row in _sequence(tools.get("executables"))
    ]
    repeated_rows = tuple(
        (
            _short(row.get("category"), 28),
            _short(row.get("executable") or "unknown", 28),
            _integer(row.get("invocation_count")),
            _integer(row.get("session_count")),
        )
        for row in _sequence(tools.get("repeated_patterns"))
    )
    summary = (
        f'<div class="secondary"><span>Originated tools <strong>{_integer(tools.get("originated_tool_calls"))}</strong></span>'
        f'<span>Originated commands <strong>{_integer(tools.get("originated_commands"))}</strong></span>'
        f'<span>Tests <strong>{_integer(tools.get("test_invocations"))}</strong></span>'
        f'<span>Git inspection <strong>{_integer(tools.get("git_inspections"))}</strong></span>'
        f'<span>Patch/edit <strong>{_integer(tools.get("patch_edits"))}</strong></span></div>'
    )
    return _section(
        "Tool and command activity",
        "Only originated normalized metadata is shown; repeated patterns are descriptive.",
        summary
        + '<div class="grid-2"><div class="panel"><h3>Command categories</h3>'
        + _bar_rows(categories, alternative=False)
        + '</div><div class="panel"><h3>Executables</h3>'
        + _bar_rows(executables, alternative=True)
        + "</div></div>"
        + _table(
            ("Repeated category", "Executable", "Invocations", "Sessions"),
            repeated_rows,
            caption="Repeated command patterns",
        ),
    )


def _tasks_section(tasks: Mapping[str, object]) -> str:
    actions = _task_rows(_mapping(tasks.get("actions")))
    domains = _task_rows(_mapping(tasks.get("domains")))
    return _section(
        "Task taxonomy",
        "UNKNOWN remains part of the denominator; actions and domains are independent views.",
        '<div class="grid-2"><div class="panel"><h3>Actions</h3>'
        + _table(
            ("Action", "Sessions", "Tokens"),
            actions,
            caption="Task actions",
            panel=False,
        )
        + '</div><div class="panel"><h3>Domains</h3>'
        + _table(
            ("Domain", "Sessions", "Tokens"),
            domains,
            caption="Task domains",
            panel=False,
        )
        + "</div></div>",
    )


def _task_rows(report: Mapping[str, object]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            _short(row.get("key"), 34),
            _integer(_nested(row, "metrics", "session_count")),
            _number(_nested(row, "metrics", "reconciled_tokens")),
        )
        for row in _sequence(report.get("groups"))
    )


def _outcomes_section(outcomes: Mapping[str, object]) -> str:
    outcome_rows = tuple(
        (str(key), _integer(value))
        for key, value in sorted(_mapping(outcomes.get("outcomes")).items())
    )
    confidence_rows = tuple(
        (str(key), _integer(value))
        for key, value in sorted(_mapping(outcomes.get("confidence")).items())
    )
    body = (
        '<div class="grid-2"><div class="panel"><h3>Outcome</h3>'
        + _table(
            ("Class", "Sessions"),
            outcome_rows,
            caption="Task outcomes",
            panel=False,
        )
        + '</div><div class="panel"><h3>Confidence</h3>'
        + _table(
            ("Tier", "Sessions"),
            confidence_rows,
            caption="Outcome confidence",
            panel=False,
        )
        + "</div></div>"
        + f'<div class="secondary"><span>Strongly evidenced <strong>{_integer(outcomes.get("strongly_evidenced_count"))}</strong></span>'
        f'<span>Broad non-UNKNOWN <strong>{_integer(outcomes.get("classifiable_count"))}</strong></span>'
        f'<span>Unknown <strong>{_integer(outcomes.get("unknown_count"))}</strong></span></div>'
    )
    return _section(
        "Outcomes",
        "Heuristic classes use originated evidence; UNKNOWN is never treated as failure or success.",
        body,
    )


def _git_section(git: Mapping[str, object]) -> str:
    rows = (
        ("HIGH confirmed", _integer(git.get("high"))),
        ("MEDIUM candidate", _integer(git.get("medium"))),
        ("LOW candidate", _integer(git.get("low"))),
        ("Ambiguous", _integer(git.get("ambiguous"))),
    )
    return _section(
        "Git provenance",
        "Only HIGH is confirmed; MEDIUM and LOW remain candidates.",
        _table(("Confidence", "Associations"), rows, caption="Git association confidence")
        + f'<div class="secondary"><span>Confirmed commits <strong>{_integer(git.get("high_confidence_commits"))}</strong></span>'
        f'<span>Timing candidates omitted <strong>{_integer(git.get("timing_candidates_omitted"))}</strong></span>'
        f'<span>Sessions with confirmed commits <strong>{_integer(git.get("sessions_with_high_confidence_commits"))}</strong></span>'
        f'<span>Resolved repositories <strong>{_integer(git.get("repositories_resolved"))}</strong></span></div>',
    )


def _interesting_section(rows: list[dict[str, object]]) -> str:
    table_rows = tuple(
        (
            _short(row.get("reason"), 34),
            _short(row.get("session_id"), 18),
            _short(row.get("repository"), 40),
            _short(row.get("model") or "unknown", 28),
            _number(row.get("observed_rollout_tokens")),
            _duration(row.get("duration_seconds")),
        )
        for row in rows
    )
    return _section(
        "Interesting sessions",
        "IDs and normalized metadata only; no prompt or transcript content is included.",
        _table(
            ("Reason", "Session", "Repository", "Model", "Observed tokens", "Duration"),
            table_rows,
            caption="Interesting sessions",
        ),
    )


def _quality_section(quality: Mapping[str, object]) -> str:
    compatibility = _mapping(quality.get("compatibility"))
    warnings = _mapping(compatibility.get("warnings"))
    event = _mapping(quality.get("event_provenance"))
    token_coverage = _mapping(quality.get("token_coverage"))
    temporal = _mapping(quality.get("temporal_attribution"))
    items = (
        ("Token coverage", _fraction(token_coverage.get("fraction"))),
        ("Event-time attribution", _fraction(temporal.get("attributed_fraction"))),
        ("Temporally unattributed tokens", _number(temporal.get("unattributed_total_tokens"))),
        ("Temporal fallback sessions", _integer(temporal.get("fallback_sessions"))),
        ("Replayed tokens removed", _number(quality.get("reconciled_replay_tokens"))),
        ("Child reconciliation", _fraction(quality.get("child_reconciliation_coverage"))),
        ("Ambiguous lineage threads", _integer(quality.get("ambiguous_lineage_threads"))),
        ("Originated event observations", _integer(event.get("originated"))),
        ("Logical prompts", _integer(quality.get("logical_prompts"))),
        ("Prompt storage", _enabled(quality.get("prompt_storage_enabled"))),
        ("Command-text storage", _enabled(quality.get("command_text_storage_enabled"))),
        ("Unknown outcomes", _integer(quality.get("unknown_outcomes"))),
        ("Unknown tasks", _integer(quality.get("unknown_tasks"))),
        ("Parser warnings", _distribution(warnings)),
        ("Stale sessions", _integer(compatibility.get("stale_sessions"))),
    )
    body = '<dl class="quality">' + "".join(
        f'<div class="quality-item"><dt>{_escape(label)}</dt><dd>{_escape(value)}</dd></div>'
        for label, value in items
    ) + "</dl>"
    return _section(
        "Data quality and methodology",
        "Coverage and ambiguity are first-class results, not hidden zeros.",
        body,
    )


def _section(title: str, note: str, body: str) -> str:
    return (
        f'<section><div class="section-head"><h2>{_escape(title)}</h2>'
        f'<span class="muted">{_escape(note)}</span></div>{body}</section>'
    )


def _bar_rows(rows: Sequence[tuple[str, int]], *, alternative: bool) -> str:
    if not rows:
        return '<div class="empty">No matching activity.</div>'
    maximum = max((value for _, value in rows), default=0)
    bar_class = "bar alt" if alternative else "bar"
    return "".join(
        '<div class="bar-row"><div class="bar-label" title="'
        + _escape(label)
        + '">'
        + _escape(label)
        + '</div><div class="track"><div class="'
        + bar_class
        + '" style="width:'
        + f"{(value / maximum * 100) if maximum else 0:.3f}%"
        + '"></div></div><div class="bar-value">'
        + _escape(_number(value))
        + "</div></div>"
        for label, value in rows
    )


def _table(
    headers: tuple[str, ...],
    rows: Iterable[tuple[str, ...]],
    *,
    caption: str,
    panel: bool = True,
) -> str:
    materialized = tuple(rows)
    if not materialized:
        content = '<div class="empty">No matching data.</div>'
    else:
        head = "".join(f'<th scope="col">{_escape(value)}</th>' for value in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_escape(value)}</td>" for value in row) + "</tr>"
            for row in materialized
        )
        content = (
            f'<table><caption class="sr-only">{_escape(caption)}</caption>'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )
    return f'<div class="panel">{content}</div>' if panel else content


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _nested(row: Mapping[str, object], *keys: str) -> object:
    current: object = row
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _label(row: Mapping[str, object]) -> str:
    return str(row.get("label") or row.get("key") or "unknown")


def _number(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def _integer(value: object) -> str:
    return f"{_integer_value(value):,}"


def _integer_value(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _fraction(value: object) -> str:
    return f"{float(value):.1%}" if isinstance(value, (int, float)) else "unknown"


def _coverage(value: object) -> str:
    coverage = _mapping(value)
    return f"{_integer(coverage.get('total_tokens'))}/{_integer(coverage.get('session_count'))}"


def _distribution(value: object) -> str:
    mapping = _mapping(value)
    return ", ".join(f"{key}:{_integer(item)}" for key, item in sorted(mapping.items())) or "none"


def _duration(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _enabled(value: object) -> str:
    return "enabled" if value is True else "disabled" if value is False else "unknown"


def _short(value: object, limit: int) -> str:
    text = "unknown" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
