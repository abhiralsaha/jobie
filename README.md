# Jobie

Jobie is a human-review job opportunity tracker. It can poll LinkedIn's public guest job-search endpoint without logging in, or accept jobs from manual imports and authorized feeds. New, recent jobs can trigger Telegram notifications; applications remain manual.

## Safety boundary

The optional LinkedIn worker requests public guest job-search results without logging in or using account cookies. This is not an official LinkedIn API; endpoint behavior and access may change, and automated access may be restricted by LinkedIn's terms. Review and follow the applicable terms. Jobie does not bypass controls, solve challenges, fill application forms, or submit applications.

The system is not legal advice. Check the terms and API rules for every source you use.

## What it does

- accepts one validated job at a time through `POST /jobs/import`
- optionally polls public LinkedIn guest search results in the background
- detects whether an imported job was posted within the recent alert window
- deduplicates jobs by your supplied ID
- stores local status in SQLite
- optionally sends a Telegram review reminder
- parses `YES`/`NO` replies as a local review decision
- logs operational events
- never applies or submits on your behalf

## Requested features, implemented safely

| Requested capability | Safe implementation |
|---|---|
| LinkedIn login automation | Not implemented. Log in manually on the source website. |
| Persistent LinkedIn cookies/session storage | Not implemented. Jobie stores no source-site cookies or credentials. |
| LinkedIn search | Optional unauthenticated guest-search worker; subject to endpoint availability and applicable terms. |
| Playwright dependency | Deliberately excluded because it is unnecessary for this review workflow. |
| Automatic application submission | Not implemented. Applications remain manual and user-controlled. |
| CAPTCHA/checkpoint handling | Not implemented and never bypassed. Follow the source site's own verification flow manually. |
| LinkedIn credential configuration | Not used. No LinkedIn username, password, or session cookie is accepted by Jobie. |

The LinkedIn worker only searches and reports public job listings. It does not interact with applications or attempt to evade platform controls.

## Setup on Windows

```powershell
cd C:\Users\abhiral\Desktop\jobie
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m scripts.bootstrap_db
```

Edit `.env` with your own settings. Generate a strong `JOBIE_API_TOKEN` (for example, `python -c "import secrets; print(secrets.token_urlsafe(32))"`) and use it for API imports and the authorized-feed connector. Keep secrets in `.env`; do not commit `.env`, your database, resume, or personal documents.

For automated LinkedIn search, set the following in `.env` (change the search terms and location to match your target roles):

```env
LINKEDIN_SEARCH_KEYWORDS=software engineer
LINKEDIN_SEARCH_LOCATION=Gurugram, Haryana, India
LINKEDIN_POLL_INTERVAL_SECONDS=120
LINKEDIN_LOOKBACK_HOURS=24
LINKEDIN_MAX_SEARCH_PAGES=5
RECENT_JOB_WINDOW_SECONDS=180
DAILY_DIGEST_TIME=18:00
DAILY_DIGEST_TIMEZONE=Asia/Kolkata
TELEGRAM_BOT_TOKEN=your-bot-token-from-botfather
TELEGRAM_CHAT_ID=your-private-chat-id
TELEGRAM_DIGEST_BOT_TOKEN=your-second-bot-token-from-botfather
TELEGRAM_DIGEST_CHAT_ID=your-second-bot-private-chat-id
TELEGRAM_DIGEST_INTERVAL_HOURS=10
TELEGRAM_DIGEST_5M_BOT_TOKEN=bot-token-from-botfather
TELEGRAM_DIGEST_5M_CHAT_ID=private-chat-id
TELEGRAM_DIGEST_15M_BOT_TOKEN=bot-token-from-botfather
TELEGRAM_DIGEST_15M_CHAT_ID=private-chat-id
TELEGRAM_DIGEST_30M_BOT_TOKEN=bot-token-from-botfather
TELEGRAM_DIGEST_30M_CHAT_ID=private-chat-id
TELEGRAM_DIGEST_1H_BOT_TOKEN=bot-token-from-botfather
TELEGRAM_DIGEST_1H_CHAT_ID=private-chat-id
JOBIE_API_TOKEN=your-generated-api-token
```

Create each Telegram bot with [@BotFather](https://t.me/BotFather), send `/start` to it, and put that bot's token and private chat ID in `.env`. The primary bot uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Optional rolling bots use the `TELEGRAM_DIGEST_*` settings for 5 minutes, 15 minutes, 30 minutes, 1 hour, and 10 hours. Each sends after the startup search and repeats at its own interval, including jobs posted in that time window. An incomplete or malformed pair disables only that bot; the others continue. Keep bot tokens private. Jobie sends messages only; it does not receive bot replies. No public webhook or tunnel is needed.

Then start the background search service:

```powershell
.venv\Scripts\python.exe -m uvicorn app.linkedin_fresh_jobs:app --host 127.0.0.1 --port 8000
```

It searches without a LinkedIn login, deduplicates job IDs in SQLite, and sends an immediate Telegram alert for jobs posted within the recent-job window. It also stores matching listings from the last `LINKEDIN_LOOKBACK_HOURS` hours and sends one daily Telegram digest at `DAILY_DIGEST_TIME` in `DAILY_DIGEST_TIMEZONE`. The search checks at most `LINKEDIN_MAX_SEARCH_PAGES` pages per poll. Keep the worker running through the configured digest time. The digest contains listings the worker successfully retrieved and stored.

Check worker readiness and recent poll status at `http://127.0.0.1:8000/health`. It reports safe status metadata only and returns HTTP 503 before a successful poll or when polling is stale.

To request a digest immediately when matching jobs are available, use the authenticated endpoint. If none are found, no Telegram message is sent:

```powershell
$apiToken = (Get-Content .env | Where-Object { $_ -match '^JOBIE_API_TOKEN=' } | Select-Object -First 1) -replace '^JOBIE_API_TOKEN=', ''
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/digest/send' -Headers @{ Authorization = "Bearer $apiToken" }
```

Manual digest requests are limited to three per local day and one every five
minutes by default. Configure `MANUAL_DIGEST_MAX_PER_DAY` and
`MANUAL_DIGEST_COOLDOWN_SECONDS` in `.env`; reservations persist in SQLite across
restarts. These limits do not affect the scheduled daily digest.

`GET /fresh-jobs` exposes the current in-memory buffer and requires the same bearer token; it is optional and is not required for Telegram alerts.

```powershell
$apiToken = (Get-Content .env | Where-Object { $_ -match '^JOBIE_API_TOKEN=' } | Select-Object -First 1) -replace '^JOBIE_API_TOKEN=', ''
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/fresh-jobs?peek=true' -Headers @{ Authorization = "Bearer $apiToken" }
```

The worker polls every 120 seconds by default and alerts for newly found jobs posted within the last 180 seconds, giving each poll a one-minute buffer. It only alerts once per job ID. LinkedIn's guest endpoint and page markup can change or restrict automated requests.

To run the separate import API instead:

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check it:

```powershell
curl.exe http://127.0.0.1:8000/health
```

## Import a job

Use a job URL and data you obtained manually or through a permitted official source:

```powershell
$apiToken = (Get-Content .env | Where-Object { $_ -match '^JOBIE_API_TOKEN=' } | Select-Object -First 1) -replace '^JOBIE_API_TOKEN=', ''
curl.exe -X POST http://127.0.0.1:8000/jobs/import -H "Authorization: Bearer $apiToken" -H "Content-Type: application/json" -d "{\"job_id\":\"example-123\",\"title\":\"Backend Engineer\",\"company\":\"Example Inc\",\"url\":\"https://example.com/jobs/example-123\",\"source\":\"official_feed\"}"
```

The endpoint rejects requests without this bearer token. Keep the service bound to localhost during development; use HTTPS and a trusted reverse proxy if it must be reachable remotely.

A first import returns `imported: true`. Sending the same `job_id` again returns `duplicate: true` and does not send another alert.

If `posted_at` is within `RECENT_JOB_WINDOW_SECONDS`, the response includes `recent: true` and Jobie attempts an optional Telegram review alert. Configure the window in `.env`.

For batch imports, copy `config/jobs.example.csv`, fill it with records from an authorized export or official feed, and run:

```powershell
.venv\\Scripts\\python.exe -m scripts.import_jobs_csv path\\to\\jobs.csv
```

## Authorized feed connector

If you have an authorized JSON feed, configure it in `.env`:

```env
AUTHORIZED_JOB_FEED_URL=https://your-authorized-provider.example/jobs.json
AUTHORIZED_JOB_FEED_TOKEN=optional-bearer-token
JOBIE_API_URL=http://127.0.0.1:8000/jobs/import
JOBIE_API_TOKEN=your-generated-api-token
FEED_POLL_SECONDS=120
```

Configure the authorized provider's feed URL or search settings for the job domain and location you want to monitor. This separate feed connector only calls that configured provider; LinkedIn guest search is handled by the optional background worker described above.

The feed must return either a JSON list or an object containing a `jobs` list. Each job needs `job_id`, `title`, `company`, `url`, and an ISO-8601 `posted_at`, for example:

```json
{
	"jobs": [
		{
			"job_id": "job-123",
			"title": "Backend Engineer",
			"company": "Example Inc",
			"url": "https://example.com/jobs/job-123",
			"posted_at": "2026-09-27T10:30:00Z",
			"source": "authorized_feed"
		}
	]
}
```

Start the connector in another terminal after the API is running:

```powershell
.venv\\Scripts\\python.exe -m scripts.poll_authorized_feed
```

For a one-time test instead of a continuous poll:

```powershell
.venv\\Scripts\\python.exe -m scripts.poll_authorized_feed --once
```

The connector polls the provider every 120 seconds by default and posts records to Jobie. Jobie deduplicates each job and sends a Telegram notification with its link when `posted_at` is within `RECENT_JOB_WINDOW_SECONDS`. Use a provider feed that retains newly posted jobs to avoid gaps. Only configure a feed you are authorized to use.

## Telegram notifications

Telegram alerts and digests are sent through the official Bot API using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. This integration is outbound-only; incoming bot messages and reply handling are not supported.

## Candidate profile

`config/candidate_profile.json` is for your own review workflow and reusable notes. It is not used to auto-fill or submit forms. Do not store passwords, MFA codes, or other credentials in it.

## Project layout

- `app/main.py` - API and validated job intake
- `app/job_intake.py` - normalization and deduplication
- `app/db.py` - SQLite schema and connection
- `app/messaging.py` - Telegram Bot API alerts and digest messages
- `app/linkedin_fresh_jobs.py` - optional public guest-search poller and Telegram alerts
- `app/scheduler.py` - idle maintenance worker
- `config/candidate_profile.json` - local candidate information
- `scripts/bootstrap_db.py` - database initialization

## Security checklist

- bind locally during development with `127.0.0.1`
- keep `JOBIE_API_TOKEN` and `TELEGRAM_BOT_TOKEN` secret
- use HTTPS and a trusted reverse proxy before remote deployment
- limit inbound request sizes and restrict inbound network access at the firewall/reverse proxy
- rotate provider API keys if exposed
- keep personal data, resumes, `.env`, logs, and `jobie.db` out of version control
- review every job and submit applications manually on the source site
- honor the terms, rate limits, and permissions of every job source

These safeguards reduce common risks; they cannot guarantee that a host, dependency, messaging account, or deployment is uncompromised. Do not expose the development server directly to the public internet. Back up the SQLite database securely and restrict access to the machine and `.env` file.

## Explicit non-goals

Jobie intentionally does not provide browser login automation, cookie persistence, scraping, CAPTCHA or checkpoint handling, application form automation, automatic submission, or evasion of platform controls.
