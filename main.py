"""
FilmStash Bot - entry point.

Responsibilities are deliberately minimal: configure logging, validate the
environment, build the Telegram ``Application``, register handlers, then start
either the webhook server (production / Render) or the long-polling loop
(local development).

Everything else lives in its own module:

* ``config``    - environment loading and validation
* ``handlers``  - command / callback handlers and their registration
* ``services``  - TMDB and Supabase access
* ``utils``     - formatting helpers
* ``web``       - the Mini App dashboard served as a static file
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

import config
from handlers import register_all_handlers

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


def build_application() -> Application:
    """Construct the Telegram Application with all handlers registered."""
    # validate_config() has already guaranteed TOKEN is set; the assert narrows
    # the type for static checkers without changing runtime behaviour.
    assert config.TOKEN is not None
    application = (
        ApplicationBuilder()
        .token(config.TOKEN)
        # Concurrency: allow several updates to be processed in parallel. Our
        # blocking work runs in threads, so this is safe and keeps the bot
        # responsive under load.
        .concurrent_updates(True)
        .build()
    )

    register_all_handlers(application)
    return application


def main() -> None:
    """
    Entry point: validate config, build the app, then start either the webhook
    server (production) or the long-polling loop (local development).

    The mode is chosen purely from the environment: set WEBHOOK_URL and the bot
    serves Telegram's updates over HTTPS; leave it unset and the bot polls.
    """
    config.validate_config()

    application = build_application()

    if config.WEBHOOK_ENABLED:
        # --- Webhook mode (Render and other PaaS hosts) --------------------
        # `drop_pending_updates` discards messages that arrived while the bot
        # was offline, avoiding a burst of stale commands on each deploy.
        #
        # `listen`/`port` bind the internal HTTP server. Render terminates TLS
        # at its edge and forwards plain HTTP to this port, so we bind 0.0.0.0
        # on the injected PORT and let Render handle HTTPS.
        #
        # `url_path` is the local route; `webhook_url` is the full public URL
        # Telegram is told to POST to. Both are derived from the token so the
        # path is unguessable and the two always agree.
        # validate_config() guarantees both values are present in this branch.
        assert config.TOKEN is not None
        assert config.WEBHOOK_URL is not None
        logger.info(
            "Starting FilmStash bot in WEBHOOK mode on %s:%s/%s (public: %s/%s)…",
            config.WEBHOOK_LISTEN,
            config.PORT,
            config.TOKEN,
            config.WEBHOOK_URL,
            config.TOKEN,
        )
        application.run_webhook(
            listen=config.WEBHOOK_LISTEN,
            port=config.PORT,
            url_path=config.TOKEN,
            webhook_url=f"{config.WEBHOOK_URL}/{config.TOKEN}",
            secret_token=config.WEBHOOK_SECRET,
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
