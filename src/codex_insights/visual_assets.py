"""Packaged visual assets embedded into offline HTML outputs."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def load_visual_stylesheet() -> str:
    """Return the trusted, package-owned stylesheet for self-contained HTML."""

    resource = files("codex_insights").joinpath("assets").joinpath("codex-insights.css")
    stylesheet = resource.read_text(encoding="utf-8")
    if "</style" in stylesheet.casefold():
        raise RuntimeError("Packaged stylesheet contains an unsafe closing style tag")
    return stylesheet
