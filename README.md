# search-vac-tg

Monitors public Telegram channels with freelance job posts, filters them for one developer profile and sends only the fitting jobs to your private Telegram bot.

Documentation: [docs/QUICK_START.md](docs/QUICK_START.md) (quick start, Russian) | [docs/architecture.md](docs/architecture.md) | [docs/PROMPTS.md](docs/PROMPTS.md) (JEV questions and OpenRouter prompts) | [docs/DEPLOY.md](docs/DEPLOY.md) (deploy, update, rollback, monitoring, backups; Russian) | [README_RU.md](README_RU.md) (Russian version)

## What it does

A Telethon user session reads public channels (default: `@FreelanceBay`). Every post is normalized, de-duplicated and passed through a cheap deterministic rule filter and then through JEV, a cheap typed-decision model that answers ACCEPT / REJECT / REVIEW. The expensive OpenRouter LLM is called only for REVIEW (or when JEV failed). Fitting jobs get a contact resolved (never paying, never joining) and are stored in SQLite and sent as a card to you by an aiogram bot with buttons: open the post, contact, feedback, and "make a reply draft" (you send the reply yourself; nothing is sent automatically).

```
channels (public, config/channels.yaml)
   |
   v
Telethon (new-message events + periodic poll)
   |
   v
normalization -> dedup (hash + fuzzy) -> rules (config/filter.yaml)
                                            |
                              reject <------+------> pass
                                                       |
                                                       v
                                                     JEV (typed decision API)
                                     ACCEPT / REJECT / REVIEW  (or JEV error)
                                       |        |        |
                                       |        |        +--> OpenRouter review (only for REVIEW / JEV error)
                                       |        v                     |
                                       |     dropped                  |
                                       v                              v
                                    final decision (fit score >= NOTIFY_SCORE) <-+
                                                       |
                                                       v
                                            contact resolver
                                                       |
                                                       v
                                                    SQLite
                                                       |
                                                       v
                                        aiogram bot -> job card to the owner
```

## Stack

- Python 3.12, asyncio
- Telethon (MTProto user session), aiogram 3 (notification bot)
- SQLAlchemy 2 (async) + aiosqlite (SQLite)
- httpx (JEV and OpenRouter HTTP), pydantic, PyYAML, python-dotenv, RapidFuzz (fuzzy dedup)
- pypdf, python-docx (profile builder)
- pytest, pytest-asyncio, respx (tests)
- Docker / docker compose

## Project structure

```
search-vac-tg/
  src/
    main.py                     entry point: monitor + bot in one event loop; build_pipeline()
    config.py                   settings from .env + config.ini, channels loader, logging setup
    health.py                   heartbeat file + Docker healthcheck (python -m src.health check)
    alerts.py                   owner alerts about the bot's own health (rate-limited)
    backup.py                   daily and manual backups of the DB and the Telegram session
    telegram/
      client.py                 Telethon wrapper: session, channel reading, callback click, refetch
      listener.py               NewMessage handler + poll loop + retry loop
      parser.py                 post normalization, contact/budget/button extraction
      contact_resolver.py       contact resolution and paid-contact detection
    filtering/
      pipeline.py               the whole processing flow, statuses, retries
      rules.py                  keyword pre-filter (config/filter.yaml)
      deduplicator.py           hash + RapidFuzz duplicate detection
      scorer.py                 final fit score from rules + JEV + OpenRouter
    jev/
      client.py                 HTTP client of the JEV System One API
      classifier.py             the three typed questions and guard rules
      schemas.py                JevDecision / JevError / JevUsage
    llm/
      openrouter.py             OpenRouter chat client (review, reply draft, profile polish)
      prompts.py                OpenRouter prompts
      schemas.py                ReviewResult / LlmError / LlmUsage
    profile/
      builder.py                builds data/profile.json and data/profile.md from materials/
      loader.py                 loads the profile, compact form, provisional profile
      matcher.py                picks projects relevant to a job
    bot/
      bot.py                    NotifyBot: sends cards, runs the dispatcher
      handlers.py               feedback and reply-draft callbacks, owner-only middleware
      menu.py                   /start, /menu dashboard, settings, backups, channels, profile screens
      cards.py                  job card text (score bar, labels, HTML-escaped)
      keyboards.py              inline keyboard of a card
      stats_format.py           statistics text (Russian, sectioned) and job status labels
    db/
      database.py               async engine and session
      models.py                 ORM models and JobStatus
      repository.py             all DB queries
  config/
    channels.yaml               channels to monitor
    filter.yaml                 keyword lists of the rule filter
  config.ini                    non-secret constants
  materials/                    resume, portfolio, project READMEs for the profile
  scripts/
    telegram_login.py           interactive login, creates the session file
    rebuild_profile.py          rebuild the profile from materials/
    dry_run.py                  run the real pipeline on sample texts without Telegram
    stats.py                    print statistics from the DB
    restore_backup.py           list backups / restore the DB (and session) from a backup
  tests/                        pytest tests
  docs/                         QUICK_START.md, DEPLOY.md, PROMPTS.md, architecture.md, PROGRESS.md
  data/                         runtime files: app.db, telegram.session, profile.*, app.log (git-ignored)
  Dockerfile, docker-compose.yml
  requirements.txt, requirements-dev.txt
  ruff.toml                     linter settings
  .env.example                  template of secrets (real values go to .env)
```

## Installation and running on a VPS (Docker)

Everything runs on an Ubuntu VPS in Docker; the server needs only Docker and git. Step-by-step commands-only version (in Russian): [docs/QUICK_START.md](docs/QUICK_START.md). Server path: `/opt/search-vac-tg`, service name in `docker-compose.yml`: `search-vac-tg`.

1. Docker and the code:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo git clone https://github.com/Lightwell-bg/search-vac-tg.git /opt/search-vac-tg
   sudo chown -R "$USER":"$USER" /opt/search-vac-tg
   cd /opt/search-vac-tg
   ```
2. Secrets: `cp .env.example .env && nano .env`. See "Third-party services and API keys" below for where to get each key. Save in nano: Ctrl+O, Enter, Ctrl+X.
3. Data directories: `mkdir -p data materials && sudo chown -R 1000:1000 data && sudo chmod 700 data`. The container runs as uid 1000, so `data/` must be writable by it and closed to others.
4. Build: `sudo docker compose build`.
5. Check `.env` (expected `OK`):
   ```bash
   sudo docker compose run --rm search-vac-tg python -c "from src.config import load_settings; m=load_settings().missing_for_run(); print('OK' if not m else 'Не заполнено: '+', '.join(m))"
   ```
6. Telegram login (once; phone number, code from Telegram, 2FA password): `sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py`. The file `data/telegram.session` appears on the host (it is a secret).
7. Dry run (optional; real JEV/OpenRouter, no Telegram; writes `data/dry_run.db`): `sudo docker compose run --rm search-vac-tg python scripts/dry_run.py`.
8. Start: `sudo docker compose up -d`, then `sudo docker compose logs -f`: the log shows `monitoring @FreelanceBay`; send `/stats` in the bot.

Everyday commands (from `/opt/search-vac-tg`):

```bash
# update
sudo git pull && sudo docker compose up -d --build
# statistics (same as /stats in the bot)
sudo docker compose exec search-vac-tg python scripts/stats.py
# logs / restart / stop
sudo docker compose logs -f
sudo docker compose restart
sudo docker compose down
```

- Channels: manage in the bot (`/menu`). Filter: edit `config/filter.yaml` on the server (`nano`), then `sudo docker compose restart`.
- Profile: the ready `data/profile.json` and `data/profile.md` are tracked in git and arrive with `git clone`. Rebuild on the server only after you put your files into `/opt/search-vac-tg/materials` on the host (the directory is mounted into the container read-only, so it cannot be filled from inside): `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py && sudo docker compose restart`.
- Volumes (`docker-compose.yml`): `./data` (database, session, profile, logs; writable), `./config`, `./materials` and `./config.ini` (read-only). Secrets come from `.env` (`env_file`). The session file gets mode 0600 automatically. `.dockerignore` keeps `.claude/`, `materials/private/`, `*.session` and `data/` out of the image. Container logs are rotated (10 MB x 3). JEV needs no extra container.
- Backups: automatic, see "Backups" below.
- Network: only outbound HTTPS to `openrouter.ai` and Telegram is needed. No incoming ports.

## Deployment

Update, rollback, health checks, backups and restore, logs: [docs/DEPLOY.md](docs/DEPLOY.md) (Russian; server bash commands first, local PowerShell commands second). In short: `git pull` and `sudo docker compose up -d --build` on the server, then `sudo docker compose ps` must show `healthy`.

## Monitoring and alerts

- **Heartbeat and healthcheck**: the app writes `data/heartbeat.json` every minute (`config.ini [paths] heartbeat_file`). The Docker `HEALTHCHECK` (`python -m src.health check`) is unhealthy when the file is missing or older than 180 s, or when no channel poll has finished for `2 x poll interval + 10 min`. Check: `sudo docker compose ps` (state `healthy`) or `sudo docker compose exec search-vac-tg python -m src.health check` (exit code 0).
- **Status dashboard**: `/menu` shows notifications on/paused, active channels, the interval and time of the last poll (with the number of new posts), consecutive poll failures with the last error, the last backup and the alerts state.
- **Alerts** to the owner chat (toggle "🔔 Alerts" in "⚙️ Settings", applies live; `config.ini [alerts] enabled` is only the initial default): bot started / stopped, channel polling failed 3 times in a row, JEV or OpenRouter errors (5 within 30 minutes), a failed backup, a damaged database at start. Alerts of the same kind are sent at most once per hour (start and stop always).

## Backups

- Automatic daily backup of the SQLite database (a consistent online copy) and of the Telegram session file into `data/backups` (`config.ini [backup] dir`) at `[backup] hour` local time (`[ui] timezone`, default 04:00 Europe/Sofia). Files: `app-YYYYMMDD-HHMMSS.db`, `session-YYYYMMDD-HHMMSS.session`.
- In the bot: "⚙️ Settings -> 💾 Backups" shows the last backup, the last error and the 5 newest copies; "💾 Make a backup now" runs one immediately; "Keep last" 3 / 7 / 14 / 30 applies live (`[backup] keep` is only the initial default, 1..60). Old copies are rotated automatically. Available only for SQLite.
- The session file gives full access to the Telegram account: it is never sent to the chat; keep copies private and never commit them.
- Restore (server, bash): `sudo docker compose stop`, `sudo docker compose run --rm search-vac-tg python scripts/restore_backup.py --list`, then `sudo docker compose run --rm search-vac-tg python scripts/restore_backup.py data/backups/app-....db --force` (add `--session data/backups/session-....session` to restore the session too), then `sudo docker compose up -d`. The current database (and session, if restored) is first saved as a raw file copy (works even if it is corrupted) with its `-wal`/`-shm`/`-journal` files: `data/app.db.before-restore-<time>`. Copying backups off the server (PowerShell `scp`): [docs/DEPLOY.md](docs/DEPLOY.md).

## How JEV is used

JEV is the TypeSafe "System One" typed-decision API: instead of free text, it returns typed answers with real probabilities and confidence.

- Production call: `POST https://openrouter.ai/api/v1/systemone` with the model `~typesafe/jev-latest` (uses the same `OPENROUTER_API_KEY`; the URL is `base_url` from `config.ini` `[openrouter]` + `/systemone`). The model is set in `config.ini` `[jev] model`.
- One call per job with three typed questions: `decision` (choice: accept / reject / review), `fit` (score 0-3, converted to 0-100) and `category` (choice). The exact texts are in [docs/PROMPTS.md](docs/PROMPTS.md).
- Only the compact profile and the normalized job text (at most `[jev] max_text_chars` = 3000 characters) are sent.
- Guard rules (in `src/jev/classifier.py`): an ACCEPT or REJECT with confidence below `[jev] min_confidence` (0.70) becomes REVIEW; ACCEPT with fit below 1.5 of 3 becomes REVIEW; REJECT with fit 2.0 or more of 3 becomes REVIEW (inconsistent answers).
- Cost: about $0.00003 per job (estimate; the real cost is stored per call in the `jev_usage` table and shown in `/stats`).
- JEV is a remote HTTPS API: there is no local runtime and no extra container. The server only needs outbound HTTPS to `openrouter.ai`.
- Development tooling (not used by the app at runtime): `~/.claude/scripts/ojc/jev-route.sh` routes coding subtasks to models through the same JEV runtime.

### When OpenRouter is called and when it is not

Called:
- JEV returned REVIEW (including ACCEPT/REJECT downgraded by the guard rules);
- JEV failed (timeout, HTTP error, invalid answer) and `JEV_FALLBACK_TO_OPENROUTER=true` (route `JEV error → OpenRouter`);
- you press "Сделать отклик" in the bot (reply draft), and `scripts/rebuild_profile.py --llm` (profile polish).

Not called:
- rule filter rejected the post (too short, only negative words, no job signal);
- the post is a duplicate;
- JEV returned ACCEPT or REJECT with enough confidence;
- JEV failed and `JEV_FALLBACK_TO_OPENROUTER=false`: the job is parked as `jev_unavailable` and retried later.

## Fit score and thresholds

- `NOTIFY_SCORE` (default 65): minimal final score to send a job to you.
- `HIGH_FIT_SCORE` (default 80): from this score the card header is "🔥 Подходящий заказ", below it "✅ Возможно подходит".

Formula (`src/filtering/scorer.py`):
- rules bonus = `min(10, rules_score // 10)`, where rules score = `strong_positive hits * 25 + positive hits * 10 - negative hits * 5 - strong_negative hits * 15`, clamped to 0..100;
- JEV ACCEPT without OpenRouter: `score = min(100, jev_fit + bonus)`; accepted if `score >= NOTIFY_SCORE`;
- OpenRouter review used: `base = 0.7 * llm_fit + 0.3 * jev_fit` (just `llm_fit` if JEV failed), `score = min(100, base + bonus)`; accepted only if the LLM also says `should_notify=true` and `score >= NOTIFY_SCORE`.

## Contact resolver

Runs only for jobs already accepted as a fit. Order:

1. Payment scan of the whole keyboard first: a Buy button, a contact-looking button with a payment marker ("Контакт за 50 ⭐") or a payment/checkout link (`buy.stripe.com`, `boosty.to`, `/checkout`, `t.me/$...` ...) -> `paid_contact` immediately, even if the text also has a `@username`. Phrases like "бесплатно", "оплата не требуется", "free" are not payment markers.
2. Direct human contact in the text (username, t.me user link, email, phone) -> `direct`.
3. URL buttons: a link to a Telegram user -> `direct`; a bot, a website, a response form -> `external_contact_flow` (never `direct`).
4. One public callback button ("Получить контакт" style), only if the channel allows clicking (`click_callbacks`, see "Adding a channel"). The answer / edited post is scanned for payment again; a human contact in it -> `free`; only a web link or a bot -> `external_contact_flow`.

Statuses (`jobs.contact_status`):

| Status | Meaning |
|---|---|
| `direct` | human contact in the text (username, t.me user link, email, phone) or a URL button to a Telegram user |
| `free` | human contact received after a free callback click (from the answer or from the edited post) |
| `external_contact_flow` | contact goes through a bot, a website or a form (bot link / deep-link / url_auth button / web link) |
| `paid_contact` | any sign of payment on the contact path (payment scan runs first) |
| `contact_unknown` | callback buttons gave no contact, the click failed or was not allowed, a password button was not pressed, or there is no Telegram access |
| `missing` | no contact and no buttons |
| `resolving` | internal claim written before a button is pressed (see "Reliability"); never a final result |

Supported button types: `callback` (pressed once if it looks like a contact button, asks no password and the channel allows clicks), `url`, `buy` (treated as paid), `url_auth`, and bot deep-links (`t.me/bot?start=...`).

Paid markers: Telegram Stars, "звёзд", invoice, "оплат", "платн", "купить", "покупка", "subscription", "подписка", "premium", "тариф", "пополнить", "баланс", "недостаточно", "top up", "контакт за N", price patterns (`за 100 ₽/$/stars`), payment links (`t.me/$...`, `t.me/invoice/...`) and Buy buttons (full regex: `_PAYMENT_RE` in `src/telegram/contact_resolver.py`).

The service never pays, never joins channels or chats, never sends your 2FA password to any bot (a button that requires a password is never pressed; the contact becomes `contact_unknown`). Only one callback click per job (durable: a retry after a crash never presses it again), one at a time, `[telegram] contact_click_delay_sec` apart. A callback handler is the channel bot's server code and we cannot know what it does, so set `click_callbacks: false` for a channel whose bot charges credits or limits (see "Adding a channel").

## Reliability

- Job statuses: `new` (now retryable: a job interrupted by an exception or a crash is resumed), `notifying` (send claimed), `notify_uncertain` (a crash happened during the send: the card may have been delivered, it is never resent automatically; check the bot chat), plus the ones listed in [docs/architecture.md](docs/architecture.md).
- Durable claims before side effects: `contact_status='resolving'` is written before a callback button is pressed and `status='notifying'` before the card is sent. A retry that finds a claim never repeats the action.
- Orphan recovery: a message saved without a job (crash between saving and creating the job) is picked up by the retry loop after a 2 minute grace period.
- The poll cursor `last_message_id` moves only past posts that are stored durably; a post that cannot be stored stops the channel's batch and is fetched again on the next poll (after 3 failures in a row it is logged as `ERROR skipping poison message` and skipped). Channels that failed to resolve at start are re-resolved every retry interval.
- JEV and OpenRouter answers of any shape are validated: a malformed answer (wrong types, NaN, confidence outside 0..1, fit outside 0..3) is a JEV/OpenRouter error, never clamped and never a crash.

`SHOW_PAID_CONTACT` (default `false`): with `false` a fitting job with a paid contact gets the status `paid_skipped` and no card is sent; with `true` the card is sent with "⚠️ платный контакт".

## Third-party services and API keys

All secrets live only in `.env` (git-ignored). On the server: `cp .env.example .env && nano .env` (save: Ctrl+O, Enter, Ctrl+X). Get the keys in a browser/Telegram on any device, then paste them into `nano` on the server. Below is a detailed guide on where to get each key.

### 1. `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`

Needed so the service can read channels on behalf of your Telegram account.

1. Open https://my.telegram.org in a browser.
2. Enter the phone number of your account in international format (for example `+79161234567`) and click Next.
3. The code arrives in Telegram itself (the "Telegram" chat with a blue check mark), not by SMS. Enter it on the site.
4. Click **API development tools**.
5. If you have no application yet, fill in the form:
   - App title: `search-vac-tg`
   - Short name: `searchvactg` (5-32 characters, Latin letters/digits)
   - URL: leave empty
   - Platform: Desktop
   - Description: may be left empty

   Click **Create application**.
6. The page shows **App api_id** (a number, for example `12345678`) and **App api_hash** (a 32-character string).
7. Put them into `.env`:
   ```env
   TELEGRAM_API_ID=12345678
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   ```

If the form returns a bare "ERROR" with no explanation, this is a known issue of the Telegram site: turn off VPN/proxy, try another browser or incognito mode, and change App title and Short name.

**Never share `api_hash`: together with the session file it gives access to the account.**

### 2. Log in to the account (once)

```bash
cd /opt/search-vac-tg
sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py
```

The script asks for the phone number, the code from Telegram and the two-step verification password (if enabled). Afterwards `data/telegram.session` appears: it is a pass to your account and is never committed to git.

### 3. `NOTIFY_BOT_TOKEN`

A separate bot that sends you the vacancies.

1. Open https://t.me/BotFather and click Start.
2. Send `/newbot`.
3. Bot name (shown in the chat): anything, for example "My vacancies".
4. Username: Latin letters/digits/`_`, must end with `bot`, for example `vlad_vacancies_bot`. If it is taken, pick another one.
5. You receive a token like `1234567890:AAH...` (~46 characters). Copy it entirely.
6. Put it into `.env`:
   ```env
   NOTIFY_BOT_TOKEN=1234567890:AAHxxxxxxxx...
   ```
7. **You must** open your bot (`t.me/<username>`) and click Start: a bot cannot write first, and without this no notifications will arrive.

If the token leaks: BotFather -> `/revoke` -> choose the bot -> get a new token.

### 4. `OWNER_TELEGRAM_ID`

Your numeric ID; the bot answers only this user and sends notifications only to this user.

1. Open https://t.me/userinfobot and click Start.
2. It sends `Id: 123456789`.
3. Put it into `.env`:
   ```env
   OWNER_TELEGRAM_ID=123456789
   ```

### 5. `OPENROUTER_API_KEY` (used by both JEV and the OpenRouter model)

1. Sign up at https://openrouter.ai.
2. Top up the balance: https://openrouter.ai/settings/credits ($5 is enough: JEV costs about $0.00003 per vacancy, the OpenRouter model is called rarely).
3. Open https://openrouter.ai/keys -> Create Key -> name `search-vac-tg` (no limit needed).
4. Copy the key `sk-or-v1-...`: it is shown only once.
5. Put it into `.env`:
   ```env
   OPENROUTER_API_KEY=sk-or-v1-...
   OPENROUTER_MODEL=google/gemini-2.5-flash-lite
   ```
   `OPENROUTER_MODEL` can be any model from https://openrouter.ai/models; restart the service after changing it.

JEV is called only through OpenRouter with the same key; its model is set in `config.ini` `[jev] model` (default `~typesafe/jev-latest`).

### 6. Verification

1. All required variables are filled in:
   ```bash
   sudo docker compose run --rm search-vac-tg python -c "from src.config import load_settings; m=load_settings().missing_for_run(); print('OK' if not m else 'Не заполнено: '+', '.join(m))"
   ```
   Expected: `OK` (otherwise the command prints the missing variables after `Не заполнено:`, i.e. "Not filled in:").
2. Dry run on the samples:
   ```bash
   sudo docker compose run --rm search-vac-tg python scripts/dry_run.py
   ```
   Expected: a table of 8 vacancies and `JEV errors: 0`.
3. Start the service:
   ```bash
   sudo docker compose up -d && sudo docker compose logs -f
   ```
   The log must contain `monitoring @FreelanceBay`, and `/stats` in the bot replies.

### All environment variables

| Variable | Required | Meaning |
|---|---|---|
| `TELEGRAM_API_ID` | yes | Telegram app id from my.telegram.org |
| `TELEGRAM_API_HASH` | yes | Telegram app hash |
| `NOTIFY_BOT_TOKEN` | yes | token of the notification bot (BotFather) |
| `OWNER_TELEGRAM_ID` | yes | numeric id of the only user the bot talks to |
| `OPENROUTER_API_KEY` | yes (always) | OpenRouter key: REVIEW decisions, JEV fallback and reply drafts need it; also used for JEV |
| `OPENROUTER_MODEL` | yes (always) | OpenRouter model id for review / reply drafts |
| `JEV_FALLBACK_TO_OPENROUTER` | no | `true` (default): on a JEV error use OpenRouter; `false`: park the job as `jev_unavailable` |
| `NOTIFY_SCORE` | no | minimal score to notify, default 65 |
| `HIGH_FIT_SCORE` | no | score for the "🔥" header, default 80 |
| `SHOW_PAID_CONTACT` | no | `false` (default) skips jobs with a paid contact |
| `DATABASE_URL` | no | default `sqlite:///data/app.db` |

`python -m src.main` refuses to start and lists the missing required variables.

## Configuration

### config.ini (non-secret constants)

| Section / key | Default | Meaning |
|---|---|---|
| `[paths]` channels_file, filter_file, profile_file, profile_md_file, materials_dir, session_file | `config/channels.yaml`, `config/filter.yaml`, `data/profile.json`, `data/profile.md`, `materials`, `data/telegram` | file locations (relative to the project root) |
| `[jev]` timeout_sec | 20 | JEV request timeout |
| `[jev]` min_confidence | 0.70 | below it ACCEPT/REJECT becomes REVIEW |
| `[jev]` max_text_chars | 3000 | job text limit sent to JEV |
| `[openrouter]` base_url | `https://openrouter.ai/api/v1` | OpenRouter API base |
| `[openrouter]` timeout_sec | 60 | request timeout |
| `[openrouter]` max_retries | 2 | retries on timeout / 429 / 5xx |
| `[dedup]` fuzzy_threshold | 90 | similarity (0-100) to call a post a duplicate |
| `[dedup]` window_days | 14 | how far back to compare |
| `[dedup]` max_candidates | 1000 | max earlier jobs compared |
| `[telegram]` catchup_limit | 50 | posts taken on the first run of a channel |
| `[telegram]` poll_interval_sec | 120 | how often channels are polled |
| `[telegram]` contact_click_delay_sec | 2 | pause before a callback click |
| `[telegram]` flood_sleep_threshold | 60 | Telethon sleeps automatically on FloodWait up to this many seconds |
| `[pipeline]` min_text_length | 40 | shorter posts are rejected as `too_short` |
| `[pipeline]` retry_limit | 5 | max attempts for a failed job |
| `[pipeline]` retry_interval_sec | 600 | how often failed jobs are retried |
| `[paths]` heartbeat_file | `data/heartbeat.json` | heartbeat file for the Docker healthcheck |
| `[logging]` level, file | `INFO`, `data/app.log` | log level and log file |
| `[logging]` max_bytes, backup_count | 5242880, 3 | log rotation: size of one file and number of archived copies |
| `[alerts]` enabled | true | initial state of owner alerts (changed in the bot) |
| `[backup]` dir | `data/backups` | where backups are stored |
| `[backup]` keep | 7 | how many latest backups to keep, 1..60 (changed in the bot) |
| `[backup]` hour | 4 | hour of the daily backup in the local timezone `[ui] timezone` |

### Adding a channel

The easy way is the bot (`/menu` → Channels → Add channel, see "Managing from the bot"). `config/channels.yaml` only seeds the initial list: a channel is taken from it the first time it appears; later changes and deleted channels are managed in the bot. To seed from the file, edit `config/channels.yaml`; only public channels are supported (the service never joins anything):

```yaml
channels:
  - username: FreelanceBay
    enabled: true
  - username: another_public_channel
    enabled: true
    click_callbacks: false
```

`@name` and `https://t.me/name` forms are also accepted. Restart the service: `sudo docker compose restart`.

`click_callbacks` (default `true`) decides whether the contact resolver may press the channel's "get contact" callback button. The handler of that button is the channel bot's server code; set `false` for a channel whose bot charges credits/limits per press. With `false` the contact is taken only from the text and URL buttons.

### config/filter.yaml

Lowercase keyword lists of the rule pre-filter:
- `strong_positive` (+25 to the rules score per hit), `positive` (+10): technical signals;
- `negative` (-5), `strong_negative` (-15): non-technical work;
- `job_markers`: words that show "this is a job post" ("нужен", "бюджет", "hiring"...);
- `ignore_contacts`: usernames (channel admins, ad signatures) that must not be taken for the customer contact.

Semantics: a post is rejected if it is shorter than `min_text_length`; if it has only negative words and no positive ones; or if it has no positive words and no job markers. Everything else goes to JEV; a negative word next to a positive one does not reject. Two or more `strong_positive` hits without negatives are logged as `RULE_ACCEPT` (they still go through JEV). Matching is by word: Cyrillic keywords accept endings, Latin keywords accept a plural `s`, `*` at the end means a prefix.

### Changing thresholds and the OpenRouter model

- Preferred: in the bot, `/menu` → Thresholds / Model. The value is stored in the database and overrides `.env`.
- `.env` values (`NOTIFY_SCORE`, `HIGH_FIT_SCORE`, `SHOW_PAID_CONTACT`, `OPENROUTER_MODEL`) are only initial defaults, used until a setting is first changed in the bot; after that editing `.env` has no effect on it.

## Managing from the bot

Everything below is done from your notification bot (owner only); no restart is needed. Send `/menu`.

- **Main menu** (`/menu`, also shown after `/start`) is a status dashboard: notifications on/paused, active channels, check interval and the time of the last check, poll failures, last backup, alerts, thresholds, paid contacts, model, timezone. Buttons: 📜 Journal, 📊 Statistics, 🔄 Check now, ⏸ Pause / ▶️ Resume, 📡 Channels, 👤 Profile, ⚙️ Settings. Commands: `/menu`, `/channels`, `/profile`, `/stats`, `/journal`, `/help`, `/cancel`. `/start` and `/help` show a short welcome text: what the bot does, what is on a card, the commands.
- **⚙️ Settings** (submenu with a short description of each item): 🎯 Thresholds, 🧠 Model, ⏱ Check interval, 💰 Paid contacts on/off, 🗑 Journal retention, 🕒 Timezone, 💾 Backups, 🔔 Alerts on/off. "⬅️ Back" returns one level up. 👍/👎 on a card are stored (with a snapshot of the decision) for later analysis; they do not change the selection yet.
- **Job card**: header "🔥 Great match" (score at or above the high threshold) or "✅ Might fit" with the score and a 10-segment bar, the category, the title, the text, "✨ Why it fits", and the labelled block 💰 Budget, 📡 Source, 👤 Contact, 🧭 Selection (rules / JEV / JEV + OpenRouter review / OpenRouter when JEV failed).
- **Channels**: one row per channel. The first button toggles monitoring (✅ on / ⏸ off), "👆 click: yes/no" toggles `click_callbacks`, 🗑 deletes after a confirmation ("Delete @x? Yes / No"). "➕ Add channel" asks for `@username` or a `t.me/...` link (a forwarded post from the channel also works). Only public channels are supported; the account never joins anything. On an error you can retry or send `/cancel`.
- **Thresholds**: −5 / −1 / +1 / +5 for the notification threshold and the high-fit threshold (0-100, the notification threshold cannot be above the high-fit one).
- **Paid contacts**: toggle whether jobs with a paid contact are shown.
- **Pause**: while paused, accepted jobs are held; after "Resume" the backlog is sent immediately.
- **Check interval** (⏱): how often channels are polled: presets (1, 2, 5, 10, 15, 30 min, 1 h) or "✏️ Custom value" (any interval from 1 min to 24 h: `500` = minutes, or `90s`, `30m`, `2h`, `1.5h`); applies immediately (the current wait is cut short). "🔄 Check now" polls the channels right away without changing the interval and reports how many new posts were found; the screen also shows the time of the last check (in your timezone). `poll_interval_sec` in `config.ini` is only the default.
- **Model**: send a new OpenRouter model id, e.g. `google/gemini-2.5-flash-lite` (list: https://openrouter.ai/models); an invalid value is rejected with a message.

Where settings live: after the first change from the bot the value is stored in the database and **overrides** `.env` (`NOTIFY_SCORE`, `HIGH_FIT_SCORE`, `SHOW_PAID_CONTACT`, `OPENROUTER_MODEL`). These `.env` values and `config/channels.yaml` are only initial defaults. `channels.yaml` seeds a channel only the first time it appears; a channel deleted from the bot is not re-added from the file.

### Profile (from the bot)

The profile is the short description of the executor (skills, technologies, services, best-fit and "not interested" categories). JEV and OpenRouter never see the resume itself: they receive the **compact profile** (`compact_profile`, at most ~900 characters), which is also shown in the bot inside the profile screen. `/profile` (or "👤 Profile" in `/menu`) shows the status line (base projects, uploaded files, manual +added / -removed skills) and the compact text. Buttons:

- **📎 Upload resume/portfolio**: send a PDF (with a text layer), DOCX, MD or TXT file up to 10 MB as a *document* (not a photo). The bot extracts technologies and projects and replies with what was added to the profile. A scanned PDF without text is rejected.
- **➕ Add skills / ➖ Remove skills**: send names separated by commas or new lines (1-40 characters each). Matching is case-insensitive. Adding a skill that was removed (and vice versa) cancels the earlier action.
- **📂 Files**: the list of uploaded files; 🗑 deletes a file after a confirmation, its skills disappear from the profile.

Changes apply immediately (no restart): JEV, OpenRouter and reply drafts use the new profile right away.

How the layers work: the **base profile** `data/profile.json` comes from the repo (built locally from your portfolio) and is never changed by the bot. Uploaded files are stored in `data/materials_uploads/`, the profile derived only from them is cached in `data/profile_uploads.json`, manual changes are in `data/profile_overrides.json`. The effective profile = base + uploads, then manual add/remove. All these files live in `data/` on the server, so **back up `data/`** together with the database. They are git-ignored, so `git pull` does not touch them.

### Journal (from the bot)

`/journal` (or "📜 Journal" in `/menu`) is one message edited in place: a header with 24-hour counters (total, sent, rejected by rules, by JEV, by fit score, paid contact, duplicates, in progress), then the current filter and period, then 10 entries per page, newest first. Each entry is two lines: emoji, local time, `@channel`, title (a link to the post) and, below, the reason (for example "score 41 < 60", "JEV: not a fit (confidence 0.92)", "duplicate of job #12").

- **Filters**: All, Sent, Rejected by rules, Rejected by JEV, Fit score too low, Paid contact, Duplicates, In progress / errors (the current one is marked with •).
- **Period**: 24 h / 7 days / all time; pagination with ◀️ ▶️; "🔄 Refresh".
- **Retention** ("🗑 Journal retention: N d" in "⚙️ Settings"): presets 14/30/60/90/180/365 days or a custom number of days. The minimum is the duplicate-detection window (presets below it are hidden). Applies live; old records are cleaned up every 6 hours.
- **Never deleted**: jobs that were sent to you and jobs with 👍/👎 feedback (with their source messages, contacts, notifications and feedback), jobs still being retried, channels and settings.
- **Retention bounds**: minimum `max(7, dedup window_days)`, maximum `max(365, dedup window_days)`; a value from `config.ini [journal]` outside the range is logged as a warning and replaced by the nearest bound. Jobs in `notified`, `notify_uncertain`, `notifying` and every retryable status are never deleted, and a kept job keeps ALL its source messages (reposts too).
- **Timezone** ("🕒 Timezone: Europe/Sofia" in "⚙️ Settings"): presets Europe/Sofia, Europe/Moscow, Europe/Kyiv, Europe/Berlin, UTC, Asia/Almaty or your own IANA name (e.g. Europe/Warsaw). Stored in the DB, applies live to the journal, the check-interval screen and `/stats`; `config.ini [ui] timezone` is only the initial default.
- **Performance**: an index on `messages(received_at, id)` is created automatically on start; journal counters are cached for 60 s (reset on cleanup). Pages use OFFSET, which is fine for a single-owner journal of this size (documented trade-off).

## Profile

The profile is built from your materials and used for JEV/OpenRouter decisions and reply drafts.

1. Put resume, portfolio, project READMEs, `links.txt` into `materials/` on the server host (formats: md, txt, pdf, docx; no OCR). Files in `materials/private/` are git-ignored.
2. Extra files or folders outside the project: absolute paths, one per line, in `materials/extra_paths.txt` (`#` = comment; missing paths are skipped).
3. Rebuild on the server: `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py` (writes `data/profile.json` and `data/profile.md`).
   - `--dry-run` prints the summary and writes nothing;
   - `--llm` additionally polishes summary/services through OpenRouter (compact data only).
4. If there are no materials, a provisional profile (`provisional: true`) is used and a warning is logged at start.
5. Restart the service after a rebuild (`sudo docker compose restart`): the profile is loaded at start.

## Stats and savings

`/stats` in the bot or `sudo docker compose exec search-vac-tg python scripts/stats.py` shows: messages received, duplicates, rule rejects, JEV processed / accepts / rejects / reviews / errors (and fallbacks), OpenRouter calls (review / other), notifications, skipped paid contacts, feedback, estimated JEV and OpenRouter cost, jobs by status. Counters are all-time: when old journal rows are cleaned up, their contribution is moved into an archive, so the totals do not drop; the last line shows the date since which the detailed journal is kept (in your timezone). The report is in Russian and split into sections (flow, JEV selection, notifications, estimated costs, jobs by status with Russian labels); the OpenRouter line ends with "сэкономлено X%": the share of jobs JEV settled without the expensive model.

## Safety

- No payments: the service never calls payment methods; any sign of payment stops the contact resolver.
- No auto-replies: the reply draft is only a text for you; nothing is sent to the customer.
- No joining channels or chats; only public channels are read.
- The 2FA password is never sent to bots.
- Secrets only in `.env`; `.env` and `data/*.session` are git-ignored and never committed.
- The bot ignores everyone except `OWNER_TELEGRAM_ID`.
- Untrusted text (job text, contacts, LLM output) is HTML-escaped in cards.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `Telegram session is not authorized` | run `sudo docker compose run --rm search-vac-tg python scripts/telegram_login.py` |
| `FloodWait Ns` in the log | Telegram rate limit; the service waits and retries the same channel (up to 3 attempts; unresolved channels are retried every retry interval). Short waits (up to `flood_sleep_threshold`) are handled by Telethon; if frequent, raise `poll_interval_sec` |
| The bot sends nothing | press `/start` in your bot once; check `NOTIFY_BOT_TOKEN`, `OWNER_TELEGRAM_ID` and `sudo docker compose logs` for `ERROR notification` (status `notify_error`, retried automatically) |
| `fill these variables in .env: ...` | fill the listed variables |
| `JEV_ERROR` in the log | the job is either sent to OpenRouter (fallback on) or parked as `jev_unavailable` and retried every `retry_interval_sec` up to `retry_limit` attempts; check the key, balance and outbound HTTPS to openrouter.ai |
| `ERROR OpenRouter review` | the job gets `llm_error` and is retried automatically; check `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, balance |
| `no enabled channels` / `channel @x not found` | check `config/channels.yaml`; the channel must be public |
| `using PROVISIONAL profile` | add materials and run `sudo docker compose exec search-vac-tg python scripts/rebuild_profile.py`, then `sudo docker compose restart` |

## Local development (optional)

Needed only for editing code and running tests; production runs on the VPS in Docker (see above). Virtual environment (Windows):

```bash
python -m venv .venv
# Git Bash
source .venv/Scripts/activate
# PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
# for tests
pip install -r requirements-dev.txt
```

Running locally (after filling in `.env`): `python scripts/telegram_login.py`, `python scripts/dry_run.py`, `python -m src.main`.

Tests:

```bash
pytest -q
# live JEV test (real network, tiny cost, needs OPENROUTER_API_KEY):
RUN_LIVE_JEV=1 pytest -q -m live
```

Lint (ruff, settings in `ruff.toml`; `ruff` is in `requirements-dev.txt`):

```bash
ruff check src tests scripts
```
