from __future__ import annotations

import re
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


def test_light_theme_small_text_colors_meet_wcag_aa_contrast() -> None:
    stylesheet = load_visual_stylesheet()
    light_colors = {
        name: re.findall(rf"--ci-{name}: (#[0-9a-f]{{6}})", stylesheet)[-1]
        for name in (
            "canvas",
            "surface",
            "surface-subtle",
            "muted-cool",
            "accent",
            "accent-soft",
        )
    }

    for foreground in ("muted-cool", "accent", "accent-soft"):
        for background in ("canvas", "surface", "surface-subtle"):
            assert _contrast(light_colors[foreground], light_colors[background]) >= 4.5


def _contrast(left: str, right: str) -> float:
    brighter, darker = sorted(
        (_relative_luminance(left), _relative_luminance(right)), reverse=True
    )
    return (brighter + 0.05) / (darker + 0.05)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
