"""Privacy-safe, deterministic command normalization for derived analytics."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass

from codex_insights.models import CommandCategory, TestScope

COMMAND_CLASSIFIER_VERSION = "command-classifier-v1"
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
    re.compile(r"(?i)(https?://[^\s/?#]+[^\s?#]*[?&][^\s=]*(?:token|key|secret)=)[^\s&]+"),
)
_HEREDOC = re.compile(r"(?s)(<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?).*?\n\2(?:\s|$)")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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
    category: CommandCategory
    test_scope: TestScope
    redacted: bool
    truncated: bool


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
    tokens = _tokens(safe)
    executable = _executable(tokens)
    category = classify_command(tokens, executable=executable)
    scope = classify_test_scope(tokens, executable=executable)
    digest = hashlib.sha256(safe.encode("utf-8", errors="replace")).hexdigest()
    return SafeCommand(
        text=safe,
        fingerprint=digest,
        executable=executable,
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
    command_index = _command_index(lower)
    args = lower[command_index + 1 :] if command_index is not None else ()
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
    command_index = _command_index(lower)
    args = lower[command_index + 1 :] if command_index is not None else ()
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
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return tuple(command.split())


def _command_index(tokens: tuple[str, ...]) -> int | None:
    for index, token in enumerate(tokens):
        if _ASSIGNMENT.match(token) or token in {"env", "sudo", "command", "time", "nohup"}:
            continue
        return index
    return None


def _executable(tokens: tuple[str, ...]) -> str | None:
    index = _command_index(tokens)
    if index is None:
        return None
    return tokens[index].rsplit("/", 1)[-1].casefold()[:128]


def _first_positional(tokens: tuple[str, ...]) -> str | None:
    return next((token for token in tokens if not token.startswith("-")), None)


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
