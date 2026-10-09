# Jobie

Jobie is a human-review job opportunity tracker. It can monitor LinkedIn's
public guest job-search endpoint and send Telegram notifications, or accept
jobs from manual imports and authorized feeds. Applications remain manual.

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
