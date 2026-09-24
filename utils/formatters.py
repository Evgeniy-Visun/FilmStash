"""
FilmStash Bot - presentation helpers.

Everything that turns raw data (a TMDB dict, a Supabase row) into a
Telegram-ready string lives here. Handlers call these functions and send the
result; they never build HTML by hand.

All output targets Telegram's **HTML** parse mode, so every piece of
user/API-supplied text is escaped before interpolation.
"""

from __future__ import annotations

import html
from typing import Any

from config import MAX_PLOT_CHARS

# ---------------------------------------------------------------------------
# HTML-safe usage placeholders
# ---------------------------------------------------------------------------
# Telegram's HTML parser accepts only a fixed tag whitelist (<b>, <i>, <code>,
# <a>, ...). A bare "<Title>" is therefore read as an unsupported start tag and
# the send fails with:
#     BadRequest: Can't parse entities: unsupported start tag "title"
# We build the escaped forms once here and interpolate them into every usage
# hint, so the angle brackets render literally inside <code> blocks.
# Built from character codes so the literal angle brackets are unambiguous and
# cannot be silently normalised by an editor or patch tool.
_LT = chr(38) + "lt;"   # renders as "<"
_GT = chr(38) + "gt;"   # renders as ">"

PLACEHOLDER_TITLE = f"{_LT}Title{_GT}"
PLACEHOLDER_RATING = f"{_LT}Rating{_GT}"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def escape(text: str | None) -> str:
    """Escape user/API text for Telegram HTML parse mode."""
    return html.escape(text or "", quote=False)


def truncate(text: str | None, limit: int = MAX_PLOT_CHARS) -> str:
    """Trim long plot text on a word boundary and add an ellipsis."""
    if not text:
        return "No plot summary available."
    clean = " ".join(text.split())  # collapse newlines/extra spaces
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "..."


def format_rating(value: float | None) -> str:
    """Render a numeric rating or a friendly placeholder."""
    return f"{value:.1f}/10" if value is not None else "N/A"


def format_stars(value: float | None) -> str:
    """
    Render a 0-10 rating as a five-star bar, e.g. ``★★★★☆``.

    Used by the Mini App dashboard and any future text summary. Returns an
    empty string when there is no rating to show.
    """
    if value is None:
        return ""
    filled = max(0, min(5, round(float(value) / 2)))
    return "★" * filled + "☆" * (5 - filled)


# ---------------------------------------------------------------------------
# Composite cards
# ---------------------------------------------------------------------------
def format_movie_card(
    title: str | None,
    personal_rating: int | None,
    imdb_rating: float | None,
    plot: str | None,
    release_year: str | None = None,
    heading: str = "Saved to your stash",
    emoji: str = "✅",
) -> str:
    """
    Build the standard movie card used by /add and /search.

    Parameters
    ----------
    title
        Movie title (escaped here).
    personal_rating
        The user's own 1-10 score.
    imdb_rating
        TMDB's vote_average, or None.
    plot
        Overview text; truncated and escaped here.
    release_year
        Optional four-digit year, rendered in parentheses.
    heading
        Card headline, e.g. "Saved to your stash".
    emoji
        Leading emoji for the headline.
    """
    year = f" ({escape(release_year)})" if release_year else ""
    return (
        f"{emoji} <b>{escape(heading)}</b>\n\n"
        f"🎬 <b>{escape(title)}</b>{year}\n"
        f"⭐ Your rating: <b>{personal_rating}/10</b>\n"
        f"🌍 TMDB rating: <b>{format_rating(imdb_rating)}</b>\n\n"
        f"📖 <i>{escape(truncate(plot))}</i>"
    )


def format_stash_list(rows: list[dict[str, Any]], limit: int = 10) -> str:
    """
    Render the /list output for a user's most recent movies.

    ``rows`` are Supabase records with keys: title, personal_rating,
    imdb_rating, created_at.
    """
    lines = [f"🍿 <b>Your last {limit} movies</b>\n"]
    for index, row in enumerate(rows, start=1):
        created = (row.get("created_at") or "")[:10]  # YYYY-MM-DD
        lines.append(
            f"{index}. <b>{escape(row.get('title'))}</b> — "
            f"you: <b>{row.get('personal_rating')}/10</b>, "
            f"TMDB: {format_rating(row.get('imdb_rating'))} "
            f"<i>({escape(created)})</i>"
        )
    return "\n".join(lines)
