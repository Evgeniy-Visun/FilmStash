"""
FilmStash Bot - TMDB API client.

All HTTP calls to The Movie Database live here. Every function is synchronous
and must be invoked from a worker thread (``asyncio.to_thread``) so the bot's
event loop stays responsive.

The public surface is deliberately small: :func:`fetch_movie_from_tmdb` returns
a clean, normalised dict, and failures are raised as typed exceptions rather
than leaking ``requests`` internals to the caller.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import (
    TMDB_API_KEY,
    TMDB_IMAGE_BASE,
    TMDB_MOVIE_URL,
    TMDB_SEARCH_URL,
    TMDB_TIMEOUT,
)

logger = logging.getLogger("filmstash.tmdb")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class MovieNotFoundError(Exception):
    """Raised when TMDB returns no usable match for a title."""


class ExternalServiceError(Exception):
    """Raised when TMDB fails for an infrastructure reason."""


# ---------------------------------------------------------------------------
# Low-level transport
# ---------------------------------------------------------------------------
def _tmdb_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Perform a single authenticated GET against TMDB and return parsed JSON.

    Raises
    ------
    ExternalServiceError
        On timeout, connection failure, non-2xx status or malformed JSON.
    """
    if not TMDB_API_KEY:
        raise ExternalServiceError("TMDB_API_KEY is not configured.")

    # TMDB v3 accepts the key either as a query param or a Bearer token.
    # The query-param form is the simplest and works with every key type.
    query = {**params, "api_key": TMDB_API_KEY, "language": "en-US"}

    try:
        response = requests.get(url, params=query, timeout=TMDB_TIMEOUT)
    except requests.Timeout as exc:
        raise ExternalServiceError("TMDB request timed out.") from exc
    except requests.RequestException as exc:
        raise ExternalServiceError("Could not reach TMDB.") from exc

    if response.status_code == 401:
        raise ExternalServiceError("TMDB rejected the API key (401).")
    if response.status_code == 429:
        raise ExternalServiceError("TMDB rate limit hit (429). Try again shortly.")
    if not response.ok:
        raise ExternalServiceError(f"TMDB returned HTTP {response.status_code}.")

    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ExternalServiceError("TMDB returned a malformed response.") from exc
    return payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_movie_from_tmdb(title: str) -> dict[str, Any]:
    """
    Resolve a free-text title to a normalised movie record.

    Strategy: search TMDB, take the best match (TMDB already sorts by
    relevance/popularity), then fetch the full detail record so we get a
    complete overview and vote_average.

    Returns
    -------
    dict with keys: title, imdb_rating, poster_url, plot, tmdb_id, release_year

    Raises
    ------
    MovieNotFoundError
        When the search yields no results.
    ExternalServiceError
        On any transport/API failure.
    """
    search_payload = _tmdb_get(TMDB_SEARCH_URL, {"query": title, "include_adult": "false"})
    results = search_payload.get("results") or []

    if not results:
        raise MovieNotFoundError(f"No TMDB match for {title!r}.")

    best = results[0]
    movie_id = best.get("id")

    # The search endpoint already returns overview/poster/vote_average, but the
    # detail endpoint is the authoritative source and also gives us imdb_id and
    # runtime if we want them later. Fall back to the search hit on failure so a
    # flaky detail call does not lose the whole lookup.
    detail: dict[str, Any] = best
    if movie_id:
        try:
            detail = _tmdb_get(TMDB_MOVIE_URL.format(movie_id=movie_id), {})
        except ExternalServiceError:
            logger.warning("Detail lookup failed for TMDB id %s; using search hit.", movie_id)

    poster_path = detail.get("poster_path")
    vote_average = detail.get("vote_average")
    release_date = detail.get("release_date") or ""

    return {
        "title": detail.get("title") or best.get("title") or title,
        "tmdb_id": movie_id,
        # TMDB's vote_average is 0-10 with one decimal; 0.0 usually means
        # "not enough votes yet", so we normalise that to None.
        "imdb_rating": round(float(vote_average), 1) if vote_average else None,
        "poster_url": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None,
        "plot": (detail.get("overview") or "").strip() or None,
        "release_year": release_date[:4] if len(release_date) >= 4 else None,
    }
