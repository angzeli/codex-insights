"""Privacy-safe normalization of selected Codex rollout events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from codex_insights.command_normalization import (
    COMMAND_CLASSIFIER_VERSION,
    classify_tool_name,
    normalize_command,
)
from codex_insights.models import (
    CommandCategory,
    EventFamily,
    NormalizedEventObservation,
    NormalizedPromptCandidate,
    NormalizedToolCallCandidate,
    NormalizedToolResultCandidate,
    TestScope,
    ToolFamily,
    ToolResultStatus,
)

EVENT_FINGERPRINT_VERSION = "event-fingerprint-v1"
TOOL_EXTRACTION_VERSION = "codex-tool-extraction-v1"

_ID_FIELDS = ("id", "call_id", "client_id", "event_id", "turn_id")
_VALIDATION_PATTERN = re.compile(
    r"(?:^|[\s/])(pytest|ruff|mypy|tox|nox|unittest|cargo\s+test|go\s+test|npm\s+test)"
)
_GIT_PATTERN = re.compile(r"^\s*(?:git|gh)(?:\s|$)")
_FULL_GIT_HASH_LINE = re.compile(r"(?im)^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GIT_COMMIT_SUMMARY_HASH = re.compile(r"(?m)^\[[^\]]*\s([0-9a-f]{7,64})\]")


@dataclass(frozen=True, slots=True)
class ExtractedEvent:
    """One content-free observation and optional transient user text."""

    observation: NormalizedEventObservation
    prompt: NormalizedPromptCandidate | None = None
    tool_calls: tuple[NormalizedToolCallCandidate, ...] = ()
    tool_result: NormalizedToolResultCandidate | None = None


def extract_event(
    record: Mapping[str, Any],
    *,
    source_ordinal: int,
    family_ordinal: int,
    occurred_at: datetime | None,
) -> ExtractedEvent | None:
    """Extract a selected semantic event without retaining its raw payload."""

    record_type = _short_text(record.get("type") or record.get("event") or record.get("kind"))
    payload_value = record.get("payload")
    payload = payload_value if isinstance(payload_value, Mapping) else {}
    payload_type = _short_text(payload.get("type"))
    family = _event_family(record_type, payload_type, payload)
    if family is None:
        return None

    canonical, content_length, prompt_text = _canonical_event(family, payload_type, payload)
    fingerprint = _digest_json({"family": family.value, "semantic": canonical})
    stable_id_digest = _stable_id_digest(payload)
    observation = NormalizedEventObservation(
        source_ordinal=source_ordinal,
        family_ordinal=family_ordinal,
        family=family,
        fingerprint=fingerprint,
        source_record_type=record_type,
        source_payload_type=payload_type,
        occurred_at=occurred_at,
        stable_id_digest=stable_id_digest,
        approximate_content_length=content_length,
        fingerprint_version=EVENT_FINGERPRINT_VERSION,
    )
    prompt = (
        NormalizedPromptCandidate(
            source_ordinal=source_ordinal,
            fingerprint=fingerprint,
            occurred_at=occurred_at,
            text=prompt_text,
        )
        if family is EventFamily.USER_MESSAGE and prompt_text is not None
        else None
    )
    return ExtractedEvent(
        observation=observation,
        prompt=prompt,
        tool_calls=_tool_call_candidates(payload_type, payload, source_ordinal, occurred_at),
        tool_result=_tool_result_candidate(payload_type, payload, source_ordinal),
    )


def _event_family(
    record_type: str,
    payload_type: str,
    payload: Mapping[str, Any],
) -> EventFamily | None:
    if payload_type == "user_message":
        return EventFamily.USER_MESSAGE
    if payload_type == "message":
        role = _short_text(payload.get("role"))
        if role == "user":
            return EventFamily.USER_MESSAGE
        if role == "assistant":
            return EventFamily.ASSISTANT_MESSAGE
        return None
    if payload_type == "agent_message":
        return (
            EventFamily.ASSISTANT_MESSAGE
            if record_type == "event_msg"
            else EventFamily.INTER_AGENT_MESSAGE
        )
    if payload_type in {"function_call", "custom_tool_call", "tool_call"}:
        return _tool_family(payload)
    if payload_type in {"function_call_output", "custom_tool_call_output"}:
        return EventFamily.TOOL_OUTPUT
    if payload_type == "patch_apply_end":
        return EventFamily.PATCH_RESULT
    if payload_type in {"task_started", "task_complete", "turn_aborted"}:
        return EventFamily.TASK_LIFECYCLE
    combined = f"{record_type} {payload_type}"
    if any(marker in combined for marker in ("error", "failed")):
        return EventFamily.ERROR
    return None


def _tool_family(payload: Mapping[str, Any]) -> EventFamily:
    name = _short_text(payload.get("name") or payload.get("tool_name"))
    lowered = name.lower()
    if "patch" in lowered or lowered in {"edit", "write_file"}:
        return EventFamily.PATCH_EDIT
    if lowered in {"exec_command", "shell", "shell_command", "command"}:
        command = _command_text(payload)
        if command is not None and _GIT_PATTERN.search(command):
            return EventFamily.GIT_COMMAND
        if command is not None and _VALIDATION_PATTERN.search(command):
            return EventFamily.VALIDATION_COMMAND
        return EventFamily.SHELL_COMMAND
    return EventFamily.TOOL_CALL


def _command_text(payload: Mapping[str, Any]) -> str | None:
    arguments = payload.get("arguments", payload.get("input"))
    parsed: Any = arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, Mapping):
        return None
    command = parsed.get("cmd") or parsed.get("command")
    return command.lower() if isinstance(command, str) else None


def _tool_call_candidates(
    payload_type: str,
    payload: Mapping[str, Any],
    source_ordinal: int,
    occurred_at: datetime | None,
) -> tuple[NormalizedToolCallCandidate, ...]:
    if payload_type not in {"function_call", "custom_tool_call", "tool_call"}:
        return ()
    outer_name = _short_text(payload.get("name") or payload.get("tool_name"))
    arguments = payload.get("arguments", payload.get("input"))
    parsed = _decode_arguments(arguments)
    nested_source = arguments if isinstance(arguments, str) else None
    if outer_name in {"js", "javascript"} and isinstance(parsed, Mapping):
        code = parsed.get("code")
        nested_source = code if isinstance(code, str) else nested_source
    specifications = (
        _nested_tool_specs(nested_source)
        if outer_name in {"exec", "js", "javascript"} and nested_source is not None
        else ()
    )
    if not specifications:
        specifications = ((outer_name or "unknown", parsed),)
    call_id_digest = _call_id_digest(payload)
    results: list[NormalizedToolCallCandidate] = []
    for operation_ordinal, (name, operation_arguments) in enumerate(specifications):
        normalized_name = _short_text(name) or "unknown"
        command = _command_from_arguments(operation_arguments)
        if command is not None:
            safe = normalize_command(command)
            category = safe.category
            command_text = safe.text
            command_fingerprint = safe.fingerprint
            executable = safe.executable
            command_operation = safe.operation
            test_scope = safe.test_scope
            redacted = safe.redacted
            truncated = safe.truncated
        else:
            category = classify_tool_name(normalized_name)
            command_text = None
            command_fingerprint = None
            executable = None
            command_operation = None
            test_scope = TestScope.NOT_APPLICABLE
            redacted = False
            truncated = False
        results.append(
            NormalizedToolCallCandidate(
                source_ordinal=source_ordinal,
                operation_ordinal=operation_ordinal,
                occurred_at=occurred_at,
                call_id_digest=call_id_digest,
                tool_family=_normalized_tool_family(normalized_name, category),
                tool_name=normalized_name[:128],
                command_category=category,
                command_text=command_text,
                command_fingerprint=command_fingerprint,
                executable=executable,
                command_operation=command_operation,
                test_scope=test_scope,
                redacted=redacted,
                truncated=truncated,
                extraction_version=TOOL_EXTRACTION_VERSION,
                classifier_version=COMMAND_CLASSIFIER_VERSION,
            )
        )
    return tuple(results)


def _tool_result_candidate(
    payload_type: str,
    payload: Mapping[str, Any],
    source_ordinal: int,
) -> NormalizedToolResultCandidate | None:
    if payload_type == "patch_apply_end":
        success = payload.get("success")
        status = (
            ToolResultStatus.SUCCESS
            if success is True
            else ToolResultStatus.FAILURE
            if success is False
            else ToolResultStatus.UNKNOWN
        )
        return NormalizedToolResultCandidate(
            source_ordinal=source_ordinal,
            call_id_digest=_call_id_digest(payload),
            status=status,
        )
    if payload_type not in {"function_call_output", "custom_tool_call_output"}:
        return None
    structured = _structured_output(payload.get("output"))
    exit_code = _optional_int(structured.get("exit_code")) if structured else None
    duration = _optional_float(structured.get("wall_time_seconds")) if structured else None
    if exit_code is not None:
        status = ToolResultStatus.SUCCESS if exit_code == 0 else ToolResultStatus.FAILURE
    elif structured and structured.get("isError") is True:
        status = ToolResultStatus.FAILURE
    elif structured and structured.get("ok") is True:
        status = ToolResultStatus.SUCCESS
    else:
        status = ToolResultStatus.UNKNOWN
    output_text = structured.get("output") if structured else None
    full_hashes = (
        tuple(_FULL_GIT_HASH_LINE.findall(output_text))
        if isinstance(output_text, str)
        else ()
    )
    abbreviated = (
        _GIT_COMMIT_SUMMARY_HASH.search(output_text)
        if isinstance(output_text, str)
        else None
    )
    return NormalizedToolResultCandidate(
        source_ordinal=source_ordinal,
        call_id_digest=_call_id_digest(payload),
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        git_commit_hash=(full_hashes[0].casefold() if len(set(full_hashes)) == 1 else None),
        git_commit_abbrev=(abbreviated.group(1).casefold() if abbreviated else None),
    )


def _nested_tool_specs(source: str) -> tuple[tuple[str, Any], ...]:
    decoder = json.JSONDecoder()
    results: list[tuple[str, Any]] = []
    for match in re.finditer(r"tools\.([A-Za-z0-9_]+)\s*\(", source):
        position = match.end()
        while position < len(source) and source[position].isspace():
            position += 1
        try:
            arguments, _ = decoder.raw_decode(source, position)
        except json.JSONDecodeError:
            arguments = None
        results.append((match.group(1), arguments))
    return tuple(results)


def _decode_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return None


def _command_from_arguments(arguments: Any) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    command = arguments.get("cmd") or arguments.get("command")
    return command if isinstance(command, str) else None


def _normalized_tool_family(name: str, category: CommandCategory) -> ToolFamily:
    lowered = name.casefold()
    if category in {
        CommandCategory.GIT_INSPECTION,
        CommandCategory.GIT_MUTATION,
        CommandCategory.TESTING,
        CommandCategory.LINTING,
        CommandCategory.TYPE_CHECKING,
        CommandCategory.BUILD_PACKAGING,
        CommandCategory.FILESYSTEM_INSPECTION,
        CommandCategory.TEXT_SEARCH,
        CommandCategory.PYTHON_EXECUTION,
        CommandCategory.DEPENDENCY_MANAGEMENT,
        CommandCategory.SCIENTIFIC_COMPUTATION,
        CommandCategory.PROCESS_STATUS_MONITORING,
    } or lowered in {"exec_command", "shell", "shell_command", "command"}:
        return ToolFamily.SHELL
    if category is CommandCategory.EDITING_PATCHING:
        return ToolFamily.PATCH
    if category is CommandCategory.USER_INTERACTION:
        return ToolFamily.USER_INTERACTION
    if lowered in {
        "wait",
        "wait_agent",
        "write_stdin",
        "spawn_agent",
        "followup_task",
        "list_agents",
    }:
        return ToolFamily.COLLABORATION
    if lowered in {"view_image", "read_mcp_resource"}:
        return ToolFamily.FILE
    if lowered.startswith(("web__", "mcp__")):
        return ToolFamily.NETWORK
    return ToolFamily.OTHER if lowered != "unknown" else ToolFamily.UNKNOWN


def _call_id_digest(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("call_id")
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    return _digest_json({"call_id": value})


def _structured_output(value: Any) -> Mapping[str, Any] | None:
    candidates: list[Any] = [value]
    if isinstance(value, list):
        candidates.extend(
            item.get("text")
            for item in value
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        )
    for candidate in candidates:
        decoded = _decode_arguments(candidate)
        if isinstance(decoded, Mapping):
            return decoded
    return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return float(value)


def _canonical_event(
    family: EventFamily,
    payload_type: str,
    payload: Mapping[str, Any],
) -> tuple[object, int | None, str | None]:
    if family is EventFamily.USER_MESSAGE:
        text = _message_text(payload)
        attachments = _attachment_descriptors(payload)
        return (
            {"role": "user", "text": text, "attachments": attachments},
            len(text) if text is not None else None,
            text,
        )
    if family in {EventFamily.ASSISTANT_MESSAGE, EventFamily.INTER_AGENT_MESSAGE}:
        text = _message_text(payload)
        return ({"role": family.value, "text": text}, len(text) if text else None, None)
    if family in {
        EventFamily.TOOL_CALL,
        EventFamily.SHELL_COMMAND,
        EventFamily.VALIDATION_COMMAND,
        EventFamily.GIT_COMMAND,
        EventFamily.PATCH_EDIT,
    }:
        arguments = payload.get("arguments", payload.get("input"))
        return (
            {
                "name": _short_text(payload.get("name") or payload.get("tool_name")),
                "namespace": _short_text(payload.get("namespace")),
                "arguments_digest": _digest_value(arguments),
            },
            _approximate_length(arguments),
            None,
        )
    if family is EventFamily.TOOL_OUTPUT:
        output = payload.get("output")
        return (
            {"payload_type": payload_type, "output_digest": _digest_value(output)},
            _approximate_length(output),
            None,
        )
    if family is EventFamily.PATCH_RESULT:
        private_parts = {
            key: _digest_value(payload.get(key))
            for key in ("changes", "stdout", "stderr")
            if key in payload
        }
        return (
            {
                "payload_type": payload_type,
                "status": _short_text(payload.get("status")),
                "success": (
                    payload.get("success")
                    if isinstance(payload.get("success"), bool)
                    else None
                ),
                "private_digests": private_parts,
            },
            sum(_approximate_length(payload.get(key)) or 0 for key in private_parts),
            None,
        )
    if family is EventFamily.TASK_LIFECYCLE:
        return (
            {
                "payload_type": payload_type,
                "reason": _short_text(payload.get("reason")),
                "has_error": payload.get("error") is not None,
                "message_digest": _digest_value(payload.get("last_agent_message")),
            },
            _approximate_length(payload.get("last_agent_message")),
            None,
        )
    return (
        {"payload_type": payload_type, "payload_digest": _digest_value(payload)},
        _approximate_length(payload),
        None,
    )


def _message_text(payload: Mapping[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, str):
        return message
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts) if parts else None


def _attachment_descriptors(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    descriptors: list[tuple[str, str]] = []
    for key in ("images", "local_images", "audio", "local_audio"):
        value = payload.get(key)
        if isinstance(value, list):
            descriptors.extend((key, _digest_value(item)) for item in value)
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") == "input_text":
                continue
            descriptors.append((str(item.get("type", "attachment")), _digest_value(item)))
    return tuple(descriptors)


def _stable_id_digest(payload: Mapping[str, Any]) -> str | None:
    values = {
        key: payload[key]
        for key in _ID_FIELDS
        if isinstance(payload.get(key), (str, int)) and not isinstance(payload.get(key), bool)
    }
    return _digest_json(values) if values else None


def _digest_value(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8", errors="replace")).hexdigest()


def _digest_json(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8", errors="replace")).hexdigest()


def _stable_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _approximate_length(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return len(value)
    return None


def _short_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text[:256]
