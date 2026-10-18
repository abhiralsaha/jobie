# AI pipeline integration status

The addon has been merged into `app/ai_pipeline/` and is wired into the
existing watcher and authorized import API. The original source bundle remains
under `jobie_ai_addon/` as the unmodified input copy.

## Integrated behavior

- `app/fresh_watcher.py` passes each successfully deduplicated job to the AI
  pipeline when `AI_PIPELINE_ENABLED=true`; otherwise it sends the original
  immediate Telegram alert.
- The watcher starts the retry scheduler and approval poller once, when enabled.
  Keep `AI_RUN_APPROVAL_POLLER=true` only on the watcher service.
- Description fetches use `app.http_client.fetch`, so they share Jobie's
  retries and circuit breaker.
- `POST /jobs/import` accepts an optional description and submits a newly
  imported job to the pipeline when enabled. With the pipeline disabled, the
  existing recent-job Telegram alert behavior is preserved.
- The watcher widens its LinkedIn `f_TPR` look-back to cover the time since its
  last successful poll (up to 24 hours), including after restarts. The existing
  `seen_jobs` table continues to deduplicate.
- The watcher runs an owner-only Telegram agent in the same `getUpdates`
  long-poll loop as referral approvals. It supports `/start`, `/help`, `/status`,
  `/jobs`, `/ai`, `/digest`, `/search`, `/retry`, inline menu buttons, and
  allow-listed GPT-backed natural-language requests. Search and retry actions
  require a separate confirmation; email still requires the existing explicit
  approval button. In dry-run mode the agent can answer commands, but job
  notifications are recorded and approval callbacks cannot send email.
- The main LinkedIn search worker also routes newly found jobs through the AI
  pipeline when enabled, rather than sending its raw alert. Its new-job records
  are processed by the existing watcher-side retry scheduler.
- Set `FRESH_WATCHER_IN_MAIN=true` to run the main search/API, fresh watcher,
  Telegram agent, and approval poller in the single
  `app.linkedin_fresh_jobs:app` server process. That app mounts the existing
  authorized import route at `/jobs/import`. In this mode do not run the
  standalone watcher, optional separate import API, multiple Uvicorn workers,
  or multiple Render replicas.

## Required operator setup

1. Install dependencies with `python -m pip install -r requirements.txt`.
2. Copy the example files in `config/` to their runtime names and replace all
   placeholder resume/profile/email content with your own.
3. Create `config/telegram_destinations.json` using the real environment
   variable names for each Telegram bot/chat. Resume PDFs are fail-closed and
   may go only to the destinations named exactly `1 min`, `Fresher bot`,
   `5 min`, and `30 min`.
4. Set `AI_DRY_RUN=true`, the AI provider/key/model (or reuse a supported
   existing key), your numeric `AI_TELEGRAM_AUTHORIZED_USER_ID`, durable
   `AI_DATABASE_URL`/`SUPABASE_DB_URL`, and any optional SMTP/contact keys.
5. For a single Render server set `FRESH_WATCHER_IN_MAIN=true`, use the
   `app.linkedin_fresh_jobs:app` command with one Uvicorn worker and one
   instance, and do not deploy a separate watcher/import API. Use
   `deploy/Dockerfile.addon` if resumes must compile. Never configure a second
   poller or webhook for the control bot.
6. For GPT Luna, set `AI_PROVIDER=openai_compat`, `AI_MODEL=gpt-luna`,
   `AI_API_KEY`, and the provider-supplied `AI_BASE_URL`. Until credentials are
   present, slash-command agent functions work, but free-text answers and
   description classification cannot call the model.
7. Run `python -m app.ai_pipeline.cli check-config`,
   `python -m app.ai_pipeline.cli check-destinations`, and the unit tests before
   enabling the live pipeline.

The operator assigned `FRESH_WATCHER_BOT_TOKEN` and `FRESH_WATCHER_CHAT_ID`
to both `1 min` and `Fresher bot`, and identified existing
`TELEGRAM_DIGEST_5M_*` and `TELEGRAM_DIGEST_30M_*` pairs for the separate `5
min` and `30 min` destinations. The private control bot uses
`AI_TELEGRAM_BOT_TOKEN` and `AI_TELEGRAM_CONTROL_CHAT_ID`. Telegram access can
be checked without sending a message. Resume-PDF delivery remains blocked
until all four resume destinations resolve to unique bot/chat pairs.

See the AI pipeline section in `README.md` for local and Render start commands.
