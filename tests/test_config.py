from __future__ import annotations

from pathlib import Path

from codex_insights.config import CodexHomeSource, default_config_path, resolve_codex_home


def test_explicit_codex_home_takes_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    result = resolve_codex_home(
        explicit,
        environ={"CODEX_HOME": str(tmp_path / "environment")},
        home=tmp_path / "home",
    )

    assert result.path == explicit
    assert result.source is CodexHomeSource.EXPLICIT


def test_codex_home_environment_precedes_default(tmp_path: Path) -> None:
    configured = tmp_path / "environment"
    result = resolve_codex_home(
        environ={"CODEX_HOME": str(configured)},
        home=tmp_path / "home",
    )

    assert result.path == configured
    assert result.source is CodexHomeSource.ENVIRONMENT


def test_default_codex_home_uses_supplied_user_home(tmp_path: Path) -> None:
    result = resolve_codex_home(environ={}, home=tmp_path)

    assert result.path == tmp_path / ".codex"
    assert result.source is CodexHomeSource.DEFAULT


def test_default_privacy_config_path_is_platform_aware(tmp_path: Path) -> None:
    assert default_config_path(home=tmp_path, platform_name="darwin") == (
        tmp_path / "Library" / "Application Support" / "Codex Insights" / "config.json"
    )
    assert default_config_path(
        home=tmp_path,
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg-config")},
        platform_name="linux",
    ) == tmp_path / "xdg-config" / "codex-insights" / "config.json"
