"""
FilmStash Bot - Supabase data access layer.

Every read and write against the ``movies`` table goes through this module.
Handlers never build queries themselves, which keeps the multi-user isolation
boundary (``.eq("user_id", user_id)``) in exactly one place.

All functions are synchronous and must be invoked from a worker thread
(``asyncio.to_thread``).
"""

from __future__ import annotations

import logging
from typing import Any

from supabase import Client, create_client

from config import LIST_LIMIT, SUPABASE_KEY, SUPABASE_URL
from services.tmdb_client import ExternalServiceError

logger = logging.getLogger("filmstash.supabase")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
# The Supabase client is created once at import time and reused. It is
# thread-safe for our usage pattern (one request per call, no shared cursors).
supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:  # pragma: no cover - only hit on bad credentials
        logger.exception("Failed to initialise the Supabase client.")
else:
    logger.warning(
        "SUPABASE_URL / SUPABASE_KEY are not set - database features are disabled."
    )


def _require_supabase() -> Client:
    """Return the Supabase client or raise if it was never configured."""
    if supabase is None:
        raise ExternalServiceError(
            "Database is not configured. Set SUPABASE_URL and SUPABASE_KEY."
        )
    return supabase


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def db_insert_movie(row: dict[str, Any]) -> dict[str, Any]:
    """Insert one movie row and return the stored record."""
    client = _require_supabase()
    try:
        response = client.table("movies").insert(row).execute()
    except Exception as exc:  # supabase-py raises various transport errors
        logger.exception("Supabase insert failed.")

        # Translate the two most common setup mistakes into actionable advice.
        message = str(exc)
        if "row-level security" in message or "42501" in message:
            raise ExternalServiceError(
                "The database rejected the write because of Row Level Security. "
                "This usually means SUPABASE_KEY holds the 'anon' key. Use the "
                "'service_role' key instead (Supabase Dashboard -> Project "
                "Settings -> API)."
            ) from exc
        if "PGRST205" in message or "schema cache" in message:
            raise ExternalServiceError(
                "The 'movies' table was not found. Run schema.sql in the "
                "Supabase SQL Editor first."
            ) from exc

        raise ExternalServiceError("Could not save the movie to the database.") from exc

    if not response.data:
        raise ExternalServiceError("The database did not confirm the insert.")
    inserted: dict[str, Any] = response.data[0]
    return inserted


def db_list_movies(user_id: int, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
    """Return this user's most recently added movies, newest first."""
    client = _require_supabase()
    try:
        response = (
            client.table("movies")
            .select("title, personal_rating, imdb_rating, created_at")
            .eq("user_id", user_id)          # <-- multi-user isolation
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        logger.exception("Supabase select (list) failed.")
        raise ExternalServiceError("Could not read your library right now.") from exc

    return response.data or []


def db_find_movie(user_id: int, title: str) -> dict[str, Any] | None:
    """
    Find the most recent entry for this user whose title matches (case
    insensitive, exact). Returns None when the user has not logged it.
    """
    client = _require_supabase()
    try:
        response = (
            client.table("movies")
            .select("title, personal_rating, imdb_rating, plot, poster_url, created_at")
            .eq("user_id", user_id)
            .ilike("title", title)           # case-insensitive exact match
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Supabase select (search) failed.")
        raise ExternalServiceError("Could not search your library right now.") from exc

    rows = response.data or []
    return rows[0] if rows else None


def db_delete_movie(user_id: int, title: str) -> list[dict[str, Any]]:
    """
    Delete every entry for this user whose title matches (case insensitive,
    exact) and return the deleted rows.

    Deleting *all* matching rows is deliberate: the table is a history log, so
    a user may have logged the same title more than once. ``/remove`` means
    "take this movie out of my stash", which is the whole set, not one row.

    The ``.eq("user_id", user_id)`` filter is the multi-user isolation boundary -
    without it this call would delete other users' rows. It is never optional.

    Returns an empty list when nothing matched, so the caller can tell the
    difference between "removed" and "you never logged this".
    """
    client = _require_supabase()
    try:
        response = (
            client.table("movies")
            .delete()
            .eq("user_id", user_id)          # <-- multi-user isolation
            .ilike("title", title)           # case-insensitive exact match
            .execute()
        )
    except Exception as exc:
        logger.exception("Supabase delete failed.")
        raise ExternalServiceError("Could not update your library right now.") from exc

    return response.data or []
