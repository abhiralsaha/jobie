# Jobie

Jobie is a job-tracking and job-discovery application built for job seekers who want a lightweight, automated way to monitor relevant opportunities without manually checking multiple listings every day. The project continuously scans public LinkedIn job search results, filters jobs by configured keywords and location, stores them in a persistent database, and sends Telegram alerts when new matches appear. It also supports manual job submissions and authorized feed imports, making it useful both for automated discovery and for manually curated opportunities.

## What this application does

Jobie acts as an automated job assistant that helps a user stay on top of new opportunities. In practical terms, it does the following:

- Monitors LinkedIn's public guest job-search endpoint for fresh postings that match the configured search criteria.
- Checks jobs against a time window so only recent opportunities are considered.
- Deduplicates jobs by ID and stores them in PostgreSQL/Supabase to avoid repeated alerts.
- Maintains search run history, digests, and keep-alive records for operational visibility.
- Sends Telegram notifications for new jobs, recent-job alerts, and scheduled daily or manual digests.
- Accepts jobs from other trusted sources through its import API, including manually submitted jobs and authorized feed-based ingestion.
- Provides a web API for health checks, fresh jobs retrieval, digest sending, and keep-alive requests.
- Can optionally run an AI-powered screening pipeline that reads full job descriptions, validates experience fit, and prepares resume and referral workflows.

## Core workflow

1. The main worker queries LinkedIn using the public guest job-search endpoint.
2. Results are parsed, validated, and filtered to recent jobs.
3. Duplicate jobs are ignored using the database-backed deduplication layer.
4. New jobs are stored in Postgres/Supabase and can be retrieved through the API.
5. Telegram alerts are sent for new matches or digest summaries.
6. If enabled, the AI pipeline checks the job description for fit, builds a tailored resume, identifies referral contacts, and drafts referral emails for review.
7. The user manually reviews the final opportunity and applies through the actual job portal, keeping the human in the loop.

## Key features

- Automated LinkedIn job discovery using public search endpoints.
- Real-time and periodic alerting via Telegram.
- PostgreSQL/Supabase-backed persistence and deduplication.
- Health endpoints and authenticated API routes for operational monitoring.
- Support for import of manually obtained or authorized jobs.
- Scheduled digest support and custom polling windows.
- Optional AI assist for fresher/early-career job validation, resume tailoring, and referral email generation.
- Resume template integration with LaTeX-based output and validation safeguards.
- Retry loops, circuit breaker behavior, and rate-limit-aware HTTP handling for resilience.

## Safety boundary

The LinkedIn workers use the public guest-search endpoint without a LinkedIn
login, account cookies, or stored credentials. This is not an official
LinkedIn API; endpoint behavior, availability, and access rules can change.
Follow the applicable source terms and rate limits. Jobie does not bypass
controls, solve CAPTCHAs or checkpoints, fill application forms, or submit
applications.

## Current architecture

Jobie uses PostgreSQL/Supabase for all persistent state. Set
`SUPABASE_DB_URL` to the Supabase pooled PostgreSQL connection string. The
database layer uses a thread-safe connection pool and creates the required
tables at worker/API startup.

There are two separate workers:

1. **Main search/API worker** — `app.linkedin_fresh_jobs:app` — searches the
   configured lookback window, stores and deduplicates results, serves the
   health/search/digest endpoints, and sends scheduled or manual digests.
2. **Ultra-fresh watcher** — `python -m app.fresh_watcher` — runs as a separate
   process, checks the last two minutes by default, and sends immediate alerts
   through its dedicated Telegram bot (or the main bot as fallback).

The separate import API is `app.main:app`. An authorized feed connector may
post to it, but it is not a replacement for either worker.

## Local setup (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Set real values in `.env`, especially `SUPABASE_DB_URL`,
`JOBIE_API_TOKEN` (at least 32 random characters), Telegram credentials, and
the search settings. Keep `.env`, tokens, logs, and personal data private.
There is no local SQLite database or `DB_PATH` setting in the current runtime.

Start the two workers in separate terminals:

```powershell
.venv\Scripts\python.exe -m uvicorn app.linkedin_fresh_jobs:app --host 127.0.0.1 --port 8000
.venv\Scripts\python.exe -m app.fresh_watcher
```

The main worker's readiness endpoint is `http://127.0.0.1:8000/health`.
The optional import API can run separately on port 8001:

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## Render deployment

Create two Render **Background Worker** services from this repository. Set the
same environment variables in both services, including `SUPABASE_DB_URL` and
the appropriate Telegram credentials.

Main search/API worker start command:

```text
python -m uvicorn app.linkedin_fresh_jobs:app --host 0.0.0.0 --port $PORT
```

Ultra-fresh watcher start command:

```text
python -m app.fresh_watcher
```

Render uses `runtime.txt` (Python 3.14.3). The main worker exposes
`POST /keep-alive` for the repository's optional GitHub Actions keep-alive
workflow. A keep-alive ping only records a timestamp in Supabase; it does not
create a job or send Telegram messages. Scheduled pings are best effort and
do not guarantee uninterrupted service; use an always-on plan when continuous
availability is required.

## Useful endpoints

The main worker requires `Authorization: Bearer <JOBIE_API_TOKEN>` for
`GET /fresh-jobs`, `POST /digest/send`, and `POST /keep-alive`.

```powershell
$token = (Get-Content .env | Where-Object { $_ -match '^JOBIE_API_TOKEN=' } | Select-Object -First 1) -replace '^JOBIE_API_TOKEN=', ''
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health'
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/fresh-jobs?peek=true' -Headers @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/digest/send' -Headers @{ Authorization = "Bearer $token" }
```

Import a manually obtained or authorized job through the separate API:

```powershell
curl.exe -X POST http://127.0.0.1:8001/jobs/import -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d "{\"job_id\":\"example-123\",\"title\":\"Backend Engineer\",\"company\":\"Example Inc\",\"url\":\"https://example.com/jobs/example-123\",\"source\":\"authorized_feed\"}"
```

## Security checklist

- Bind local development services to `127.0.0.1`; use HTTPS and a trusted
  reverse proxy for remote access.
- Keep `SUPABASE_DB_URL`, `JOBIE_API_TOKEN`, Telegram tokens, `.env`, logs, and
  personal documents secret.
- Restrict database access and use Supabase's connection security controls.
- Review every listing and submit applications manually on the source site.
- Use only feeds and sources you are authorized to access; honor their terms,
  rate limits, and permissions.

These safeguards reduce common risks but cannot guarantee that a host,
dependency, messaging account, or deployment is uncompromised.

## Optional AI pipeline

The integrated `app.ai_pipeline` package can screen newly found jobs against
their full descriptions, send eligible-job alerts, tailor resumes from a
user-provided fact bank, and prepare referral email drafts for explicit
Telegram approval. The normal watcher/API behavior remains in place while
`AI_PIPELINE_ENABLED=false` (the default). Keep `AI_DRY_RUN=true` until the
pipeline, Telegram destinations, resume profile, and email drafts have been
reviewed.

### Local setup

Install the project requirements, copy the example assets, and edit them with
your own resume facts and templates:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\resume_profile.example.json config\resume_profile.json
Copy-Item config\referral_template.example.txt config\referral_template.txt
Copy-Item config\company_domains.example.json config\company_domains.json
Copy-Item templates\resume_template.tex templates\resume_template.local.tex
```

The checked-in `config/telegram_destinations.json` contains environment
variable names only. The existing `FRESH_WATCHER_*` pair is the `1 min`
destination; the new add-on bot uses `AI_TELEGRAM_BOT_TOKEN` and separate chat
IDs for the other two destinations and private control chat. Add the bot to
each chat, then set those variables in `.env` and Render. The PDF allowlist
remains blocked until all three destinations resolve to unique chats.

The resume compiler reads `templates/resume_template.tex` by default. Replace
that template with your own LaTeX template and add its supported
`% JOBIE:BEGIN/END <SECTION>` markers, or set `AI_RESUME_TEMPLATE_FILE` to your
edited file. Never use the example resume facts as real candidate information.

Set the AI settings in `.env` from `.env.example`:

- `AI_PIPELINE_ENABLED=true` enables screening in the watcher and import API.
- `AI_DRY_RUN=true` records Telegram/email effects without sending them.
- Reuse an existing provider key or set `AI_PROVIDER`, `AI_API_KEY`, and
  `AI_MODEL`. Set `AI_TELEGRAM_AUTHORIZED_USER_ID` to your numeric Telegram
  user ID.
- Use the existing `SUPABASE_DB_URL` for durable pipeline state, or set
  `AI_DATABASE_URL`; SQLite is suitable only for local testing.
- Add the bot token/chat ID environment variable pairs to the destination
  registry. The registry contains variable **names**, never credentials.
- Configure SMTP and optional contact-search/Hunter keys only if you want
  those optional features.

After your destination mapping is confirmed, check configuration and run the
offline demo. The tests and demo do not send real messages or email:

```powershell
.\.venv\Scripts\python.exe -m app.ai_pipeline.cli check-config
.\.venv\Scripts\python.exe -m app.ai_pipeline.cli check-destinations
.\.venv\Scripts\python.exe -m app.ai_pipeline.cli demo
.\.venv\Scripts\python.exe -m unittest discover -s tests_ai -t .
```

Start the existing search/API worker and watcher in separate terminals as
shown above. The watcher alone calls `start_background()` and should be the
only service with `AI_RUN_APPROVAL_POLLER=true`. Set it to false on all other
services. Do not configure a webhook or another `getUpdates` poller for the
control bot. The watcher shares Jobie's existing HTTP client and rate-limit
circuit breaker for description retrieval.

### Render and resume compilation

For resume generation, configure the watcher Background Worker to build from
`deploy/Dockerfile.addon`; it includes TeX Live, `latexmk`, and Poppler. Keep
the app/search API on its existing Python runtime if desired. Set the watcher
start command to `python fresh_watcher_runner.py` and keep exactly one approval
poller enabled. Supply the same durable Supabase URL and required settings to
the processes that need them.

The watcher now expands its LinkedIn query look-back after a pause or restart
to reach back to the last successful poll (up to 24 hours). This closes the
gap where a fixed two-minute query could miss postings during a 5/15/30-minute
HTTP circuit-breaker cooldown; `seen_jobs` still deduplicates recovered jobs.

Do not set `AI_DRY_RUN=false` until the report and manually reviewed dry-run
results are correct, and only test an approved email using an address you own.
