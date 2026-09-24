"""
FilmStash Bot - service layer.

Encapsulates every external dependency (TMDB, Supabase) behind plain Python
functions. Handlers import from here; this package must never import from
``handlers`` (that would create a circular import).
"""

from __future__ import annotations

from services.supabase_client import (
    db_delete_movie,
    db_find_movie,
    db_insert_movie,
    db_list_movies,
)
from services.tmdb_client import fetch_movie_from_tmdb

__all__ = [
    "db_delete_movie",
    "db_find_movie",
    "db_insert_movie",
    "db_list_movies",
    "fetch_movie_from_tmdb",
]
