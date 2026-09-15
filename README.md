# 🎬 FilmStash Bot

A personal movie tracker for Telegram. Log what you watch, rate it 1–10, and
the bot enriches every entry with metadata from **TMDB** and stores it in
**Supabase** (cloud PostgreSQL).

Each user's library is private — every query is scoped by the Telegram numeric
`user_id`.

---

## Features

| Command | Description |
|---|---|
| `/start` | Welcome message and usage help. |
| `/add <Title> <Rating>` | Looks the title up on TMDB, saves it, replies with a poster card. |
| `/list` | The 10 most recent movies **you** logged. |
| `/search <Title>` | Checks whether **you** already logged a movie and shows your rating + plot. |

---

## Multiple users

The bot is **multi-user out of the box**. Share its link
(`https://t.me/YourBotUsername`) and anyone can use it — no code changes, no
extra configuration.

Each person's library is isolated automatically:

| Action | What happens |
|---|---|
| Someone else sends `/start` | They get the welcome message. |
| They send `/add Inception 9` | Stored under **their** Telegram ID. |
| They send `/list` | Shows only **their** movies. |
| They send `/search Inception` | Finds only **their** entry. |
| You send `/list` | Your library is unaffected by their activity. |

### The security boundary

Because the bot connects with the `service_role` key, **the bot's code is the
security boundary** — not the database. Isolation depends entirely on every
query filtering by `user_id`.

> ⚠️ **When adding new commands:** never query the `movies` table without a
> `.eq("user_id", user_id)` clause. A single missing filter would expose one
> user's library to another.

### Adding a non-Telegram client (web / mobile / CLI)

If you later want a second client talking to the same database, do **not** give
it the `service_role` key. Use the `anon` key with RLS policies instead:

```sql
create policy "users read own movies"
    on public.movies for select
    to authenticated
    using (auth.uid() = user_id);

create policy "users insert own movies"
    on public.movies for insert
    to authenticated
    with check (auth.uid() = user_id);
```

The complication: these policies compare against Supabase's `auth.uid()`, but
`user_id` currently holds a **Telegram** ID. Bridging the two identity systems
requires a mapping table and a way to authenticate Telegram users against
Supabase. That is the main design problem to solve before adding a second
client — the policies themselves are the easy part.

---

## Project layout

```
filmstash-bot/
├── main.py            # The bot (async, fully commented)
├── schema.sql         # Supabase table + indexes + RLS
├── requirements.txt   # Python dependencies
├── .env.example       # Template for your secrets
└── README.md
```

---

## ⚠️ Prerequisites — read this first

The bot will **not** work until all four of these are done. Three are accounts
you must create; the fourth is a **manual database step** that no script can do
for you.

| # | Prerequisite | Who does it |
|---|---|---|
| 1 | Telegram bot token from @BotFather | You (manual) |
| 2 | TMDB API key | You (manual) |
| 3 | Supabase project created | You (manual) |
| 4 | **`schema.sql` executed in the Supabase SQL Editor** | **You (manual)** |

**Step 4 is the one people miss.** [`schema.sql`](schema.sql:1) is a *file you
run*, not something that applies itself. Until you paste it into the Supabase
SQL Editor and click **Run**, the `movies` table does not exist and every
`/add` fails with:

```
PGRST205: Could not find the table 'public.movies' in the schema cache
```

If you see that error, jump to [Setup step 3](#3-create-the-supabase-database).

---

## Setup

### 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot`, follow the prompts, and copy the **HTTP API token**.

### 2. Get a TMDB API key

1. Create an account at [themoviedb.org](https://www.themoviedb.org/).
2. Go to **Settings → API** and request a key (choose the *Developer* plan).
3. Copy the **API Key (v3 auth)**.

### 3. Create the Supabase database

1. Create a project at [supabase.com](https://supabase.com/).
2. Open **SQL Editor → New query**, paste the contents of [`schema.sql`](schema.sql:1), and click **Run**.
   You should see `Success. No rows returned`.
3. **Verify the table was created.** Run this in the same SQL Editor:

   ```sql
   select column_name, data_type, is_nullable
   from information_schema.columns
   where table_schema = 'public' and table_name = 'movies'
   order by ordinal_position;
   ```

   You should get **9 rows**: `id`, `user_id`, `username`, `title`,
   `personal_rating`, `imdb_rating`, `poster_url`, `plot`, `created_at`.
   If you get 0 rows, the script did not run — repeat step 2.
4. From **Project Settings → API**, copy:
   - **Project URL** → `SUPABASE_URL`
   - **`service_role` key** → `SUPABASE_KEY`

> ⚠️ The `service_role` key bypasses Row Level Security. Keep it server-side
> only — never ship it in a browser or mobile app.

> 💡 Make sure the `SUPABASE_URL` points at the **same project** where you ran
> the SQL. The project ref is the subdomain, e.g.
> `https://abcdefghijklm.supabase.co` → project `abcdefghijklm`. Running the
> schema in one project and pointing the bot at another produces the exact same
> `PGRST205` error.

### 4. Configure the environment

```cmd
copy .env.example .env
```

Then edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AA...
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_KEY=eyJhbGciOi...
TMDB_API_KEY=abcdef123456...
```

### 5. Install and run

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On macOS/Linux use `source .venv/bin/activate` instead.

You should see `Starting FilmStash bot…` and then `Application started`. Open
your bot in Telegram and send `/start`.

---

## Usage examples

```
/add The Matrix 9
/add Blade Runner 2049 10
/list
/search Inception
```

The rating is always the **last** token, so multi-word titles work without
quotes.

---

## How it works

```
Telegram  ──►  main.py  ──►  TMDB API      (poster, score, plot)
                   │
                   └──────►  Supabase      (movies table)
```

- **Async everywhere.** `python-telegram-bot` v20+ runs on `asyncio`. The
  blocking `requests` and `supabase` calls are dispatched with
  `asyncio.to_thread`, so a slow network call never freezes the bot for other
  users.
- **Multi-user isolation.** Every read and write filters on `user_id`
  (the Telegram numeric id). `username` is stored for display only and is never
  used for authorization.
- **Graceful degradation.** Missing TMDB matches, invalid ratings, malformed
  commands and infrastructure failures each produce a friendly message; the
  technical detail goes to the log.

---

## Database schema

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Primary key, `gen_random_uuid()`. |
| `user_id` | `bigint` | Telegram numeric id. **NOT NULL.** |
| `username` | `text` | Telegram `@username` (nullable). |
| `title` | `text` | **NOT NULL.** |
| `personal_rating` | `integer` | **NOT NULL**, `CHECK (1..10)`. |
| `imdb_rating` | `numeric(3,1)` | TMDB `vote_average`, nullable. |
| `poster_url` | `text` | Full TMDB CDN URL. |
| `plot` | `text` | TMDB overview. |
| `created_at` | `timestamptz` | Defaults to `now()`. |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing required environment variables` | You didn't create `.env` or a key is blank. |
| `TMDB rejected the API key (401)` | Wrong `TMDB_API_KEY`; use the **v3** key. |
| `Database is not configured` | `SUPABASE_URL` / `SUPABASE_KEY` missing. |
| `Could not save the movie` | `schema.sql` wasn't run, or the key lacks write access. |
| `PGRST205: Could not find the table 'public.movies'` | `schema.sql` was never run. See [Setup step 3](#3-create-the-supabase-database). |
| `42501: new row violates row-level security policy` | You're using the `anon` key. Switch to `service_role`. See below. |
| Bot silent | Check the token, and that only one instance is polling. |
| `Conflict: terminated by other getUpdates request` | A second bot instance is polling. Kill stray `python.exe` processes. |
| `RuntimeError: There is no current event loop in thread 'MainThread'` | See below. |

### `RuntimeError: There is no current event loop in thread 'MainThread'`

This is a **Python version mismatch**, not a bug in the bot.

Python 3.14 removed the implicit event loop that `asyncio.get_event_loop()`
used to create on demand. `python-telegram-bot` releases up to 21.11 still call
that API inside `run_polling()`, so on Python 3.13+ they crash immediately
after printing `Starting FilmStash bot…`.

Fix — upgrade the library (the pinned version in `requirements.txt` already
does this):

```cmd
.venv\Scripts\python.exe -m pip install --upgrade "python-telegram-bot>=22.0"
```

Confirm your versions:

```cmd
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe -m pip show python-telegram-bot
```

You need **Python 3.10+** and **python-telegram-bot 22.x** on Python 3.13+.
If you must stay on an older PTB, use Python 3.12 instead.

### `42501: new row violates row-level security policy for table "movies"`

You are using the **`anon`** key instead of the **`service_role`** key.

[`schema.sql`](schema.sql:78) enables Row Level Security with **no permissive
policies**. That is deliberate: it means a leaked `anon` key cannot read or
write the table. The bot is designed to connect with `service_role`, which
bypasses RLS entirely.

**Fix** — swap the key:

1. Supabase Dashboard → **Project Settings** → **API**
2. Under **Project API keys**, reveal and copy the **`service_role`** key
3. Replace `SUPABASE_KEY` in `.env`
4. Restart the bot

The bot now detects this at startup and logs a warning, so you'll see it before
the first `/add`:

```
WARNING | filmstash | SUPABASE_KEY holds the 'anon' key. Row Level Security
will block all reads and writes. Switch to the 'service_role' key.
```

To check which key you have at any time:

```cmd
.venv\Scripts\python.exe -c "import main; print(main._supabase_key_role())"
```

> ⚠️ The `service_role` key bypasses all security. Keep it server-side only.

<details>
<summary>Alternative: keep the <code>anon</code> key (not recommended)</summary>

You can instead add policies permitting the `anon` role. This is **less
secure** — the `anon` key is designed to be public, so these policies make your
table world-writable to anyone holding it.

```sql
create policy "anon can read movies"
    on public.movies for select
    to anon using (true);

create policy "anon can insert movies"
    on public.movies for insert
    to anon with check (true);
```

</details>
