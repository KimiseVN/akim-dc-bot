"""Shared language constants for cogs that render bilingual cards.

Individual cogs keep their own translation dictionaries (each game has
its own vocabulary, so a single global dict would be more overhead than
help). This module just standardises the language codes, the toggle
button label/emoji, and the "flip to the other language" helper — so
every card feels consistent.
"""

from __future__ import annotations


LANG_VI = "vi"
LANG_EN = "en"
LANG_RU = "ru"
DEFAULT_LANG = LANG_VI
VALID_LANGS: frozenset[str] = frozenset({LANG_VI, LANG_EN, LANG_RU})


# The toggle button always offers the *other* language:
#   - lang == "vi" → button says "English" (click to switch to EN).
#   - lang == "en" → button says "Tiếng Việt" (click to switch to VI).
#   - lang == "ru" → button says "English" (click to switch to EN).
LANG_BUTTON_LABEL: dict[str, str] = {
    LANG_VI: "English",
    LANG_EN: "Tiếng Việt",
    LANG_RU: "English",
}
LANG_BUTTON_EMOJI = "🌐"


def normalize_lang(value: str | None) -> str:
    """Coerce a stored/user-supplied language code to a valid one,
    falling back to DEFAULT_LANG for anything unrecognised."""
    if value in VALID_LANGS:
        return value  # type: ignore[return-value]
    return DEFAULT_LANG


def next_lang(current: str) -> str:
    """Return the language the user should be flipped to on toggle."""
    return LANG_EN if normalize_lang(current) == LANG_VI else LANG_VI
