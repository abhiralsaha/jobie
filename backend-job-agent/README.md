# LinkedIn Backend Job Agent (0–2 yrs → Telegram, Supabase DB)

Every **60 seconds** it searches LinkedIn for **backend developer** jobs posted in the last **2 minutes**
(Gurgaon, Noida, Bengaluru). An **LLM reads each job description**, finds the required experience, and only
**0–2 year** jobs are sent to your **Telegram bot with the job link**. Seen/sent jobs are stored in **Supabase**
so nothing repeats, even after restarts or redeploys. Runs 24/7.

## 1. One-time setup
1. **Supabase**: create a project → *SQL Editor* → paste contents of `schema.sql` → *Run*.
   You can safely rerun this script for an existing table; it adds the `created_at` column if
   missing so automatic cleanup can work. Then *Project Settings → API*: copy **Project URL**
   and the **service_role** key.
2. **Telegram**: `@BotFather` → `/newbot` → copy token. Send your bot a message, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `chat.id`.
3. **LLM key**: Anthropic (default) or any OpenAI-compatible key.

## 2. Run locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in Telegram, LLM and Supabase values
python run.py
```
You get "✅ Job agent started" on Telegram. Health check: http://localhost:8080
At startup, the agent makes one small LLM verification request and logs whether the configured
provider returned a parseable response. Each real job analysis is also logged separately.
The same worker also sends two HTTP GET keep-alive requests to
`https://jobie-2.onrender.com` at startup and every 13 minutes. This is an HTTP ping, not a
browser click; it runs in the background for as long as `python run.py` is running.

## 3. Run 24/7
**Docker on a VPS (recommended)**
```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```
**systemd (no Docker)**
```bash
sudo mkdir -p /opt/linkedin-job-agent && sudo cp -r . /opt/linkedin-job-agent
cd /opt/linkedin-job-agent && python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo cp linkedin-job-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now linkedin-job-agent
journalctl -u linkedin-job-agent -f
```
**Render**: use a *Background Worker* on a paid plan (`render.yaml` included; add env vars in the dashboard).
Free web services sleep and will stop a 1-minute poller. Since the DB is Supabase, no disk is needed.

## Config (.env)
| Var | Meaning |
|---|---|
| `KEYWORDS` | comma list, e.g. `backend developer,python developer,java developer` |
| `LOCATIONS` | comma list of cities |
| `LLM_PROVIDER` | `anthropic` or an OpenAI-compatible provider such as `openrouter` |
| `LLM_MODEL` | Model identifier supported by the configured provider |
| `LLM_BASE_URL` | API base URL for OpenAI-compatible providers (OpenRouter: `https://openrouter.ai/api/v1`) |
| `KEEPALIVE_URL` | URL to ping (default `https://jobie-2.onrender.com`) |
| `KEEPALIVE_INTERVAL_SECONDS` | Seconds between keep-alive batches (default `780`, or 13 minutes) |
| `POLL_INTERVAL_SECONDS` | default 60 |
| `LOOKBACK_SECONDS` | default 120 (= last 2 min); overlap is safe thanks to the DB |
| `MAX_MIN_YEARS` | pass if the job's *minimum* experience ≤ this (default 2) |
| `UNSPECIFIED_EXPERIENCE` | `skip` (default) or `send` if a post states no experience |
| `TITLE_BLOCKLIST` | titles with these words are dropped before the LLM (saves cost) |

## Supabase tips
- Table `backend_dev` also works as your history: filter `status = 'sent'` to see everything alerted.
- Statuses: `sent`, `rejected` (experience too high), `title_blocked`, `failed`. Failed rows are
  retryable; only the first three statuses are treated as completed by deduplication.
- Rows older than 30 days are auto-deleted.
- Use the **service_role** key only on your server; never commit `.env`.

## 429 handling
Few calls per minute (keywords × locations), random delays, rotating User-Agents, dedupe, and exponential
cooldown (1→2→4… up to 15 min) on 429. A VPS/home IP is blocked less than shared cloud IPs.
The LLM client serializes requests and retries transient 429/5xx/network failures up to three times,
honoring `Retry-After` when supplied. Failed jobs remain eligible for a later poll cycle. If OpenRouter
continues returning 429 with an account-credit/admission-control message, check account credits and wait
for the provider's reset; retrying cannot resolve an account-side credit or availability restriction.

## Troubleshooting
- **LinkedIn search returns 404:** the agent uses LinkedIn's public guest-search endpoint, which LinkedIn may change, block, or disable. Search errors are logged per keyword/location and the agent continues, but it cannot discover jobs until that endpoint is available again.
- **LLM returns 401 with a valid key:** make sure `LLM_PROVIDER`, `LLM_BASE_URL`, and `LLM_MODEL` all belong to the same provider. For example, an OpenRouter key and model require `LLM_BASE_URL=https://openrouter.ai/api/v1`, not OpenAI's URL. Update these values in the Render environment too if the worker is deployed there.
- **Job description is missing:** LinkedIn may return a sign-in, block, or changed-layout page instead of the job details. The agent now reports that no description element was found and avoids sending unrelated page text to the LLM.
- **Supabase cleanup returns 400:** rerun `schema.sql` in the Supabase SQL Editor. It adds the `created_at` timestamp column if missing. A cleanup failure does not stop job searches; cleanup is retried later.
- **Stopping in PowerShell:** use `Ctrl+C`; the agent handles the interrupt and closes its network clients cleanly.

## Notes
Uses LinkedIn's public guest endpoints (no login); HTML can change — selectors are in `app/linkedin.py`.
Scraping may conflict with LinkedIn's Terms of Service; use for personal job hunting at your own risk.
