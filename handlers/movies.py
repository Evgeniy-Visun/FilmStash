"""
FilmStash Bot - movie management commands.

Handlers here stay lean: read the user's input, call a service function, format
the result with ``utils.formatters``, and send the Telegram response. All
blocking work (TMDB, Supabase) is dispatched with ``asyncio.to_thread`` so the
event loop never stalls.
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import LIST_LIMIT
from services.supabase_client import (
    db_delete_movie,
    db_find_movie,
    db_insert_movie,
    db_list_movies,
)
from services.tmdb_client import (
    ExternalServiceError,
    MovieNotFoundError,
    fetch_movie_from_tmdb,
)
from utils.formatters import (
    PLACEHOLDER_RATING,
    PLACEHOLDER_TITLE,
    escape,
    format_movie_card,
    format_stash_list,
)

logger = logging.getLogger("filmstash.handlers.movies")


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
    status = await message.reply_text(f"🔎 Looking up “{escape(title)}”…")

    # --- 1. TMDB lookup (off the event loop) ------------------------------
    try:
        movie = await asyncio.to_thread(fetch_movie_from_tmdb, title)
    except MovieNotFoundError:
        await status.edit_text(
            f"❌ I couldn't find <b>{escape(title)}</b> on TMDB.\n"
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
    caption = format_movie_card(
        title=movie["title"],
        personal_rating=personal_rating,
        imdb_rating=movie["imdb_rating"],
        plot=movie["plot"],
        release_year=movie.get("release_year"),
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

    await message.reply_text(
        format_stash_list(rows, LIST_LIMIT), parse_mode=ParseMode.HTML
    )


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
            f"🔍 You haven't logged <b>{escape(title)}</b>, so there's nothing to remove.",
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
            f"🔍 <b>{escape(title)}</b> was already gone from your stash.",
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
        f"🎬 <b>{escape(removed_title)}</b>{extra}",
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
            f"🔍 You haven't logged <b>{escape(title)}</b> yet.\n"
            f"Add it with <code>/add {escape(title)} {PLACEHOLDER_RATING}</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    caption = format_movie_card(
        title=row.get("title"),
        personal_rating=row.get("personal_rating"),
        imdb_rating=row.get("imdb_rating"),
        plot=row.get("plot"),
        heading="Found in your stash",
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
