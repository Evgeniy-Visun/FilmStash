"""
FilmStash Bot - inline keyboard callbacks and pagination.

Handles ``CallbackQuery`` updates produced by inline buttons. The callback data
format is ``<action>:<arg>`` (e.g. ``list:2``), which keeps the payload short
enough for Telegram's 64-byte limit while remaining easy to extend.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import LIST_LIMIT
from services.supabase_client import db_list_movies
from services.tmdb_client import ExternalServiceError
from utils.formatters import escape, format_rating

logger = logging.getLogger("filmstash.handlers.callbacks")

# How many movies to show per page in the paginated list view.
PAGE_SIZE = 5


def _build_page(rows: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Render one page of the user's stash plus its navigation keyboard.

    Returns a ``(text, keyboard)`` tuple; ``keyboard`` is None when there is
    only a single page.
    """
    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * PAGE_SIZE
    chunk = rows[start : start + PAGE_SIZE]

    lines = [f"🍿 <b>Your stash</b> — page {page}/{total_pages}\n"]
    for index, row in enumerate(chunk, start=start + 1):
        created = (row.get("created_at") or "")[:10]
        lines.append(
            f"{index}. <b>{escape(row.get('title'))}</b> — "
            f"you: <b>{row.get('personal_rating')}/10</b>, "
            f"TMDB: {format_rating(row.get('imdb_rating'))} "
            f"<i>({escape(created)})</i>"
        )

    if total_pages == 1:
        return "\n".join(lines), None

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"list:{page + 1}"))

    return "\n".join(lines), InlineKeyboardMarkup([nav])


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route an inline button press.

    Supported actions
    -----------------
    ``list:<page>``
        Show page ``<page>`` of the user's stash.
    """
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    # Always acknowledge, otherwise the client shows a spinner for ~30s.
    await query.answer()

    data = query.data or ""
    action, _, arg = data.partition(":")

    if action != "list":
        logger.debug("Unhandled callback action: %r", data)
        return

    try:
        page = int(arg) if arg else 1
    except ValueError:
        page = 1

    try:
        rows = await asyncio.to_thread(db_list_movies, user.id, LIST_LIMIT * 10)
    except ExternalServiceError:
        await query.edit_message_text(
            "😕 I couldn't read your library right now. Please try again later."
        )
        return

    if not rows:
        await query.edit_message_text(
            "📭 Your stash is empty. Add a movie with /add first.",
            parse_mode=ParseMode.HTML,
        )
        return

    text, keyboard = _build_page(rows, page)
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
