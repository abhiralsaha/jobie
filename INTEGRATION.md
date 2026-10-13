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

## Required operator setup

1. Install dependencies with `python -m pip install -r requirements.txt`.
2. Copy the example files in `config/` to their runtime names and replace all
   placeholder resume/profile/email content with your own.
3. Create `config/telegram_destinations.json` using the real environment
   variable names for each Telegram bot/chat. Resume PDFs are fail-closed and
   may go only to the three destinations named exactly `1 min`, `Fresher bot`,
   and `5 min and 30 min ago`.
4. Set `AI_DRY_RUN=true`, the AI provider/key/model (or reuse a supported
   existing key), your numeric `AI_TELEGRAM_AUTHORIZED_USER_ID`, durable
   `AI_DATABASE_URL`/`SUPABASE_DB_URL`, and any optional SMTP/contact keys.
5. On Render, use `deploy/Dockerfile.addon` for the watcher if it must compile
   resumes. Enable the approval poller on that watcher only. Never configure a
   second poller or webhook for the control bot.
6. For GPT Luna, set `AI_PROVIDER=openai_compat`, `AI_MODEL=gpt-luna`,
   `AI_API_KEY`, and the provider-supplied `AI_BASE_URL`. Until credentials are
   present, slash-command agent functions work, but free-text answers and
   description classification cannot call the model.
7. Run `python -m app.ai_pipeline.cli check-config`,
   `python -m app.ai_pipeline.cli check-destinations`, and the unit tests before
   enabling the live pipeline.

The operator confirmed that `FRESH_WATCHER_BOT_TOKEN` and
`FRESH_WATCHER_CHAT_ID` are reserved for the `1 min` destination. The addon
registry uses the new `AI_TELEGRAM_BOT_TOKEN` for `Fresher bot`,
`5 min and 30 min ago`, and the private control bot, with separate chat IDs
for each. Blank placeholders for the new bot are in the local `.env` and
`.env.example`; PDF delivery remains blocked until the bot and all three
unique chat IDs are configured and verified.

See the AI pipeline section in `README.md` for local and Render start commands.
