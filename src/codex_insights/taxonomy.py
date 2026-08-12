"""Deterministic, explainable task taxonomy over origin-aware evidence."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

TASK_TAXONOMY_VERSION = "task-taxonomy-v1"


class TaskAction(StrEnum):
    IMPLEMENTATION = "implementation"
    BUG_FIX = "bug_fix"
    REFACTOR = "refactor"
    CODE_REVIEW = "code_review"
    REPOSITORY_ASSESSMENT = "repository_assessment"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    UI_WORK = "ui_work"
    SCIENTIFIC_STATUS_OR_DIAGNOSIS = "scientific_status_or_diagnosis"
    RESEARCH_OR_EXPLORATION = "research_or_exploration"
    GIT_OR_RELEASE = "git_or_release"
    PLANNING = "planning"
    QUESTION_ANSWERING = "question_answering"
    OTHER = "other"
    UNKNOWN = "unknown"


class TaskDomain(StrEnum):
    SCIENTIFIC_COMPUTING = "scientific_computing"
    SOFTWARE_ENGINEERING = "software_engineering"
    DEVELOPER_TOOLING = "developer_tooling"
    DOCUMENTATION = "documentation"
    DATA_ANALYSIS = "data_analysis"
    UI = "ui"
    GIT_RELEASE = "git_release"
    GENERAL = "general"
    UNKNOWN = "unknown"


class TaskConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    """Normalized inputs; prompts must already be logical origin-aware content."""

    prompts: tuple[str, ...] = ()
    repository_name: str | None = None
    command_categories: tuple[str, ...] = ()
    high_confidence_commit: bool = False


@dataclass(frozen=True, slots=True)
class TaskClassification:
    action: TaskAction
    domain: TaskDomain
    facets: tuple[str, ...]
    confidence: TaskConfidence
    evidence: tuple[str, ...]
    taxonomy_version: str = TASK_TAXONOMY_VERSION


_ACTION_RULES: tuple[tuple[TaskAction, str, re.Pattern[str]], ...] = (
    (
        TaskAction.CODE_REVIEW,
        "action_code_review",
        re.compile(r"(?i)\b(code review|review|audit)\b|審查|审查|評審|评审"),
    ),
    (
        TaskAction.BUG_FIX,
        "action_bug_fix",
        re.compile(
            r"(?i)\b(fix|debug|bug|error|failure|failing|broken)\b|"
            r"修復|修复|調試|调试|報錯|报错"
        ),
    ),
    (
        TaskAction.REFACTOR,
        "action_refactor",
        re.compile(r"(?i)\b(refactor|restructure|cleanup)\b|重構|重构"),
    ),
    (
        TaskAction.IMPLEMENTATION,
        "action_implementation",
        re.compile(
            r"(?i)\b(implement|build|create|add|develop|scaffold)\b|"
            r"實現|实现|添加|創建|创建|構建|构建"
        ),
    ),
    (
        TaskAction.REPOSITORY_ASSESSMENT,
        "action_repository_assessment",
        re.compile(
            r"(?i)\b(repository|repo)\b.{0,30}\b(assess|inspect|status|health)\b|"
            r"倉庫.{0,20}(檢查|检查|評估|评估)"
        ),
    ),
    (
        TaskAction.TESTING,
        "action_testing",
        re.compile(r"(?i)\b(test|pytest|validation|validate)\b|測試|测试|驗證|验证"),
    ),
    (
        TaskAction.DOCUMENTATION,
        "action_documentation",
        re.compile(
            r"(?i)\b(documentation|docs|readme|docstring|write up)\b|"
            r"文檔|文档|說明"
        ),
    ),
    (
        TaskAction.UI_WORK,
        "action_ui",
        re.compile(
            r"(?i)\b(ui|user interface|dashboard|layout|css|frontend)\b|"
            r"界面|介面|儀表板|仪表板"
        ),
    ),
    (
        TaskAction.SCIENTIFIC_STATUS_OR_DIAGNOSIS,
        "action_scientific_diagnosis",
        re.compile(
            r"(?i)\b(orca|vasp|cp2k|multiwfn|ase)\b.{0,40}"
            r"\b(status|diagnos|converg|calculation|job)\b|"
            r"計算.{0,20}(狀態|诊断|診斷)"
        ),
    ),
    (
        TaskAction.RESEARCH_OR_EXPLORATION,
        "action_research",
        re.compile(
            r"(?i)\b(research|investigate|explore|literature|compare approaches)\b|"
            r"研究|調研|调研|探索|文獻|文献"
        ),
    ),
    (
        TaskAction.GIT_OR_RELEASE,
        "action_git_release",
        re.compile(
            r"(?i)\b(git|commit|release|tag|pull request|\bpr\b)\b|"
            r"提交|發布|发布|版本"
        ),
    ),
    (
        TaskAction.PLANNING,
        "action_planning",
        re.compile(
            r"(?i)\b(plan|design|architecture|roadmap)\b|"
            r"計劃|计划|設計|设计|架構|架构"
        ),
    ),
    (
        TaskAction.QUESTION_ANSWERING,
        "action_question",
        re.compile(
            r"(?i)^\s*(what|why|how|where|when|can you explain)\b|"
            r"^\s*(什麼|什么|為什麼|为什么|如何|怎麼|怎么)"
        ),
    ),
)

_DOMAIN_RULES: tuple[tuple[TaskDomain, str, re.Pattern[str]], ...] = (
    (
        TaskDomain.SCIENTIFIC_COMPUTING,
        "domain_scientific",
        re.compile(
            r"(?i)\b(orca|vasp|cp2k|multiwfn|ase|quantum chemistry|dft|xps)\b|"
            r"量子化學|量子化学|科學計算|科学计算"
        ),
    ),
    (
        TaskDomain.UI,
        "domain_ui",
        re.compile(
            r"(?i)\b(ui|frontend|dashboard|css|swiftui|streamlit)\b|"
            r"界面|介面|儀表板|仪表板"
        ),
    ),
    (
        TaskDomain.GIT_RELEASE,
        "domain_git",
        re.compile(r"(?i)\b(git|commit|release|tag|pull request)\b|提交|發布|发布"),
    ),
    (
        TaskDomain.DOCUMENTATION,
        "domain_documentation",
        re.compile(
            r"(?i)\b(documentation|readme|manuscript|docs|docstring)\b|"
            r"文檔|文档|論文|论文"
        ),
    ),
    (
        TaskDomain.DATA_ANALYSIS,
        "domain_data_analysis",
        re.compile(
            r"(?i)\b(data analysis|pandas|numpy|plot|statistics|notebook)\b|"
            r"數據分析|数据分析|統計|统计"
        ),
    ),
    (
        TaskDomain.DEVELOPER_TOOLING,
        "domain_developer_tooling",
        re.compile(
            r"(?i)\b(cli|codex|pytest|ruff|mypy|tooling|observability)\b|"
            r"開發工具|开发工具"
        ),
    ),
    (
        TaskDomain.SOFTWARE_ENGINEERING,
        "domain_software",
        re.compile(
            r"(?i)\b(code|python|repository|api|database|test|bug|implementation)\b|"
            r"代碼|代码|軟件|软件|程式|程序"
        ),
    ),
)

_FACET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ORCA", re.compile(r"(?i)\borca\b")),
    ("VASP", re.compile(r"(?i)\bvasp\b")),
    ("CP2K", re.compile(r"(?i)\bcp2k\b")),
    ("ASE", re.compile(r"(?i)\base\b")),
    ("Multiwfn", re.compile(r"(?i)\bmultiwfn\b")),
    ("XPS", re.compile(r"(?i)\bxps\b")),
    ("workflow", re.compile(r"(?i)\b(workflow|orchestration|pipeline)\b|工作流|流程")),
)


def classify_task(evidence: TaskEvidence) -> TaskClassification:
    """Classify action/domain with prompt intent taking precedence over activity."""

    prompt_scores: Counter[TaskAction] = Counter()
    domain_scores: Counter[TaskDomain] = Counter()
    matched: list[str] = []
    intent_prompts = tuple(_intent_text(prompt) for prompt in evidence.prompts)
    combined = "\n".join(intent_prompts)
    for ordinal, prompt in enumerate(intent_prompts):
        weight = 3 if ordinal == 0 else 2
        for action, rule_id, pattern in _ACTION_RULES:
            match_count = sum(1 for _ in pattern.finditer(prompt))
            if match_count:
                prompt_scores[action] += weight * min(match_count, 3)
                matched.append(rule_id)
        for domain, rule_id, pattern in _DOMAIN_RULES:
            match_count = sum(1 for _ in pattern.finditer(prompt))
            if match_count:
                domain_scores[domain] += weight * min(match_count, 3)
                matched.append(rule_id)

    action = _best_action(prompt_scores)
    if action is TaskAction.UNKNOWN:
        action, fallback = _activity_action(evidence)
        if fallback:
            matched.append(fallback)
    domain = _best_domain(domain_scores)
    if domain is TaskDomain.UNKNOWN:
        domain, fallback = _activity_domain(evidence)
        if fallback:
            matched.append(fallback)
    facets = tuple(label for label, pattern in _FACET_RULES if pattern.search(combined))
    max_score = max(prompt_scores.values(), default=0)
    confidence = (
        TaskConfidence.HIGH
        if max_score >= 3
        else TaskConfidence.MEDIUM
        if action is not TaskAction.UNKNOWN or domain is not TaskDomain.UNKNOWN
        else TaskConfidence.LOW
    )
    return TaskClassification(
        action=action,
        domain=domain,
        facets=facets,
        confidence=confidence,
        evidence=tuple(dict.fromkeys(matched)) or ("insufficient_origin_intent",),
    )


def reconcile_task_taxonomy(
    connection: sqlite3.Connection,
    session_ids: set[int] | None = None,
) -> None:
    """Persist idempotent classifications from logical prompts and originated activity."""

    if session_ids is not None and not session_ids:
        return
    where = ""
    parameters: tuple[int, ...] = ()
    if session_ids is not None:
        placeholders = ",".join("?" for _ in session_ids)
        where = f"WHERE id IN ({placeholders})"
        parameters = tuple(sorted(session_ids))
    sessions = connection.execute(
        f"SELECT id, repository_name FROM source_sessions {where} ORDER BY id",
        parameters,
    ).fetchall()
    for session in sessions:
        session_id = int(session["id"])
        prompts = tuple(
            str(row["text"])
            for row in connection.execute(
                "SELECT text FROM prompts WHERE origin_session_id = ? ORDER BY prompt_ordinal",
                (session_id,),
            )
        )
        categories = tuple(
            str(row["command_category"])
            for row in connection.execute(
                """
                SELECT command_category FROM tool_activity
                WHERE observed_session_id = ? AND origin_session_id = ?
                  AND provenance_status = 'origin'
                ORDER BY source_ordinal, operation_ordinal
                """,
                (session_id, session_id),
            )
        )
        high_commit = bool(
            connection.execute(
                "SELECT 1 FROM session_commit_associations "
                "WHERE session_id = ? AND confidence = 'high' LIMIT 1",
                (session_id,),
            ).fetchone()
        )
        classification = classify_task(
            TaskEvidence(
                prompts=prompts,
                repository_name=(
                    str(session["repository_name"]) if session["repository_name"] else None
                ),
                command_categories=categories,
                high_confidence_commit=high_commit,
            )
        )
        _upsert_classification(connection, session_id, classification)


def _activity_action(evidence: TaskEvidence) -> tuple[TaskAction, str | None]:
    categories = Counter(evidence.command_categories)
    if categories["testing"]:
        return TaskAction.TESTING, "fallback_originated_testing"
    if categories["editing_patching"]:
        return TaskAction.IMPLEMENTATION, "fallback_originated_edit"
    if evidence.high_confidence_commit or categories["git_mutation"]:
        return TaskAction.GIT_OR_RELEASE, "fallback_originated_git"
    return TaskAction.UNKNOWN, None


def _intent_text(prompt: str) -> str:
    """Remove explicit line-level non-goals before matching positive intent."""

    negation = re.compile(
        r"(?i)^\s*(?:[-*]\s*)?(?:do not|don't|never|non-goal\s*:|不要|不得|切勿)\b"
    )
    return "\n".join(line for line in prompt.splitlines() if not negation.search(line))


def _activity_domain(evidence: TaskEvidence) -> tuple[TaskDomain, str | None]:
    categories = Counter(evidence.command_categories)
    if categories["scientific_computation"]:
        return TaskDomain.SCIENTIFIC_COMPUTING, "fallback_scientific_command"
    if categories["testing"] or categories["editing_patching"]:
        return TaskDomain.SOFTWARE_ENGINEERING, "fallback_software_activity"
    if evidence.high_confidence_commit or categories["git_mutation"]:
        return TaskDomain.GIT_RELEASE, "fallback_git_activity"
    repository = (evidence.repository_name or "").casefold()
    if any(marker in repository for marker in ("orca", "cp2k", "vasp", "chem", "xps")):
        return TaskDomain.SCIENTIFIC_COMPUTING, "fallback_repository_name"
    return TaskDomain.UNKNOWN, None


def _best_action(scores: Counter[TaskAction]) -> TaskAction:
    if not scores:
        return TaskAction.UNKNOWN
    priority = {action: index for index, (action, _, _) in enumerate(_ACTION_RULES)}
    return min(scores, key=lambda action: (-scores[action], priority[action]))


def _best_domain(scores: Counter[TaskDomain]) -> TaskDomain:
    if not scores:
        return TaskDomain.UNKNOWN
    priority = {domain: index for index, (domain, _, _) in enumerate(_DOMAIN_RULES)}
    return min(scores, key=lambda domain: (-scores[domain], priority[domain]))


def _upsert_classification(
    connection: sqlite3.Connection,
    session_id: int,
    classification: TaskClassification,
) -> None:
    facets_json = json.dumps(classification.facets, separators=(",", ":"))
    evidence_json = json.dumps(classification.evidence, separators=(",", ":"))
    values = (
        classification.action.value,
        classification.domain.value,
        facets_json,
        classification.confidence.value,
        evidence_json,
        classification.taxonomy_version,
    )
    existing = connection.execute(
        "SELECT action, domain, facets_json, confidence, evidence_json, taxonomy_version "
        "FROM session_tasks WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    now = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    with connection:
        connection.execute(
            """
            INSERT INTO session_tasks(
                session_id, action, domain, facets_json, confidence,
                evidence_json, taxonomy_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                action = excluded.action, domain = excluded.domain,
                facets_json = excluded.facets_json, confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                taxonomy_version = excluded.taxonomy_version,
                updated_at = excluded.updated_at
            """,
            (session_id, *values, now),
        )
