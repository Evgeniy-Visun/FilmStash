"""
FilmStash Bot - handler registration.

``register_all_handlers(app)`` is the single entry point ``main.py`` calls to
wire every command, callback and error handler onto the Application. Keeping
the wiring here means ``main.py`` stays a thin bootstrap file.

Import direction: ``handlers`` -> ``services`` / ``utils`` / ``config``.
``services`` never imports ``handlers``, so there is no cycle.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from handlers.callbacks import on_callback_query
from handlers.movies import (
    cmd_add,
    cmd_list,
    cmd_remove,
    cmd_search,
    cmd_unknown,
)
from handlers.start import cmd_help, cmd_start

logger = logging.getLogger("filmstash.handlers")

# Slash commands that are recognised but not implemented; they fall through to
# the friendly "unknown command" reply instead of Telegram's default silence.
UNKNOWN_COMMANDS = [
    "delete",
    "stats",
    "random",
    "top",
    "export",
    "settings",
    "about",
]


async def on_error(update: object, context) -> None:
    """
    Global error handler. Logs the full traceback and, when possible, tells the
    user something went wrong without exposing internals.
    """
    logger.error(
        "Unhandled exception while processing an update.", exc_info=context.error
    )

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "💥 Something went wrong on my side. Please try again."
            )
        except Exception:  # pragma: no cover - best effort only
            logger.exception("Failed to notify the user about an error.")


def register_all_handlers(app: Application) -> None:
    """Attach every command, callback and error handler to ``app``."""
    # --- Commands ---------------------------------------------------------
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("remove", cmd_remove))

    # Catch-all for recognised-but-unimplemented slash commands.
    app.add_handler(CommandHandler(UNKNOWN_COMMANDS, cmd_unknown))

    # --- Inline keyboards -------------------------------------------------
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # --- Errors -----------------------------------------------------------
    app.add_error_handler(on_error)

    logger.info("Registered all handlers.")
