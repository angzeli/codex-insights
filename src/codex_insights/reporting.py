"""Privacy-safe Markdown, JSON, and self-contained HTML report renderers."""

from __future__ import annotations

import html
import json
from enum import StrEnum
from typing import Any

from codex_insights.analytics.reports import AnalyticsReport


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"
    HTML = "html"


def render_report(report: AnalyticsReport, report_format: ReportFormat) -> str:
    if report_format is ReportFormat.JSON:
        return json.dumps(report.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if report_format is ReportFormat.HTML:
        return render_html(report)
    return render_markdown(report)


def render_markdown(report: AnalyticsReport) -> str:
    data = report.to_dict()
    overview = data["overview"]
    assert isinstance(overview, dict)
    period = data["period"]
    assert isinstance(period, dict)
    lines = [
        f"# Codex Insights {report.kind.value.title()} Report",
        "",
        f"**Period:** {period['start']} to {period['end']} ({report.timezone})  ",
        f"**Generated:** {data['generated_at']}",
        "",
        "## Overview",
        "",
        _markdown_table(
            ("Metric", "Value"),
            (
                ("Sessions", _number(overview["sessions"])),
                ("Active days", _number(overview["active_days"])),
                ("Repositories", _number(overview["repositories"])),
                ("Models", _number(overview["models"])),
                ("Reconciled known tokens", _number(overview["reconciled_tokens"])),
                (
                    "Observed median tokens/session",
                    _number(overview["observed_median_tokens_per_session"]),
                ),
                (
                    "Observed p90 tokens/session",
                    _number(overview["observed_p90_tokens_per_session"]),
                ),
                ("Sessions/day", _decimal(overview["sessions_per_day"])),
            ),
        ),
        "",
        "## Activity",
        "",
        _activity_markdown(data),
        "",
        "## Repositories",
        "",
        _repository_markdown(data["repositories"]),
        "",
        "## Models",
        "",
        _model_markdown(data["models"]),
        "",
        "## Tasks",
        "",
        _tasks_markdown(data["tasks"]),
        "",
        "## Tool activity",
        "",
        _tools_markdown(data["tools"]),
        "",
        "## Git",
        "",
        _git_markdown(data["git"]),
        "",
        "## Outcomes",
        "",
        _outcomes_markdown(data["outcomes"]),
        "",
        "## Interesting sessions",
        "",
        _interesting_markdown(data["interesting_sessions"]),
        "",
        "## Data quality",
        "",
        _quality_markdown(data["data_quality"]),
        "",
        "## Previous-period comparison",
        "",
        _comparison_markdown(data["previous_period"]),
        "",
        "## Methodology",
        "",
        "- Additive totals use reconciled local token contributions.",
        "- Median and p90 use observed per-rollout session totals.",
        "- Prompts are logical and origin-aware; commands are originated events.",
        "- Git associations retain confidence tiers; outcomes retain `unknown`.",
        "- Prompt-pattern comparisons are descriptive, require both groups to have n >= 5, "
        "and do not imply causation.",
        "- Local Codex telemetry is not guaranteed to equal server-side billing or quota "
        "accounting.",
        "",
    ]
    return "\n".join(lines)


def render_html(report: AnalyticsReport) -> str:
    """Render an escaped, offline document with no scripts, CDNs, or tracking."""

    data = report.to_dict()
    period = _mapping(data["period"])
    overview = _mapping(data["overview"])
    title = f"Codex Insights {report.kind.value.title()} Report"
    activity = _sequence(data["activity"])
    interesting = _html_interesting(_sequence(data["interesting_sessions"]))
    quality_json = html.escape(
        json.dumps(data["data_quality"], indent=2, ensure_ascii=False)
    )
    previous_json = html.escape(
        json.dumps(data["previous_period"], indent=2, ensure_ascii=False)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f8fa; --card:#fff; --ink:#172033;
--muted:#596579; --accent:#3157c8; --line:#d9deea; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#11151d; --card:#1a202b;
--ink:#edf1f8; --muted:#aeb8c8; --accent:#8ca8ff; --line:#343e50; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--ink);
font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif }}
main {{ max-width:1120px; margin:auto; padding:32px 20px 64px }}
h1 {{ margin-bottom:4px }} h2 {{ margin-top:32px }} .muted {{ color:var(--muted) }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px }}
.card,section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
padding:16px }} section {{ margin-top:18px; overflow:auto }} .value {{ font-size:1.45rem;
font-weight:700 }} table {{ border-collapse:collapse; width:100% }} th,td {{ text-align:left;
padding:8px; border-bottom:1px solid var(--line); vertical-align:top }} th {{ color:var(--muted) }}
.bar-row {{ display:grid; grid-template-columns:minmax(88px,140px) 1fr auto; gap:10px;
align-items:center; margin:8px 0 }} .track {{ height:12px; background:var(--line);
border-radius:8px; overflow:hidden }} .bar {{ height:100%; background:var(--accent) }}
code {{ overflow-wrap:anywhere }}
@media(max-width:620px) {{ .bar-row {{ grid-template-columns:1fr }} }}
</style>
</head>
<body><main>
<header><h1>{html.escape(title)}</h1><p class="muted">{html.escape(str(period['start']))}
to {html.escape(str(period['end']))} · {html.escape(report.timezone)} · generated
{html.escape(str(data['generated_at']))}</p></header>
<div class="cards">{_overview_cards(overview)}</div>
<section><h2>Activity</h2>{_bar_chart(activity, label="label", value="sessions",
metric_path=("metrics", "session_count"))}{_html_activity_table(activity)}</section>
<section><h2>Repositories</h2>{_html_group_table(_sequence(data['repositories']))}</section>
<section><h2>Models</h2>{_html_group_table(_sequence(data['models']))}</section>
<section><h2>Tasks</h2>{_html_tasks(_mapping(data['tasks']))}</section>
<section><h2>Tool activity</h2>{_html_tools(_mapping(data['tools']))}</section>
<section><h2>Git</h2>{_html_git(_mapping(data['git']))}</section>
<section><h2>Outcomes</h2>{_html_outcomes(_mapping(data['outcomes']))}</section>
<section><h2>Interesting sessions</h2>{interesting}</section>
<section><h2>Data quality</h2><pre>{quality_json}</pre></section>
<section><h2>Previous-period comparison</h2><pre>{previous_json}</pre></section>
<section><h2>Methodology</h2><p>Additive totals use reconciled local contributions;
session distributions use observed rollout totals. Prompts and commands are origin-aware. Git and
outcome evidence retain confidence and unknown states. Prompt-pattern comparisons are descriptive,
not causal. Local telemetry may differ from server-side billing or quota accounting.</p></section>
</main></body></html>
"""


def _activity_markdown(data: dict[str, object]) -> str:
    key = "weekly_activity" if data["report_kind"] == "monthly" else "activity"
    rows = _sequence(data[key])
    return _markdown_table(
        ("Period", "Sessions", "Reconciled tokens", "Token coverage"),
        tuple(
            (
                str(row.get("label", "unknown")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "total_tokens")),
                _coverage(_nested(row, "metrics", "coverage")),
            )
            for row in rows
        ),
    )


def _repository_markdown(value: object) -> str:
    return _markdown_table(
        (
            "Repository",
            "Sessions",
            "Tokens",
            "Commands",
            "HIGH commits",
            "Outcomes",
            "Dominant task",
        ),
        tuple(
            (
                _short(row.get("label")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "total_tokens")),
                _number(row.get("originated_commands")),
                _number(row.get("high_confidence_commits")),
                _distribution(row.get("outcomes")),
                _short(row.get("dominant_task")),
            )
            for row in _sequence(value)[:10]
        ),
    )


def _model_markdown(value: object) -> str:
    return _markdown_table(
        ("Model", "Sessions", "Tokens", "Observed median", "Observed p90", "Commands"),
        tuple(
            (
                _short(row.get("label")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "total_tokens")),
                _number(_nested(row, "metrics", "median_tokens_per_session")),
                _number(_nested(row, "metrics", "p90_tokens_per_session")),
                _number(row.get("originated_commands")),
            )
            for row in _sequence(value)[:10]
        ),
    )


def _tasks_markdown(value: object) -> str:
    tasks = _mapping(value)
    parts: list[str] = []
    for label, key in (("Actions", "actions"), ("Domains", "domains")):
        report = _mapping(tasks.get(key))
        groups = _sequence(report.get("groups"))
        parts.extend(
            (
                f"### {label}",
                "",
                _markdown_table(
                    ("Group", "Sessions", "Reconciled tokens", "Commands"),
                    tuple(
                        (
                            _short(row.get("key")),
                            _number(_nested(row, "metrics", "session_count")),
                            _number(_nested(row, "metrics", "reconciled_tokens")),
                            _number(_nested(row, "metrics", "originated_commands")),
                        )
                        for row in groups[:10]
                    ),
                ),
                "",
            )
        )
    return "\n".join(parts).rstrip()


def _tools_markdown(value: object) -> str:
    tools = _mapping(value)
    rows = (
        ("Originated tool calls", _number(tools.get("originated_tool_calls"))),
        ("Originated commands", _number(tools.get("originated_commands"))),
        ("Validation/test invocations", _number(tools.get("test_invocations"))),
        ("Git inspections", _number(tools.get("git_inspections"))),
        ("Patch/edit calls", _number(tools.get("patch_edits"))),
        ("Known-result failure rate", _percent(tools.get("failure_rate"))),
        ("Repeated command groups", _number(len(_sequence(tools.get("repeated_commands"))))),
    )
    categories = _sequence(tools.get("command_categories"))
    category_table = _markdown_table(
        ("Command category", "Originated events"),
        tuple((_short(row.get("key")), _number(row.get("count"))) for row in categories),
    )
    return _markdown_table(("Metric", "Value"), rows) + "\n\n" + category_table


def _git_markdown(value: object) -> str:
    git = _mapping(value)
    return _markdown_table(
        ("Metric", "Value"),
        (
            ("HIGH associations", _number(git.get("high"))),
            ("MEDIUM associations", _number(git.get("medium"))),
            ("LOW associations", _number(git.get("low"))),
            ("Ambiguous associations", _number(git.get("ambiguous"))),
            ("HIGH-confidence commits", _number(git.get("high_confidence_commits"))),
            (
                "Reconciled tokens/HIGH commit (descriptive)",
                _number(git.get("reconciled_tokens_per_confirmed_commit")),
            ),
        ),
    )


def _outcomes_markdown(value: object) -> str:
    outcomes = _mapping(value)
    distribution = _mapping(outcomes.get("outcomes"))
    return _markdown_table(
        ("Outcome", "Sessions", "Fraction of classifiable"),
        tuple(
            (
                _short(key),
                _number(_mapping(item).get("count")),
                _percent(_mapping(item).get("fraction_of_classifiable")),
            )
            for key, item in sorted(distribution.items())
        ),
    )


def _interesting_markdown(value: object) -> str:
    rows = _sequence(value)
    return _markdown_table(
        ("Reason", "Session", "Repository", "Observed tokens", "Duration (s)"),
        tuple(
            (
                _short(row.get("reason")),
                _short(row.get("session_id")),
                _short(row.get("repository")),
                _number(row.get("observed_rollout_tokens")),
                _number(row.get("duration_seconds")),
            )
            for row in rows
        ),
    )


def _quality_markdown(value: object) -> str:
    quality = _mapping(value)
    token = _mapping(quality.get("token_coverage"))
    provenance = _mapping(quality.get("tool_event_provenance"))
    git = _mapping(quality.get("git_attribution"))
    return _markdown_table(
        ("Coverage / uncertainty", "Value"),
        (
            ("Token records", f"{token.get('sessions_with_data', 0)}/{token.get('sessions', 0)}"),
            ("Child threads", _number(quality.get("child_threads"))),
            (
                "Token reconciliation coverage",
                _percent(quality.get("token_reconciliation_coverage")),
            ),
            ("Ambiguous lineage threads", _number(quality.get("ambiguous_lineage_threads"))),
            ("Originated tool-event fraction", _percent(provenance.get("originated_fraction"))),
            ("Logical prompts", _number(quality.get("logical_prompts"))),
            ("Prompt-feature sessions", _number(quality.get("prompt_feature_sessions"))),
            ("HIGH-confidence commits", _number(git.get("high_confidence_commits"))),
            ("UNKNOWN outcomes", _number(quality.get("unknown_outcomes"))),
            ("UNKNOWN tasks", _number(quality.get("unknown_tasks"))),
        ),
    )


def _comparison_markdown(value: object) -> str:
    comparison = _mapping(value)
    metrics = _mapping(comparison.get("metrics"))
    warning = comparison.get("warning")
    prefix = f"> {warning}\n\n" if warning else ""
    return prefix + _markdown_table(
        ("Metric", "Current", "Previous", "Change", "% change"),
        tuple(
            (
                _short(key),
                _number(_mapping(item).get("current")),
                _number(_mapping(item).get("previous")),
                _signed(_mapping(item).get("change")),
                _percent(_mapping(item).get("percentage_change"), signed=True),
            )
            for key, item in metrics.items()
        ),
    )


def _markdown_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    safe_headers = tuple(_cell(value) for value in headers)
    lines = [
        "| " + " | ".join(safe_headers) + " |",
        "| " + " | ".join("---" for _ in safe_headers) + " |",
    ]
    if rows:
        lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    else:
        lines.append("| No data |" + " |" * (len(headers) - 1))
    return "\n".join(lines)


def _overview_cards(overview: dict[str, object]) -> str:
    values = (
        ("Sessions", overview.get("sessions")),
        ("Active days", overview.get("active_days")),
        ("Repositories", overview.get("repositories")),
        ("Reconciled tokens", overview.get("reconciled_tokens")),
        ("Observed median/session", overview.get("observed_median_tokens_per_session")),
        ("Observed p90/session", overview.get("observed_p90_tokens_per_session")),
    )
    return "".join(
        f'<div class="card"><div class="muted">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(_number(value))}</div></div>'
        for label, value in values
    )


def _bar_chart(
    rows: list[dict[str, object]],
    *,
    label: str,
    value: str,
    metric_path: tuple[str, str],
) -> str:
    del value
    values = [_integer(_nested(row, *metric_path)) for row in rows]
    maximum = max(values, default=0)
    if maximum == 0:
        return '<p class="muted">No activity in this period.</p>'
    parts = ['<div role="img" aria-label="Sessions by period">']
    for row, count in zip(rows, values, strict=True):
        width = count / maximum * 100
        parts.append(
            '<div class="bar-row"><span>'
            + html.escape(str(row.get(label, "unknown")))
            + '</span><span class="track"><span class="bar" style="display:block;width:'
            + f"{width:.2f}%"
            + '"></span></span><span>'
            + html.escape(_number(count))
            + "</span></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _html_activity_table(rows: list[dict[str, object]]) -> str:
    return _html_table(
        ("Period", "Sessions", "Reconciled tokens"),
        tuple(
            (
                str(row.get("label", "unknown")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "total_tokens")),
            )
            for row in rows
        ),
    )


def _html_group_table(rows: list[dict[str, object]]) -> str:
    return _html_table(
        ("Group", "Sessions", "Reconciled tokens", "Originated commands"),
        tuple(
            (
                _short(row.get("label")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "total_tokens")),
                _number(row.get("originated_commands")),
            )
            for row in rows[:10]
        ),
    )


def _html_tasks(tasks: dict[str, object]) -> str:
    actions = _sequence(_mapping(tasks.get("actions")).get("groups"))
    domains = _sequence(_mapping(tasks.get("domains")).get("groups"))
    return (
        "<h3>Actions</h3>"
        + _html_task_groups(actions)
        + "<h3>Domains</h3>"
        + _html_task_groups(domains)
    )


def _html_task_groups(rows: list[dict[str, object]]) -> str:
    return _html_table(
        ("Group", "Sessions", "Reconciled tokens"),
        tuple(
            (
                _short(row.get("key")),
                _number(_nested(row, "metrics", "session_count")),
                _number(_nested(row, "metrics", "reconciled_tokens")),
            )
            for row in rows[:10]
        ),
    )


def _html_tools(tools: dict[str, object]) -> str:
    return _html_table(
        ("Metric", "Value"),
        (
            ("Originated tool calls", _number(tools.get("originated_tool_calls"))),
            ("Originated commands", _number(tools.get("originated_commands"))),
            ("Test invocations", _number(tools.get("test_invocations"))),
            ("Git inspections", _number(tools.get("git_inspections"))),
            ("Known-result failure rate", _percent(tools.get("failure_rate"))),
        ),
    )


def _html_git(git: dict[str, object]) -> str:
    return _html_table(
        ("Confidence", "Associations"),
        tuple((level.upper(), _number(git.get(level))) for level in ("high", "medium", "low")),
    )


def _html_outcomes(outcomes: dict[str, object]) -> str:
    distribution = _mapping(outcomes.get("outcomes"))
    return _html_table(
        ("Outcome", "Sessions"),
        tuple(
            (_short(key), _number(_mapping(value).get("count")))
            for key, value in sorted(distribution.items())
        ),
    )


def _html_interesting(rows: list[dict[str, object]]) -> str:
    return _html_table(
        ("Reason", "Session", "Repository", "Observed tokens"),
        tuple(
            (
                _short(row.get("reason")),
                _short(row.get("session_id")),
                _short(row.get("repository")),
                _number(row.get("observed_rollout_tokens")),
            )
            for row in rows
        ),
    )


def _html_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">No data</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _nested(row: dict[str, object], *keys: str) -> object:
    current: object = row
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _coverage(value: object) -> str:
    coverage = _mapping(value)
    return f"{coverage.get('total_tokens', 0)}/{coverage.get('session_count', 0)}"


def _distribution(value: object) -> str:
    distribution = _mapping(value)
    return ", ".join(f"{key}={count}" for key, count in sorted(distribution.items())) or "none"


def _short(value: object, limit: int = 72) -> str:
    text = "unknown" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return f"{value:,.0f}"
    return str(value)


def _integer(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _decimal(value: object) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "unknown"


def _signed(value: object) -> str:
    return f"{value:+,.0f}" if isinstance(value, (int, float)) else "unknown"


def _percent(value: object, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"{value:+.1%}" if signed else f"{value:.1%}"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
