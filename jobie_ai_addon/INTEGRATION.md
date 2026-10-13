# Connecting the add-on to your existing Jobie code

Nothing here replaces your working code. The add-on is switched by `AI_PIPELINE_ENABLED`; when it is false your current behaviour is untouched.

## 1. Files and dependencies
Unzip into the repo root (adds `app/ai_pipeline/`, `config/`, `templates/`, `tests_ai/`, `docs/`, `deploy/`, `scripts/inspect_env.py`, `scripts/run_tests.sh`).
Append `gitignore.snippet` to your `.gitignore`. `pip install -r requirements-addon.txt`.
Expected layout: your package is `app/` and modules import as `app.xxx` (as in your overview). If your layout differs, only `hook.py` imports matter - everything else is relative.

## 2. Environment (Render dashboard / `.env`) - see `.env.ai.example`
Start with `AI_PIPELINE_ENABLED=true`, `AI_DRY_RUN=true`. Your existing AI key variable is auto-detected (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`) or set `AI_PROVIDER` + `AI_API_KEY` + `AI_MODEL`. Set `AI_TELEGRAM_AUTHORIZED_USER_ID` to your numeric Telegram id.
State: set `AI_DATABASE_URL` (or reuse `DATABASE_URL`/`SUPABASE_DB_URL`) so state survives Render restarts; SQLite on Render's free disk is wiped on every deploy.

## 3. Telegram mapping (the step that must be exact)
`python scripts/inspect_env.py` prints the variable NAMES your project already uses. In `config/telegram_destinations.json` give each of your destinations an entry whose
`name` is exactly how you call it (`1 min`, `Fresher bot`, `5 min and 30 min ago`, plus your other digest bots), pointing at those env var names.
Mark `"resume_pdf": true` on those three only; add `"control"` for your private review bot. Then `python -m app.ai_pipeline.cli check-destinations`.
Nothing is guessed from display names: unresolved or ambiguous => resume PDFs are not sent.

## 4. Three edits in your code
**a) `app/fresh_watcher.py`** - where a job has just been inserted into `seen_jobs` (`rowcount == 1`) and today `TelegramNotifier.send_alert(...)` fires:
```python
from app.ai_pipeline import hook as ai_hook
...
if ai_hook.enabled():
    ai_hook.on_new_job(job_id=job_id, title=title, company=company, location=location, url=url, posted_at=posted_at)
else:
    notifier.send_alert(...)          # your existing behaviour, unchanged
```
At process start (once): `if ai_hook.enabled(): ai_hook.start_background()`.
With the add-on on, the raw "every new job" alert is replaced by the filtered one (that is what "send only eligible jobs" means). The dashboard keeps working: `seen_jobs` inserts are unchanged.

**b) Share your HTTP client / circuit breaker for description fetches** (recommended; you already get 429s). Adapt to your `http_client` API:
```python
from app.ai_pipeline.jd_fetch import FetchRateLimited, FetchError
def _http_get(url: str) -> str:
    resp = http_client.get(url)                       # <- your existing function
    if resp.status_code == 429:
        raise FetchRateLimited(float(resp.headers.get("Retry-After") or 0))
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    return resp.text
ai_hook.set_http_get(_http_get)
```
**c) `app/main.py` `POST /jobs/import`** - after storing an imported job: `ai_hook.on_new_job(..., description=item.get("description"), source="authorized_feed")`
(a supplied description avoids the fetch entirely).

Optional watcher hardening (see README "Watcher notes"): `freshness.catchup_lookback_seconds()` for `f_TPR`, `scheduler.FixedRateScheduler` for the 60 s loop.

## 5. Processes
* Run the **approval poller in exactly one process** (`AI_RUN_APPROVAL_POLLER=true` there, `false` elsewhere): Telegram allows one `getUpdates` consumer per bot, and a webhook on the control bot conflicts with it (409).
* Render free tier sleeps: Approve taps are queued by Telegram and handled after wake-up; the add-on never treats the delay as approval.
* **LaTeX:** Render's native Python runtime has none. Use `deploy/Dockerfile.addon` (TeX Live + poppler) for the service that builds resumes.
* Alternatively run `python -m app.ai_pipeline.cli worker` as a separate background worker that only handles retries + approvals.

## 6. Go-live checklist
1. `cli demo` OK, `scripts/run_tests.sh` OK on the target machine/container (LaTeX tests auto-skip without a compiler).
2. `check-config` shows no problems; `check-destinations` is all OK.
3. Dry-run for a day: run `python -m app.ai_pipeline.cli report` and spot-check includes/excludes against the live postings.
4. Set `AI_DRY_RUN=false`. First real job: confirm alerts reached all destinations, PDFs only the three, approvals only your control bot.
5. Send a first referral only to an address you own to verify SMTP (use your own address as the draft recipient in a test job).
