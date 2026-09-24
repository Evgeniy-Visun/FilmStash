"""
FilmStash Bot - configuration.

Single source of truth for every environment variable the bot reads. Importing
this module loads a local ``.env`` file (if present) and exposes the values as
module-level constants, so no other module ever has to touch ``os.environ``.

Design notes
------------
* ``load_dotenv(override=False)`` means real environment variables (set by
  Render, Docker, a shell, ...) win over the ``.env`` file. That is the
  behaviour you want in production.
* Nothing here imports from ``handlers`` or ``services`` - ``config`` sits at
  the bottom of the dependency graph and must stay import-free of the rest of
  the project to avoid circular imports.
* ``validate_config()`` is called once at startup by ``main.py``. It fails fast
  with an actionable message instead of letting the bot crash later with a
  cryptic API error.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------
# `override=False` keeps real environment variables authoritative over `.env`.
load_dotenv(override=False)

logger = logging.getLogger("filmstash.config")

# ---------------------------------------------------------------------------
# Required secrets
# ---------------------------------------------------------------------------
# The Telegram token issued by @BotFather.
TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")

# Supabase project URL + service_role key (the bot's database).
SUPABASE_URL: str | None = os.getenv("SUPABASE_URL")
SUPABASE_KEY: str | None = os.getenv("SUPABASE_KEY")

# TMDB v3 API key, used to enrich every logged movie.
TMDB_API_KEY: str | None = os.getenv("TMDB_API_KEY")

# ---------------------------------------------------------------------------
# Webhook / hosting
# ---------------------------------------------------------------------------
# Two run modes are supported:
#
#   * POLLING (default, local development) - the bot calls Telegram's
#     getUpdates in a loop. No public URL needed.
#
#   * WEBHOOK (production, e.g. Render) - Telegram pushes each update to a
#     public HTTPS endpoint that we host.
#
# Webhook mode activates automatically when WEBHOOK_URL is set, so the same
# code runs locally and in production with no flags to remember.
WEBHOOK_URL: str | None = os.getenv("WEBHOOK_URL")

# The port the HTTP server binds to. Render injects PORT at runtime and routes
# its public HTTPS traffic to it, so we must honour it. 10000 is Render's
# documented default and the fallback used when PORT is unset or malformed.
def _parse_port(raw: str | None, default: int = 10000) -> int:
    """Parse PORT into an int, falling back to ``default`` when unusable."""
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("PORT=%r is not an integer; falling back to %s.", raw, default)
        return default


PORT: int = _parse_port(os.getenv("PORT"), 10000)

# The interface to bind. 0.0.0.0 is required inside a container/host so the
# platform's router can reach the process.
WEBHOOK_LISTEN: str = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")

# Optional shared secret. When set, Telegram sends it in the
# X-Telegram-Bot-Api-Secret-Token header and python-telegram-bot rejects any
# request whose header does not match.
WEBHOOK_SECRET: str | None = os.getenv("WEBHOOK_SECRET")

# True when a public URL is configured, i.e. we should run in webhook mode.
WEBHOOK_ENABLED: bool = bool(WEBHOOK_URL)

# ---------------------------------------------------------------------------
# TMDB endpoints / tuning
# ---------------------------------------------------------------------------
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_SEARCH_URL = f"{TMDB_BASE_URL}/search/movie"
TMDB_MOVIE_URL = f"{TMDB_BASE_URL}/movie/{{movie_id}}"

# TMDB serves images from a separate CDN. `w500` balances quality and size.
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Network timeout (seconds). Without this a hung socket would pin a worker
# thread forever.
TMDB_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Application tuning
# ---------------------------------------------------------------------------
# How many rows /list returns.
LIST_LIMIT = 10

# Telegram caption limit is 1024 characters. We truncate the plot so the
# caption never gets rejected by the API.
MAX_PLOT_CHARS = 600

# Path to the Mini App / web dashboard served as a static file.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INDEX_HTML = os.path.join(WEB_DIR, "index.html")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_config() -> None:
    """
    Fail fast with a clear message if required secrets are missing.

    Raises
    ------
    SystemExit
        When a critical variable is absent or a webhook value is malformed.
    """
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", TOKEN),
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
    # clear message.
    if WEBHOOK_ENABLED and WEBHOOK_URL is not None and not WEBHOOK_URL.startswith("https://"):
        raise SystemExit(
            f"WEBHOOK_URL must start with https:// (got {WEBHOOK_URL!r}). "
            "Telegram only delivers updates to HTTPS endpoints."
        )

    if WEBHOOK_ENABLED and not 1 <= PORT <= 65535:
        raise SystemExit(f"PORT must be between 1 and 65535 (got {PORT}).")

    # Warn early if the wrong Supabase key type is configured. The `anon` key is
    # subject to Row Level Security, and schema.sql deliberately enables RLS
    # with no permissive policies, so every write would fail with error 42501.
    role = supabase_key_role()
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


def supabase_key_role() -> str | None:
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
        role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
        return str(role) if role is not None else None
    except Exception:
        return None
