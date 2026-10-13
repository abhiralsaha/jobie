# Paste this into your coding agent (the one that can open your repo)

You are working in the **Jobie** repository. A self-contained add-on has been unzipped into it: `app/ai_pipeline/` (+ `config/`, `templates/`,
`tests_ai/`, `INTEGRATION.md`, `README.md`). **Do not rewrite the add-on; wire it in.** Read `README.md` and `INTEGRATION.md` first.

Tasks, in order - stop and report if any step fails:

1. Run `python -m unittest discover -s tests_ai -t .` and `python -m app.ai_pipeline.cli demo`. Report results. Do not edit add-on code to make tests pass without telling me why.
2. Inspect `app/fresh_watcher.py`, `app/linkedin_fresh_jobs.py`, `app/messaging.py`, `app/main.py`, `app/config.py`, `app/http_client.py`, `.env.example`. Then apply the three edits in
   `INTEGRATION.md` section 4 (hook call where a new job is inserted; `set_http_get` adapter using the project's real HTTP client and circuit breaker; hook call in `/jobs/import`).
   Keep the old alert path when `AI_PIPELINE_ENABLED` is false. Do not create a second scheduler or Telegram client.
3. Trace the Fresher Watcher end to end and find the real root cause of missed jobs. Check specifically: look-back (`f_TPR=r120`) vs 429 circuit-breaker pauses, relative-time rounding,
   jitter vs window, and where the 2-minute freshness filter is applied. If confirmed, fix it using `freshness.catchup_lookback_seconds` and add a regression test. Do not claim a root cause you did not reproduce.
4. Build `config/telegram_destinations.json` from the project's real env var names (never put token values in files). The three resume destinations must be named exactly
   `1 min`, `Fresher bot`, `5 min and 30 min ago`. Identify which existing variables correspond to each; if any mapping is ambiguous, STOP and ask me. Run `check-destinations`.
5. Add the new variables from `.env.ai.example` to `.env.example` (no secrets). Tell me which values I must set in Render.
6. Update the Render service for LaTeX (`deploy/Dockerfile.addon`) and ensure only one process runs the approval poller.
7. Run the full test suite again plus any existing project tests. Report exact output, remaining blockers, and the exact start commands.

Constraints: never log or commit secrets; keep `AI_DRY_RUN=true` in any config you create; do not send real Telegram messages or e-mails during testing.
