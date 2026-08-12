"""Privacy-safe, deterministic command normalization for derived analytics."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass

from codex_insights.models import CommandCategory, TestScope

COMMAND_CLASSIFIER_VERSION = "command-classifier-v2"
COMMAND_PRIVACY_VERSION = "command-privacy-v1"
MAX_STORED_COMMAND_CHARS = 512

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s'\"]+)",
    ),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*)"
        r"([^\s;&|]+)",
    ),
    re.compile(
        r"(?i)((?:--?)(?:api[_-]?key|token|secret|password|passwd|pwd)\s+)"
        r"([^\s;&|]+)",
    ),
    re.compile(r"(?i)(https?://[^\s/?#]+[^\s?#]*[?&][^\s=]*(?:token|key|secret)=)[^\s&]+"),
)
_HEREDOC = re.compile(r"(?s)(<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?).*?\n\2(?:\s|$)")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_KEYWORDS = {
    "!",
    "case",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "select",
    "then",
    "until",
    "while",
}
_GROUPING_TOKENS = {"(", ")", "{", "}"}
_SETUP_BUILTINS = {
    ".",
    ":",
    "alias",
    "cd",
    "export",
    "set",
    "source",
    "ulimit",
    "umask",
    "unset",
}

_ENV_FLAGS = {"-0", "-i", "--ignore-environment", "--null"}
_ENV_OPTIONS = {"-C", "--chdir", "-u", "--unset", "--argv0"}
_SUDO_FLAGS = {
    "-A",
    "-b",
    "-E",
    "-e",
    "-H",
    "-K",
    "-k",
    "-n",
    "-P",
    "-S",
    "-s",
    "-V",
}
_SUDO_OPTIONS = {
    "-C",
    "--close-from",
    "-D",
    "--chdir",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-r",
    "--role",
    "-T",
    "--command-timeout",
    "-t",
    "--type",
    "-u",
    "--user",
}
_TIME_FLAGS = {"-a", "--append", "-p", "-v", "--verbose"}
_TIME_OPTIONS = {"-f", "--format", "-o", "--output"}
_UV_RUN_FLAGS = {
    "--active",
    "--compile-bytecode",
    "--exact",
    "--frozen",
    "--isolated",
    "--locked",
    "--managed-python",
    "--native-tls",
    "--no-cache",
    "--no-editable",
    "--no-managed-python",
    "--no-progress",
    "--no-project",
    "--no-python-downloads",
    "--no-sync",
    "--offline",
}
_UV_RUN_OPTIONS = {
    "--default-index",
    "--directory",
    "--env-file",
    "--exclude-newer",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--index",
    "--index-url",
    "--link-mode",
    "--prerelease",
    "--project",
    "--python",
    "--resolution",
    "--with",
    "--with-editable",
    "--with-requirements",
}

_GIT_INSPECTION = {
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "rev-parse",
    "remote",
    "ls-files",
    "describe",
    "worktree",
}
_GIT_MUTATION = {
    "add",
    "commit",
    "push",
    "pull",
    "fetch",
    "merge",
    "rebase",
    "reset",
    "restore",
    "checkout",
    "switch",
    "tag",
    "rm",
    "mv",
    "cherry-pick",
}
_SCIENTIFIC_EXECUTABLES = {
    "orca",
    "orca_2mkl",
    "cp2k",
    "cp2k.psmp",
    "cp2k.popt",
    "vasp",
    "vasp_std",
    "vasp_gam",
    "vasp_ncl",
    "multiwfn",
}


@dataclass(frozen=True, slots=True)
class SafeCommand:
    """Bounded representation that never retains the original command alongside it."""

    text: str
    fingerprint: str
    executable: str | None
    operation: str | None
    category: CommandCategory
    test_scope: TestScope
    redacted: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class _ResolvedHead:
    executable: str
    classifier_executable: str
    classifier_tokens: tuple[str, ...]


def normalize_command(command: str) -> SafeCommand:
    """Redact sensitive values and classify one shell command without executing it."""

    safe = command.replace("\x00", "")
    redacted = False
    for pattern in _SECRET_PATTERNS:
        safe, count = pattern.subn(r"\1[REDACTED]", safe)
        redacted = redacted or count > 0
    safe, heredoc_count = _HEREDOC.subn(r"\1\n[HEREDOC REDACTED]\n\2", safe)
    redacted = redacted or heredoc_count > 0
    safe = " ".join(safe.split())
    truncated = len(safe) > MAX_STORED_COMMAND_CHARS
    if truncated:
        safe = safe[: MAX_STORED_COMMAND_CHARS - 1] + "…"
    resolved = _resolve_shell_head(_tokens(safe))
    executable = resolved.executable if resolved is not None else None
    classifier_executable = (
        resolved.classifier_executable if resolved is not None else None
    )
    classifier_tokens = resolved.classifier_tokens if resolved is not None else ()
    category = classify_command(classifier_tokens, executable=classifier_executable)
    scope = classify_test_scope(classifier_tokens, executable=classifier_executable)
    operation = _privacy_safe_operation(
        classifier_tokens,
        executable=classifier_executable,
    )
    digest = hashlib.sha256(safe.encode("utf-8", errors="replace")).hexdigest()
    return SafeCommand(
        text=safe,
        fingerprint=digest,
        executable=executable,
        operation=operation,
        category=category,
        test_scope=scope,
        redacted=redacted,
        truncated=truncated,
    )


def classify_tool_name(tool_name: str) -> CommandCategory:
    """Classify non-shell tools using stable semantic names."""

    lowered = tool_name.casefold()
    if "patch" in lowered or lowered in {"edit", "write_file"}:
        return CommandCategory.EDITING_PATCHING
    if lowered in {"wait", "wait_agent", "write_stdin", "sleep"}:
        return CommandCategory.WAIT_POLL
    if lowered in {"request_user_input"}:
        return CommandCategory.USER_INTERACTION
    return CommandCategory.OTHER


def classify_command(
    tokens: tuple[str, ...],
    *,
    executable: str | None,
) -> CommandCategory:
    """Assign an extensible deterministic category from bounded command syntax."""

    if executable is None:
        return CommandCategory.UNKNOWN
    lower = tuple(token.casefold() for token in tokens)
    args = lower[1:]
    name = executable.casefold()
    if name in {"git", "gh"}:
        subcommand = _first_positional(args)
        if subcommand in _GIT_INSPECTION or (name == "gh" and subcommand not in _GIT_MUTATION):
            return CommandCategory.GIT_INSPECTION
        if subcommand in _GIT_MUTATION:
            return CommandCategory.GIT_MUTATION
        return CommandCategory.GIT_INSPECTION
    if _is_test_command(name, args):
        return CommandCategory.TESTING
    if name in {"ruff", "flake8", "pylint", "eslint", "biome"}:
        return CommandCategory.LINTING
    if name in {"mypy", "pyright", "pyre"}:
        return CommandCategory.TYPE_CHECKING
    if _is_dependency_command(name, args):
        return CommandCategory.DEPENDENCY_MANAGEMENT
    if _is_build_command(name, args):
        return CommandCategory.BUILD_PACKAGING
    if name in {"rg", "grep", "ag", "ack"}:
        return CommandCategory.TEXT_SEARCH
    if name in {"ls", "find", "pwd", "tree", "wc", "stat", "du", "df", "head", "tail", "sed"}:
        return CommandCategory.FILESYSTEM_INSPECTION
    if name in {"ps", "pgrep", "top", "htop", "jobs", "lsof", "killall"}:
        return CommandCategory.PROCESS_STATUS_MONITORING
    if name in {"sleep"}:
        return CommandCategory.WAIT_POLL
    if name in _SCIENTIFIC_EXECUTABLES or name.startswith(("orca_", "cp2k.", "vasp_")):
        return CommandCategory.SCIENTIFIC_COMPUTATION
    if name in {"python", "python3", "ipython", "jupyter"}:
        if any(marker in " ".join(args) for marker in ("ase", "orca", "cp2k", "vasp", "multiwfn")):
            return CommandCategory.SCIENTIFIC_COMPUTATION
        return CommandCategory.PYTHON_EXECUTION
    if name in {"apply_patch", "patch", "perl"}:
        return CommandCategory.EDITING_PATCHING
    return CommandCategory.OTHER


def classify_test_scope(
    tokens: tuple[str, ...],
    *,
    executable: str | None,
) -> TestScope:
    """Infer only test scopes that command syntax demonstrates."""

    if executable is None:
        return TestScope.NOT_APPLICABLE
    lower = tuple(token.casefold() for token in tokens)
    args = lower[1:]
    name = executable.casefold()
    if not _is_test_command(name, args):
        return TestScope.NOT_APPLICABLE
    if "-k" in args or any("::" in token for token in args):
        return TestScope.SUBSET
    positional = tuple(token for token in args if not token.startswith("-") and token != "test")
    if any(
        token.endswith((".py", ".rs", ".go", ".js", ".ts"))
        or token.startswith(("tests/", "test/"))
        for token in positional
    ):
        return TestScope.FILE
    if name in {"pytest", "py.test", "tox", "nox"} and not positional:
        return TestScope.FULL_SUITE
    if name in {"cargo", "go", "npm", "pnpm", "yarn"} and _first_positional(args) == "test":
        return TestScope.FULL_SUITE if len(positional) <= 1 else TestScope.UNKNOWN
    if name in {"python", "python3"} and "-m" in args and "pytest" in args:
        pytest_index = args.index("pytest")
        following = tuple(token for token in args[pytest_index + 1 :] if not token.startswith("-"))
        return TestScope.FULL_SUITE if not following else TestScope.FILE
    return TestScope.UNKNOWN


def _tokens(command: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return tuple(lexer)
    except ValueError:
        return ()


def _resolve_shell_head(tokens: tuple[str, ...]) -> _ResolvedHead | None:
    """Return the first defensible command head without evaluating shell syntax."""

    if not tokens or any(token in _GROUPING_TOKENS for token in tokens):
        return None
    segments = _segments(tokens)
    if segments is None:
        return None
    for segment, following_operator in segments:
        resolved, skippable_setup = _resolve_simple_command(segment)
        if resolved is not None:
            return _resolve_uv_workload(resolved)
        if not skippable_setup or following_operator not in {"&&", ";"}:
            return None
    return None


def _segments(
    tokens: tuple[str, ...],
) -> tuple[tuple[tuple[str, ...], str | None], ...] | None:
    segments: list[tuple[tuple[str, ...], str | None]] = []
    current: list[str] = []
    for token in tokens:
        if _is_control_operator(token):
            if not current:
                return None
            segments.append((tuple(current), token))
            current = []
        else:
            current.append(token)
    if not current:
        return None
    segments.append((tuple(current), None))
    return tuple(segments)


def _resolve_simple_command(
    tokens: tuple[str, ...],
) -> tuple[_ResolvedHead | None, bool]:
    index = _skip_assignments(tokens, 0)
    if index >= len(tokens) or _looks_like_leading_redirection(tokens, index):
        return None, False

    for _ in range(8):
        name = _normalized_executable(tokens[index])
        if name is None:
            return None, False
        if name in _SHELL_KEYWORDS:
            return None, False
        if name in _SETUP_BUILTINS:
            return None, True
        if name not in {"command", "env", "exec", "nohup", "sudo", "time"}:
            command_tokens = tokens[index:]
            return _ResolvedHead(name, name, command_tokens), False
        next_index = _wrapped_command_index(tokens, index, name)
        if next_index is None:
            return None, False
        index = _skip_assignments(tokens, next_index)
        if index >= len(tokens):
            return None, False
    return None, False


def _resolve_uv_workload(resolved: _ResolvedHead) -> _ResolvedHead:
    tokens = resolved.classifier_tokens
    if resolved.executable != "uv" or len(tokens) < 3 or tokens[1].casefold() != "run":
        return resolved
    index = _skip_known_options(
        tokens,
        2,
        flags=_UV_RUN_FLAGS,
        options=_UV_RUN_OPTIONS,
    )
    if index is None or index >= len(tokens):
        return resolved
    workload, skippable_setup = _resolve_simple_command(tokens[index:])
    if workload is None or skippable_setup:
        return resolved
    return _ResolvedHead(
        executable="uv",
        classifier_executable=workload.classifier_executable,
        classifier_tokens=workload.classifier_tokens,
    )


def _wrapped_command_index(
    tokens: tuple[str, ...],
    wrapper_index: int,
    wrapper: str,
) -> int | None:
    index = wrapper_index + 1
    if wrapper == "env":
        return _skip_known_options(
            tokens,
            index,
            flags=_ENV_FLAGS,
            options=_ENV_OPTIONS,
            assignments=True,
            reject_options={"-S", "--split-string"},
        )
    if wrapper == "sudo":
        return _skip_known_options(
            tokens,
            index,
            flags=_SUDO_FLAGS,
            options=_SUDO_OPTIONS,
        )
    if wrapper == "time":
        return _skip_known_options(
            tokens,
            index,
            flags=_TIME_FLAGS,
            options=_TIME_OPTIONS,
        )
    if wrapper == "command":
        return _skip_known_options(
            tokens,
            index,
            flags={"-p"},
            options=set(),
            reject_options={"-V", "-v"},
        )
    if wrapper == "exec":
        return _skip_known_options(
            tokens,
            index,
            flags={"-c", "-l"},
            options={"-a"},
        )
    return _skip_known_options(
        tokens,
        index,
        flags=set(),
        options=set(),
        reject_options={"--help", "--version"},
    )


def _skip_known_options(
    tokens: tuple[str, ...],
    start: int,
    *,
    flags: set[str],
    options: set[str],
    assignments: bool = False,
    reject_options: frozenset[str] | set[str] = frozenset(),
) -> int | None:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if assignments and _ASSIGNMENT.match(token):
            index += 1
            continue
        if token in reject_options or any(
            token.startswith(f"{option}=") for option in reject_options
        ):
            return None
        if token in flags:
            index += 1
            continue
        if token in options:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if any(
            token.startswith(f"{option}=")
            for option in options
            if option.startswith("--")
        ):
            index += 1
            continue
        if token.startswith("-"):
            return None
        return index
    return None


def _skip_assignments(tokens: tuple[str, ...], start: int) -> int:
    index = start
    while index < len(tokens) and _ASSIGNMENT.match(tokens[index]):
        index += 1
    return index


def _normalized_executable(token: str) -> str | None:
    if (
        not token
        or token.startswith("-")
        or token in _GROUPING_TOKENS
        or _is_control_operator(token)
        or _is_redirection_operator(token)
        or "`" in token
        or "$" in token
    ):
        return None
    name = token.rsplit("/", 1)[-1].casefold()[:128]
    if not name or name.startswith("-") or name in _SHELL_KEYWORDS:
        return None
    return name


def _looks_like_leading_redirection(tokens: tuple[str, ...], index: int) -> bool:
    token = tokens[index]
    return _is_redirection_operator(token) or (
        token.isdigit()
        and index + 1 < len(tokens)
        and _is_redirection_operator(tokens[index + 1])
    )


def _is_control_operator(token: str) -> bool:
    return bool(token) and set(token) <= {";", "&", "|"}


def _is_redirection_operator(token: str) -> bool:
    return (
        bool(token)
        and bool({"<", ">"} & set(token))
        and set(token) <= {"<", ">", "&"}
    )


def _first_positional(tokens: tuple[str, ...]) -> str | None:
    return next((token for token in tokens if not token.startswith("-")), None)


def _privacy_safe_operation(
    tokens: tuple[str, ...],
    *,
    executable: str | None,
) -> str | None:
    """Retain only the non-sensitive operation needed for derived Git provenance."""

    if executable != "git":
        return None
    subcommand = _first_positional(tuple(token.casefold() for token in tokens[1:]))
    return "git_commit" if subcommand == "commit" else None


def _is_test_command(name: str, args: tuple[str, ...]) -> bool:
    if name in {"pytest", "py.test", "tox", "nox"}:
        return True
    if name in {"cargo", "go", "npm", "pnpm", "yarn"}:
        return _first_positional(args) == "test"
    return name in {"python", "python3"} and "-m" in args and (
        "pytest" in args or "unittest" in args
    )


def _is_dependency_command(name: str, args: tuple[str, ...]) -> bool:
    if name in {"pip", "pip3", "poetry", "conda", "mamba", "brew"}:
        return _first_positional(args) in {"install", "add", "remove", "update", "sync", "lock"}
    if name == "uv":
        return _first_positional(args) in {"add", "remove", "sync", "lock", "pip"}
    if name in {"npm", "pnpm", "yarn"}:
        return _first_positional(args) in {"install", "add", "remove", "update"}
    return False


def _is_build_command(name: str, args: tuple[str, ...]) -> bool:
    if name in {"make", "cmake", "ninja", "meson"}:
        return True
    if name in {"cargo", "npm", "pnpm", "yarn", "uv"}:
        return _first_positional(args) in {"build", "package", "publish"}
    return name in {"python", "python3"} and "-m" in args and "build" in args
