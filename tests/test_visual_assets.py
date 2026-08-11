from __future__ import annotations

from importlib.resources import files

from codex_insights.visual_assets import load_visual_stylesheet


def test_packaged_visual_stylesheet_is_local_and_safe_to_embed() -> None:
    resource = files("codex_insights").joinpath("assets").joinpath("codex-insights.css")
    stylesheet = load_visual_stylesheet()

    assert resource.is_file()
    assert "--ci-canvas: #11181d" in stylesheet
    assert "--ci-accent: #c9855c" in stylesheet
    assert "prefers-color-scheme: light" in stylesheet
    assert "</style" not in stylesheet.casefold()
    assert "url(" not in stylesheet.casefold()
    assert "http://" not in stylesheet and "https://" not in stylesheet
