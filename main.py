"""
FilmStash Bot - a personal movie tracker for Telegram.

Stack
-----
* python-telegram-bot v20+ (fully asynchronous)
* TMDB API          - movie metadata (poster, score, plot)
* Supabase          - cloud PostgreSQL storage, one row per logged movie

Commands
--------
/start              Welcome message + usage help.
/add <Title> <Rating>   Look the title up on TMDB, store it, reply with a card.
/list               Show the 10 most recent movies logged by *this* user.
/search <Title>     Check whether this user already logged a given movie.

Design notes
------------
* Every blocking call (TMDB via `requests`, Supabase via its sync client) is
  pushed onto a worker thread with `asyncio.to_thread`. This keeps the bot's
  event loop responsive so one slow network call cannot stall other users.
* All database reads/writes are scoped by `user_id`, which is the Telegram
  numeric id. That is the multi-user isolation boundary.
* User-facing errors are deliberately friendly; technical detail goes to the
  logger so we never leak stack traces or API keys into a chat.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load variables from a local `.env` file into os.environ. `override=False`
# means real environment variables (e.g. set by Docker or a host) win over the
# file, which is the behaviour you want in production.
load_dotenv(override=False)

TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL")
SUPABASE_KEY: Optional[str] = os.getenv("SUPABASE_KEY")
TMDB_API_KEY: Optional[str] = os.getenv("TMDB_API_KEY")

# --- Webhook / hosting ------------------------------------------------------
# Two run modes are supported:
#
#   * POLLING (default, local development) - the bot calls Telegram's
#     getUpdates in a loop. No public URL needed. This is what `python main.py`
#     does on your machine.
#
#   * WEBHOOK (production, e.g. Render) - Telegram pushes each update to a
#     public HTTPS endpoint that we host. This is required on hosts that do not
#     allow a long-lived outbound polling loop, and it is the recommended mode
#     for Render.
#
# Webhook mode activates automatically when WEBHOOK_URL is set, so the same
# code runs locally and in production with no flags to remember.
WEBHOOK_URL: Optional[str] = os.getenv("WEBHOOK_URL")

# The port the HTTP server binds to. Render injects PORT at runtime and routes
# its public HTTPS traffic to it, so we must honour it. 10000 is Render's
# documented default and the fallback used when PORT is unset.
PORT: int = int(os.getenv("PORT", "10000"))

# The interface to bind. 0.0.0.0 is required inside a container/host so the
# platform's router can reach the process; 127.0.0.1 would only accept
# connections from inside the same machine.
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")

# The URL path Telegram will POST updates to. Keeping it non-obvious means a
# random internet scanner cannot easily hit the endpoint. It must start with a
# slash and must match the path appended to WEBHOOK_URL.
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook")

# Optional shared secret. When set, Telegram sends it in the
# X-Telegram-Bot-Api-Secret-Token header and python-telegram-bot rejects any
# request whose header does not match. This is the recommended way to stop
# strangers from POSTing fake updates to your public endpoint.
WEBHOOK_SECRET: Optional[str] = os.getenv("WEBHOOK_SECRET")

# True when a public URL is configured, i.e. we should run in webhook mode.
WEBHOOK_ENABLED: bool = bool(WEBHOOK_URL)

# TMDB REST endpoints. `search/movie` finds candidates; `movie/{id}` is used
# when we need the full record for a specific id.
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_SEARCH_URL = f"{TMDB_BASE_URL}/search/movie"
TMDB_MOVIE_URL = f"{TMDB_BASE_URL}/movie/{{movie_id}}"

# TMDB serves images from a separate CDN. `w500` is a good balance between
# quality and Telegram's upload limits.
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Network timeouts (seconds). Without these a hung socket would pin a worker
# thread forever.
TMDB_TIMEOUT = 10

# How many rows /list returns.
LIST_LIMIT = 10

# Telegram caption limit is 1024 characters. We truncate the plot so the
# caption never gets rejected by the API.
MAX_PLOT_CHARS = 600

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx logs every request at INFO, which is noisy for a long-running bot.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("filmstash")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
# The Supabase client is created once at import time and reused. It is
# thread-safe for our usage pattern (one request per call, no shared cursors).
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:  # pragma: no cover - only hit on bad credentials
        logger.exception("Failed to initialise the Supabase client.")
else:
    logger.warning(
        "SUPABASE_URL / SUPABASE_KEY are not set - database features are disabled."
    )


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class MovieNotFoundError(Exception):
    """Raised when TMDB returns no usable match for a title."""


class ExternalServiceError(Exception):
    """Raised when TMDB or Supabase fails for an infrastructure reason."""


# ---------------------------------------------------------------------------
# TMDB helpers (synchronous - always call via asyncio.to_thread)
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
        return response.json()
    except ValueError as exc:
        raise ExternalServiceError("TMDB returned a malformed response.") from exc


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


# ---------------------------------------------------------------------------
# Supabase helpers (synchronous - always call via asyncio.to_thread)
# ---------------------------------------------------------------------------
def _require_supabase() -> Client:
    """Return the Supabase client or raise if it was never configured."""
    if supabase is None:
        raise ExternalServiceError(
            "Database is not configured. Set SUPABASE_URL and SUPABASE_KEY."
        )
    return supabase


def _supabase_key_role() -> Optional[str]:
    """
    Decode the `role` claim from the Supabase API key.

    Supabase keys are JWTs whose payload carries a `role` of either "anon" or
    "service_role". Knowing which one is configured lets us give a precise
    diagnosis instead of a cryptic RLS error. Returns None if the key is not a
    decodable JWT (e.g. the newer sb_secret_/sb_publishable_ formats).
    """
    if not SUPABASE_KEY:
        return None
    try:
        import base64
        import json

        parts = SUPABASE_KEY.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)  # restore JWT padding
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:
        return None


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
    return response.data[0]


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


def db_find_movie(user_id: int, title: str) -> Optional[dict[str, Any]]:
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
    a user may have logged the same title more than once. `/remove` means
    "take this movie out of my stash", which is the whole set, not one row.

    The `.eq("user_id", user_id)` filter is the multi-user isolation boundary -
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _escape(text: Optional[str]) -> str:
    """Escape user/API text for Telegram HTML parse mode."""
    return html.escape(text or "", quote=False)


def _truncate(text: Optional[str], limit: int = MAX_PLOT_CHARS) -> str:
    """Trim long plot text on a word boundary and add an ellipsis."""
    if not text:
        return "No plot summary available."
    clean = " ".join(text.split())  # collapse newlines/extra spaces
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "..."


def _format_rating(value: Optional[float]) -> str:
    """Render a numeric rating or a friendly placeholder."""
    return f"{value:.1f}/10" if value is not None else "N/A"


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
# Command handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start - explain what the bot does and how to use it."""
    user = update.effective_user
    name = _escape(user.first_name if user else "there")

    text = (
        f"🎬 <b>Welcome to FilmStash, {name}!</b>\n\n"
        "I keep a private, personal log of the movies you watch.\n\n"
        "<b>Commands</b>\n"
        f"• <code>/add {PLACEHOLDER_TITLE} {PLACEHOLDER_RATING}</code> — look a movie up and save it.\n"
        "    <i>Example:</i> <code>/add The Matrix 9</code>\n"
        "• <code>/list</code> — show your 10 most recent entries.\n"
        f"• <code>/search {PLACEHOLDER_TITLE}</code> — check if you already logged a movie.\n"
        "    <i>Example:</i> <code>/search Inception</code>\n"
        f"• <code>/remove {PLACEHOLDER_TITLE}</code> — delete a movie from your stash.\n"
        "    <i>Example:</i> <code>/remove The Matrix</code>\n\n"
        "Ratings are on a <b>1–10</b> scale. Your library is private to your "
        "Telegram account."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /add <Title> <Rating>

    The rating is the final whitespace-separated token; everything before it is
    the title. This lets multi-word titles work without quoting.
    """
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    raw_args = context.args or []

    # --- Validate the command shape ---------------------------------------
    if len(raw_args) < 2:
        await message.reply_text(
            f"⚠️ <b>Usage:</b> <code>/add {PLACEHOLDER_TITLE} {PLACEHOLDER_RATING}</code>\n"
            "Example: <code>/add Blade Runner 2049 10</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    rating_token = raw_args[-1]
    title = " ".join(raw_args[:-1]).strip()

    # Rating must be an integer 1-10. Reject "9.5", "abc", "0", "11".
    if not re.fullmatch(r"\d{1,2}", rating_token):
        await message.reply_text(
            "⚠️ The rating must be a whole number between <b>1</b> and <b>10</b>.\n"
            "Example: <code>/add Dune 8</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    personal_rating = int(rating_token)
    if not 1 <= personal_rating <= 10:
        await message.reply_text(
            "⚠️ The rating must be between <b>1</b> and <b>10</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not title:
        await message.reply_text(
            "⚠️ Please include a movie title, e.g. <code>/add Arrival 9</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Tell the user we are working (network calls take a moment) --------
    status = await message.reply_text(f"🔎 Looking up “{_escape(title)}”…")

    # --- 1. TMDB lookup (off the event loop) ------------------------------
    try:
        movie = await asyncio.to_thread(fetch_movie_from_tmdb, title)
    except MovieNotFoundError:
        await status.edit_text(
            f"❌ I couldn't find <b>{_escape(title)}</b> on TMDB.\n"
            "Try a different spelling or the original-language title.",
            parse_mode=ParseMode.HTML,
        )
        return
    except ExternalServiceError as exc:
        logger.warning("TMDB lookup failed for %r: %s", title, exc)
        await status.edit_text(
            "😕 The movie database is unavailable right now. Please try again in a moment."
        )
        return

    # --- 2. Persist to Supabase (off the event loop) ----------------------
    row = {
        "user_id": user.id,                       # Telegram numeric id
        "username": user.username,                # may be None
        "title": movie["title"],
        "personal_rating": personal_rating,
        "imdb_rating": movie["imdb_rating"],
        "poster_url": movie["poster_url"],
        "plot": movie["plot"],
    }

    try:
        await asyncio.to_thread(db_insert_movie, row)
    except ExternalServiceError as exc:
        logger.error("Insert failed for user %s: %s", user.id, exc)
        await status.edit_text(
            "😕 I found the movie but couldn't save it. Please try again later."
        )
        return

    # --- 3. Build the confirmation card -----------------------------------
    year = f" ({movie['release_year']})" if movie.get("release_year") else ""
    caption = (
        f"✅ <b>Saved to your stash</b>\n\n"
        f"🎬 <b>{_escape(movie['title'])}</b>{_escape(year)}\n"
        f"⭐ Your rating: <b>{personal_rating}/10</b>\n"
        f"🌍 TMDB rating: <b>{_format_rating(movie['imdb_rating'])}</b>\n\n"
        f"📖 <i>{_escape(_truncate(movie['plot']))}</i>"
    )

    # Prefer sending the poster as a photo with the caption. If there is no
    # poster (or Telegram rejects the URL), fall back to a plain text message.
    poster_url = movie.get("poster_url")
    if poster_url:
        try:
            await status.delete()
            await message.reply_photo(
                photo=poster_url,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            logger.warning("Could not send poster for %r; falling back to text.", title)

    await status.edit_text(caption, parse_mode=ParseMode.HTML)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list - the 10 most recent movies logged by the requesting user."""
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    try:
        rows = await asyncio.to_thread(db_list_movies, user.id, LIST_LIMIT)
    except ExternalServiceError:
        await message.reply_text(
            "😕 I couldn't read your library right now. Please try again later."
        )
        return

    if not rows:
        await message.reply_text(
            "📭 Your stash is empty.\n"
            f"Add your first movie with <code>/add {PLACEHOLDER_TITLE} {PLACEHOLDER_RATING}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["🍿 <b>Your last 10 movies</b>\n"]
    for index, row in enumerate(rows, start=1):
        created = (row.get("created_at") or "")[:10]  # YYYY-MM-DD
        lines.append(
            f"{index}. <b>{_escape(row.get('title'))}</b> — "
            f"you: <b>{row.get('personal_rating')}/10</b>, "
            f"TMDB: {_format_rating(row.get('imdb_rating'))} "
            f"<i>({_escape(created)})</i>"
        )

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /remove <Title> - delete this user's entry (or entries) for a movie.

    The title is the whole argument list, so multi-word titles work without
    quoting. Matching is case-insensitive and exact, mirroring /search, so a
    user can confirm what they are about to delete with /search first.
    """
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    title = " ".join(context.args or []).strip()
    if not title:
        await message.reply_text(
            f"⚠️ <b>Usage:</b> <code>/remove {PLACEHOLDER_TITLE}</code>\n"
            "Example: <code>/remove The Matrix</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- 1. Confirm the movie is actually in this user's stash -------------
    # Doing the lookup first lets us give a precise "not found" message and
    # avoids a delete that silently affects zero rows.
    try:
        existing = await asyncio.to_thread(db_find_movie, user.id, title)
    except ExternalServiceError:
        await message.reply_text(
            "😕 I couldn't read your library right now. Please try again later."
        )
        return

    if existing is None:
        await message.reply_text(
            f"🔍 You haven't logged <b>{_escape(title)}</b>, so there's nothing to remove.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- 2. Delete (off the event loop) ------------------------------------
    try:
        deleted = await asyncio.to_thread(db_delete_movie, user.id, title)
    except ExternalServiceError:
        await message.reply_text(
            "😕 I couldn't update your library right now. Please try again later."
        )
        return

    # The delete is scoped by user_id, so a non-empty result is guaranteed to
    # be this user's data. Guard anyway in case the row vanished concurrently.
    if not deleted:
        await message.reply_text(
            f"🔍 <b>{_escape(title)}</b> was already gone from your stash.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- 3. Confirm what was removed ---------------------------------------
    # Report the canonical TMDB title from the deleted row, not the raw input,
    # so the user sees exactly which movie left their library.
    removed_title = deleted[0].get("title") or title
    count = len(deleted)
    extra = f" <i>({count} entries)</i>" if count > 1 else ""

    await message.reply_text(
        f"🗑️ <b>Removed from your stash</b>\n\n"
        f"🎬 <b>{_escape(removed_title)}</b>{extra}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <Title> - has this user already logged the movie?"""
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    title = " ".join(context.args or []).strip()
    if not title:
        await message.reply_text(
            f"⚠️ <b>Usage:</b> <code>/search {PLACEHOLDER_TITLE}</code>\n"
            "Example: <code>/search Parasite</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        row = await asyncio.to_thread(db_find_movie, user.id, title)
    except ExternalServiceError:
        await message.reply_text(
            "😕 I couldn't search your library right now. Please try again later."
        )
        return

    if row is None:
        await message.reply_text(
            f"🔍 You haven't logged <b>{_escape(title)}</b> yet.\n"
            f"Add it with <code>/add {_escape(title)} {PLACEHOLDER_RATING}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    caption = (
        f"✅ <b>Found in your stash</b>\n\n"
        f"🎬 <b>{_escape(row.get('title'))}</b>\n"
        f"⭐ Your rating: <b>{row.get('personal_rating')}/10</b>\n"
        f"🌍 TMDB rating: <b>{_format_rating(row.get('imdb_rating'))}</b>\n\n"
        f"📖 <i>{_escape(_truncate(row.get('plot')))}</i>"
    )

    poster_url = row.get("poster_url")
    if poster_url:
        try:
            await message.reply_photo(
                photo=poster_url, caption=caption, parse_mode=ParseMode.HTML
            )
            return
        except Exception:
            logger.warning("Could not send poster for %r; falling back to text.", title)

    await message.reply_text(caption, parse_mode=ParseMode.HTML)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback for any unrecognised command."""
    if update.message:
        await update.message.reply_text(
            "🤔 I don't know that command. Try /start to see what I can do."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler. Logs the full traceback and, when possible, tells the
    user something went wrong without exposing internals.
    """
    logger.error("Unhandled exception while processing an update.", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "💥 Something went wrong on my side. Please try again."
            )
        except Exception:  # pragma: no cover - best effort only
            logger.exception("Failed to notify the user about an error.")


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------
def validate_config() -> None:
    """Fail fast with a clear message if required secrets are missing."""
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_KEY", SUPABASE_KEY),
            ("TMDB_API_KEY", TMDB_API_KEY),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in the values."
        )

    # --- Webhook sanity checks --------------------------------------------
    # Telegram refuses to register a webhook that is not HTTPS, so catching a
    # plain-http WEBHOOK_URL here turns a confusing API error at startup into a
    # clear message. Render always serves HTTPS, so this only trips on a typo.
    if WEBHOOK_ENABLED and not WEBHOOK_URL.startswith("https://"):
        raise SystemExit(
            f"WEBHOOK_URL must start with https:// (got {WEBHOOK_URL!r}). "
            "Telegram only delivers updates to HTTPS endpoints."
        )

    if WEBHOOK_ENABLED and not WEBHOOK_PATH.startswith("/"):
        raise SystemExit(
            f"WEBHOOK_PATH must start with '/' (got {WEBHOOK_PATH!r})."
        )

    if WEBHOOK_ENABLED and not 1 <= PORT <= 65535:
        raise SystemExit(f"PORT must be between 1 and 65535 (got {PORT}).")

    # Warn early if the wrong Supabase key type is configured. The `anon` key is
    # subject to Row Level Security, and schema.sql deliberately enables RLS
    # with no permissive policies, so every write would fail with error 42501.
    # Catching it here turns a confusing runtime failure into a clear warning.
    role = _supabase_key_role()
    if role == "anon":
        logger.warning(
            "SUPABASE_KEY holds the 'anon' key. Row Level Security will block "
            "all reads and writes. Switch to the 'service_role' key "
            "(Supabase Dashboard -> Project Settings -> API)."
        )
    elif role == "service_role":
        logger.info("Supabase key role: service_role (RLS bypassed, as intended).")
    elif role is not None:
        logger.warning("Unexpected Supabase key role: %s", role)


def build_application() -> Application:
    """Construct the Telegram Application with all handlers registered."""
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        # Concurrency: allow several updates to be processed in parallel. Our
        # blocking work runs in threads, so this is safe and keeps the bot
        # responsive under load.
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("search", cmd_search))
    application.add_handler(CommandHandler("remove", cmd_remove))
    # Catch-all for unknown slash commands.
    application.add_handler(
        CommandHandler(
            [
                "help", "delete", "stats", "random", "top",
                "export", "settings", "about",
            ],
            cmd_unknown,
        )
    )
    application.add_error_handler(on_error)

    return application


def main() -> None:
    """
    Entry point: validate config, build the app, then start either the webhook
    server (production) or the long-polling loop (local development).

    The mode is chosen purely from the environment: set WEBHOOK_URL and the bot
    serves Telegram's updates over HTTPS; leave it unset and the bot polls.
    """
    validate_config()

    application = build_application()

    if WEBHOOK_ENABLED:
        # --- Webhook mode (Render and other PaaS hosts) --------------------
        # `drop_pending_updates` discards messages that arrived while the bot
        # was offline, avoiding a burst of stale commands on each deploy.
        #
        # `listen`/`port` bind the internal HTTP server. Render terminates TLS
        # at its edge and forwards plain HTTP to this port, so we bind 0.0.0.0
        # on the injected PORT and let Render handle HTTPS.
        #
        # `url_path` is the local route; `webhook_url` is the full public URL
        # Telegram is told to POST to. They must agree on the path, which is why
        # both are derived from WEBHOOK_PATH.
        logger.info(
            "Starting FilmStash bot in WEBHOOK mode on %s:%s%s (public: %s%s)…",
            WEBHOOK_LISTEN,
            PORT,
            WEBHOOK_PATH,
            WEBHOOK_URL,
            WEBHOOK_PATH,
        )
        application.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        return

    # --- Polling mode (local development) ---------------------------------
    # run_polling blocks and manages its own asyncio loop.
    logger.info("Starting FilmStash bot in POLLING mode (no WEBHOOK_URL set)…")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
