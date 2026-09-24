"""
FilmStash Bot - /start, /help and the main menu.

The main menu exposes the Mini App dashboard (``web/index.html``) through an
``InlineKeyboardButton`` with a ``web_app`` payload. Telegram only renders that
button when the URL is HTTPS, so it is attached only when ``WEBHOOK_URL`` is
configured; locally the menu falls back to a plain help message.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import WEBHOOK_URL
from utils.formatters import PLACEHOLDER_RATING, PLACEHOLDER_TITLE, escape

logger = logging.getLogger("filmstash.handlers.start")


def _help_text(name: str) -> str:
    """Build the shared welcome/help body."""
    return (
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
        "Ratings are on a <b>1–10</b> scale. Your library is private to your "  # noqa: RUF001
        "Telegram account."
    )


def _main_menu() -> InlineKeyboardMarkup | None:
    """
    Build the main menu keyboard.

    Returns None when no HTTPS base URL is configured, because Telegram rejects
    a ``web_app`` button whose URL is not HTTPS.
    """
    if not WEBHOOK_URL:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 Open FilmStash Dashboard",
                    web_app=WebAppInfo(url=WEBHOOK_URL),
                )
            ]
        ]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start - explain what the bot does and how to use it."""
    message = update.message
    if message is None:
        return

    user = update.effective_user
    name = escape(user.first_name if user else "there")

    await message.reply_text(
        _help_text(name),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_menu(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help - alias for /start."""
    await cmd_start(update, context)
