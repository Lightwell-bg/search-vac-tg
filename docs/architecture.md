# Architecture

## Components

One process (`python -m src.main`), one asyncio event loop:

| Component | File | Role |
|---|---|---|
| Entry point | `src/main.py` | loads settings, checks required variables, wires everything (`build_pipeline`), runs the listener and the bot with `asyncio.gather` |
| Settings | `src/config.py` | secrets from `.env`, constants from `config.ini`, channels from `config/channels.yaml`, rotating log |
| TelegramService | `src/telegram/client.py` | Telethon user session; reads public channels; the only two Telegram actions the resolver may use: callback click and refetch |
| ChannelListener | `src/telegram/listener.py` | NewMessage handler + poll loop + retry loop |
| Parser | `src/telegram/parser.py` | normalization, contacts, budget, title, buttons |
| Pipeline | `src/filtering/pipeline.py` | the whole flow and job statuses |
| RuleFilter | `src/filtering/rules.py` | keyword pre-filter (`config/filter.yaml`) |
| Deduplicator | `src/filtering/deduplicator.py` | hash + RapidFuzz similarity |
| JevClassifier / JevClient | `src/jev/` | three typed questions to the remote JEV API, guard rules |
| OpenRouterClient | `src/llm/openrouter.py` | review of ambiguous jobs, reply drafts, profile polish |
| scorer | `src/filtering/scorer.py` | final fit score and decision |
| ContactResolver | `src/telegram/contact_resolver.py` | contact for accepted jobs, paid-contact detection |
| Repository / Database | `src/db/` | SQLite (WAL) through SQLAlchemy async |
| NotifyBot | `src/bot/` | aiogram: cards, feedback, reply drafts; owner only. `menu.py`: `/start` and `/help` (welcome text), `/menu` (status dashboard), settings submenu (`m:set`), backups screen, `/channels`, `/profile`, `/stats`, `/journal`, `/cancel`, FSM (MemoryStorage) for adding a channel and changing the model |
| Health | `src/health.py` | writes `data/heartbeat.json` every minute (`heartbeat_loop`); `python -m src.health check` is the Docker HEALTHCHECK (stale file or no finished poll = unhealthy); the same loop raises the `poll_failing` alert after 3 failed poll cycles |
| Alerter | `src/alerts.py` | short messages to the owner (start/stop, poll failing, JEV/OpenRouter error bursts, backup failed, DB integrity); per-kind cooldown, never raises, on/off via the runtime setting `alerts_enabled` |
| BackupService | `src/backup.py` | daily (`[backup] hour`, local time) and manual backups of the SQLite DB (online copy) and the Telegram session into `data/backups`, rotation by `backup_keep`; restore with `scripts/restore_backup.py`; the bot menu (`m:bk`, `bk:*`) uses `backup_now()` / `list()` |
| RuntimeSettings | `src/settings_store.py` | settings editable from the bot, stored in the `settings` table, validated and pushed to live objects |

## Data flow

```
Telegram channel post
  -> ChannelListener (event handler task, or poll_once)
  -> Pipeline.process_post
       message_exists? -> "seen"
       normalize_post (text, contacts, budget, title, hash, dedup key)
       rules.evaluate
       [dedup lock] save_message -> dedup.find -> link_duplicate | create_job [/dedup lock]
       _evaluate:
         1. rules reject           -> rule_rejected
         2. JEV classify           -> jev_rejected | (ACCEPT) | (REVIEW) | JEV error
         3. OpenRouter review      only for REVIEW, or JEV error with fallback
         4. decide() (scorer)      -> fit_rejected | accepted
         5. _deliver               contact resolver -> paid_skipped | notify -> notified
```

Empty text is stored and logged as `RULE_REJECT ... empty text`; no job is created.

The JEV call is one HTTPS request (`POST .../systemone`) with `state` (compact profile, job text, keywords found by rules) and three typed questions. The parsed answer becomes a `JevDecision` (decision, fit 0-100, category, confidence).

## Database tables (`src/db/models.py`)

| Table | Purpose |
|---|---|
| `channels` | monitored channels; `last_message_id` is the poll cursor; flags `enabled` (monitoring on/off) and `click_callbacks` (may the resolver press the contact button) are edited from the bot |
| `settings` | key/value runtime settings: `notify_score`, `high_fit_score`, `show_paid_contact`, `notifications_paused`, `openrouter_model`, and the list of channels deleted from the bot |
| `messages` | every stored post (original and normalized text, buttons, extracted data); unique `(channel_tg_id, message_id)`; `is_duplicate`, `job_id` |
| `jobs` | one row per unique vacancy: status, attempts, `rules_result` / `jev_result` / `llm_result`, route, category, fit score, decision reason, contact status/value, last error |
| `job_sources` | links a job to every message with the same vacancy (original, hash duplicate, fuzzy duplicate with similarity) |
| `contacts` | contacts found for a job (kind, value, source: text / url_button / callback / edited_message) |
| `notifications` | messages sent to the owner (kind `job` or `application`) |
| `feedback` | 👍/👎 clicks with a snapshot of rules/JEV/LLM/final decision |
| `jev_usage` | every JEV call: decision, confidence, duration, tokens, cost, error, `fallback_used` |
| `llm_usage` | every OpenRouter call: purpose (`job_review`, `application_generation`, `profile_building`), tokens, cost, error |

Tables are created on start (`create_all`); there are no migrations.

## Runtime settings flow

1. On start `RuntimeSettings.load` takes defaults from `.env` (`Settings`) and overrides each key that has a row in `settings`; `apply_to` copies the values to `PipelineOptions` and the OpenRouter client.
2. The bot (`/menu`) calls `RuntimeSettings.set(key, value)`: validation (`ValueError` with a Russian message is shown to the owner), write to the DB, in-memory update, then subscribers (`main.on_settings_change`) re-apply the values to live objects. No restart.
3. Unpausing (`notifications_paused` True -> False) starts `Pipeline.flush_backlog` in the background: held `ACCEPTED` jobs are sent. While paused the pipeline keeps jobs `ACCEPTED` without spending attempts.
4. Channels: the bot calls `ChannelListener.add_channel / set_enabled / set_click / remove_channel`. `channels.yaml` only seeds rows the first time a channel appears; removed channels are remembered in `settings` and never re-seeded. Only public channels are added; the account never joins.
5. After the first change from the bot the DB value overrides `.env`; `.env` and `channels.yaml` are initial defaults only.

## Journal and cleanup (`src/journal.py`, `src/bot/journal_view.py`)

- The journal has no table of its own: one entry per stored `messages` row, LEFT JOIN `jobs`. `Repository.journal` / `journal_counts` classify each row into a kind (sent, rules, jev, fit, paid, dup, pending) with one SQL `CASE`; `journal.describe` builds the Russian reason from `decision_reason`, `jev_result`, `llm_result`.
- Bot UI: `/journal` and `j:<kind>:<period>:<page>` callbacks edit one message in place (10 entries per page, text trimmed to 4096, all untrusted strings HTML-escaped, links only for http(s)/t.me). Retention screen: `m:jr`, `jr:<days>`, `jr:custom` (FSM `waiting_retention`).
- Cleanup: the listener runs `journal.cleanup_old(repo, log_retention_days)` every 6 hours (`CLEANUP_INTERVAL_SEC`) in one transaction. Kept: notified jobs, jobs with feedback, jobs in a retryable status or in progress (with messages, sources, contacts, notifications, feedback); channels and settings are never touched. `log_retention_days` is a runtime setting with a minimum of the dedup window (`retention_min`).

## Profile layers (`src/profile/service.py`)

- BASE: `data/profile.json` (committed, built locally by `scripts/rebuild_profile.py`; the bot never writes it).
- UPLOADS: files sent to the bot are saved in `data/materials_uploads/`; the builder runs only over them (in `asyncio.to_thread`) and the result is cached in `data/profile_uploads.json`.
- OVERRIDES: `data/profile_overrides.json` `{"add": [], "remove": []}` (manual skills, case-insensitive).
- EFFECTIVE = BASE + UPLOADS, then OVERRIDES (`ProfileService.load()`); `compact_profile(effective)` is what JEV/OpenRouter get. After every change `ProfileService` notifies subscribers (`pipeline.set_profile`, `bot.set_profile`), so no restart is needed. Mutations are serialized with an `asyncio.Lock`; files are written atomically.

## Job statuses (`JobStatus`)

| Status | Meaning |
|---|---|
| `new` | created; **retryable**: a job interrupted by a stage exception or a crash is resumed by `retry_pending` (after a 2 minute grace so a task still running is not touched) |
| `rule_rejected` | rule filter rejected |
| `jev_rejected` | JEV REJECT (with enough confidence and consistent fit) |
| `jev_unavailable` | JEV failed and fallback is off; retried |
| `llm_error` | OpenRouter review failed (or is not configured); retried |
| `fit_rejected` | final decision: not a fit |
| `accepted` | fit; contact/notification not finished yet; retried if stuck |
| `paid_skipped` | fit, but the contact is paid and `SHOW_PAID_CONTACT=false` |
| `notifying` | send claim written before `notify_job`; if a retry finds it, the send outcome is unknown, so the job becomes `notify_uncertain` |
| `notified` | card sent (a failure to record it afterwards is only logged) |
| `notify_error` | sending the card raised before Telegram accepted it; retried |
| `notify_uncertain` | crashed mid-send: the card may have been delivered; never resent automatically (no duplicates); not retried |
| `error` | unrecoverable (for example the primary message is missing) |

Transitions:

```
new -> rule_rejected
new -> jev_rejected
new -> jev_unavailable -> (retry) -> ...
new -> llm_error       -> (retry) -> ...
new -> fit_rejected
new -> (stage exception, attempts+1) -> new -> (retry) -> ...
new -> accepted -> paid_skipped
                -> notifying -> notified
                             -> notify_error -> (retry) -> notifying -> notified
                             -> (crash) -> notifying -> (retry) -> notify_uncertain
```

## Durable claims (no repeated side effects)

External actions are claimed in the DB before they happen, so a crash never repeats them:

- **Contact click.** `jobs.contact_status='resolving'` is written before the resolver runs. A retry that finds `resolving` calls `resolve(..., allow_click=False)`: the button is never pressed twice (the contact may then end as `contact_unknown`).
- **Card send.** `status='notifying'` is written before `notify_job`. A retry that finds it marks the job `notify_uncertain` and does not send again. After a successful send a DB failure while recording it is only logged, the outcome stays `notified`.
- **Orphan messages.** `save_message` and `create_job` are separate transactions. A message with `job_id IS NULL`, `is_duplicate=false`, non-empty text and older than the grace period (2 minutes) is re-ingested by `retry_pending` (`Repository.orphan_messages`).
- **Poll cursor.** `process_post` returns `"error"` only when the post was not stored; the listener then keeps `last_message_id`, stops the channel's batch and retries on the next poll; after 3 consecutive errors for the same message it logs `ERROR skipping poison message` and moves on. Any other outcome (including `"failed"`, which is stored and recovered by `retry_pending`) advances the cursor.

Duplicates do not create jobs: the message is marked `is_duplicate` and linked through `job_sources`.

## Retry mechanism

- `JobStatus.RETRYABLE = (new, jev_unavailable, llm_error, notify_error, accepted, notifying)`.
- The listener calls `Pipeline.retry_pending` every `[pipeline] retry_interval_sec` (600 s): first orphan messages (up to 50), then up to 50 jobs with `attempts < retry_limit` (5), oldest first. The same loop re-resolves configured channels that failed to resolve earlier.
- `attempts` grows on a stage exception, a JEV failure without fallback, an OpenRouter failure and a notification failure; it is reset to 0 after a successful JEV answer, a successful OpenRouter review and on `accepted`.
- Resuming: `accepted` / `notify_error` go straight to delivery (the contact is not resolved again if it is already stored; `resolving` is resolved without clicking); `notifying` becomes `notify_uncertain`; `llm_error` reuses the stored JEV result; `jev_unavailable` and `new` run the whole evaluation again.
- Jobs currently in progress (`_in_progress`) are skipped.

## Concurrency

- Two ingestion paths: the Telethon `NewMessage` handler (each event runs as a separate `asyncio` task so JEV/LLM/clicks never block Telethon's update loop) and `poll_once` every `[telegram] poll_interval_sec` (по умолчанию 120 с, меняется в боте).
- The unique key `(channel_tg_id, message_id)` drops the copy that arrives by both paths; `message_exists` is a fast pre-check.
- `_dedup_lock` makes save message + dedup search + create job atomic, so two identical posts processed at once cannot create two jobs.
- `_in_progress` (set of job ids) prevents the retry loop from touching a job that is being processed.
- The poll is the source of truth for `channels.last_message_id`; the event handler never moves the cursor.
- Callback clicks are serialized by `_click_lock` with `contact_click_delay_sec` between them.
- Reply drafts run as background tasks; `_generating` prevents two drafts for one job at once.

## Error handling

- `process_post` and `retry_pending` never raise: exceptions are logged as `ERROR ...`, one bad post does not stop the monitor.
- Poll errors per channel are logged (`ERROR polling @channel`) and the loop continues; retry errors likewise.
- JEV: one retry on 429/5xx (numeric `Retry-After` only, anything else waits 1 s), then `JevError` (`config`, `timeout`, `http`, `invalid`, `unexpected`); every adapter failure is a `JevError`. Non-object JSON, wrong-typed fields, NaN, confidence outside [0,1] (probabilities are used as fallback), fit outside [0,3] are `invalid` (never clamped). The call is recorded in `jev_usage` with the error; fallback to OpenRouter or `jev_unavailable`.
- OpenRouter: retries on timeout / 429 / 5xx (`max_retries`), no retry on other 4xx; a failure gives `llm_error`. A non-object body, non-object `usage` or non-numeric usage fields are handled (0), no `ValueError` escapes.
- Contact resolver failure gives `contact_unknown` (the job is still delivered).
- Notification failure before Telegram accepted the card gives `notify_error`.
- FloodWait on resolving a channel: sleep and retry the same channel (up to 3 attempts); on reading a channel: sleep and continue; on a callback click: the click fails and the contact becomes `contact_unknown`.
- Session file gets mode 0600 and its directory 0700 (best effort, ignored on Windows).

## Contact rules (`ContactResolver`)

1. **Payment scan first** over the whole keyboard: a `buy` button, a contact-looking button with a payment marker (Stars, "оплат", price...), or a payment/checkout link (Telegram invoice, `stripe.com`, `boosty.to`, `/checkout`, `/pay` ...) -> `paid_contact`, before any text contact is considered. Negations ("бесплатно", "оплата не требуется", "free") are removed before the scan.
2. Human contact in the text (username, t.me user, email, phone) -> `direct`.
3. URL buttons: Telegram user -> `direct`; bot, website, response form -> `external_contact_flow`, never `direct`. Text-only web links are `external_contact_flow` too.
4. A callback button "get contact": pressed at most once, never when it requires a password (-> `contact_unknown`), never when `allow_click=False` (durable claim found) or when the channel has `click_callbacks: false` (`config/channels.yaml`, passed to `ContactResolver` as `click_policy` by `build_pipeline`). The answer (or the edited post) is scanned for payment again (payment/checkout domains -> `paid_contact`); a human contact -> `free`; only a web URL or bot -> `external_contact_flow`.

## Required configuration

`OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are always required (REVIEW decisions, fallback and reply drafts need them). JEV uses the same `OPENROUTER_API_KEY` (`POST {base_url}/systemone`); its model is set in `config.ini` `[jev] model`. Docker: `./config` and `./materials` are mounted read-only, `data/` must be owned by uid 1000 with mode 700 (`mkdir -p data && sudo chown -R 1000:1000 data && chmod 700 data`); `.dockerignore` excludes `data/`, `*.session*`, `.claude/`, `materials/private/`. `data/profile.json` and `data/profile.md` are tracked in git so the profile reaches the server.
- Telethon reconnects automatically (`auto_reconnect`, infinite connection retries).
- The bot ignores every user except the owner; the bot handlers catch errors of background tasks.
- Startup: missing required variables or no enabled channels stop the service with exit code 2; an unauthorized Telegram session stops it with a clear message.

## Logging events

Written to stderr and `data/app.log` (rotating, 5 MB x 3). Event names (prefix of the message):

| Event | When |
|---|---|
| `NEW_MESSAGE` | a new post was stored |
| `DUPLICATE` | the post is a duplicate of an existing job (hash or fuzzy) |
| `RULE_REJECT` | rule filter rejected the post (or the text is empty) |
| `RULE_ACCEPT` | two or more strong positive hits without negatives (still goes to JEV) |
| `JEV_ACCEPT` / `JEV_REJECT` / `JEV_REVIEW` | JEV decision after the guard rules |
| `JEV_ERROR` | JEV call failed |
| `OPENROUTER_REVIEW` | OpenRouter review finished |
| `FIT_ACCEPTED` / `FIT_REJECTED` | final decision with score and route |
| `CONTACT_DIRECT` / `CONTACT_FREE` / `CONTACT_EXTERNAL` / `CONTACT_PAID` / `CONTACT_UNKNOWN` | contact resolution result |
| `NOTIFICATION_SENT` | card sent to the owner |
| `ERROR ...` | processing, retry, poll, OpenRouter review, notification or resolver error |

Other useful lines: `monitoring @channel (title)`, `poll: N new posts`, `JEV: url model=...; OpenRouter model=...; notify>=...` (at start), `using PROVISIONAL profile`.
