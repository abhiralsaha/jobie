# LinkedIn Job Agent (Full Stack, 0–2 yrs → Telegram)

Every **60 seconds** it searches LinkedIn for jobs posted in the last **2 minutes**
(Gurgaon, Noida, Bengaluru), has an **LLM read each job description** to find the
required experience, and sends **only 0–2 year jobs + link** to your Telegram bot. Runs 24/7.

## 1. Get the values
1. **Telegram bot token** – talk to `@BotFather` → `/newbot`.
2. **Chat ID** – send any message to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. **LLM API key** – Anthropic key (default) or any OpenAI-compatible key.

## 2. Run locally (test)
### Windows PowerShell
```powershell
Set-Location .\linkedin-job-agent
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```
If PowerShell blocks activation, allow it for this terminal only:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

### macOS / Linux
```bash
cd linkedin-job-agent
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then edit .env with your 3 values
python run.py
```
The agent logs to the console and to `logs/agent.log` (rotates automatically). Logs include search progress, results, processing stages, errors, and durations. Health check: http://localhost:8080

### Python web-app entrypoint
For Vercel, set the project root directory to `jobie_subagent/linkedin-job-agent`;
the `pyproject.toml` there declares the ASGI entrypoint `app.main:app`. Its `/`
and `/health` routes report the agent's health, and the agent starts and stops
with the ASGI server lifecycle. The standalone `python run.py` command remains
available for local and container deployments.

## 3. Run 24/7 in production (pick one)

### A) Docker on a VPS (recommended — cheapest & most reliable)
```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```
`restart: always` restarts it after crashes/reboots; `./data` keeps the dedupe DB and `./logs` keeps the rotating application log.

### B) systemd (no Docker)
```bash
sudo mkdir -p /opt/linkedin-job-agent && sudo cp -r . /opt/linkedin-job-agent
cd /opt/linkedin-job-agent && python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo cp linkedin-job-agent.service /etc/systemd/system/   # edit User= if needed
sudo systemctl daemon-reload && sudo systemctl enable --now linkedin-job-agent
journalctl -u linkedin-job-agent -f
```

### C) Render
Use a **Background Worker** on a paid plan (`render.yaml` included, with a 1 GB disk).
Free web services sleep after inactivity and will stop a 1-minute poller — don't use free tier for 24/7.
If you must use a free web service, ping `https://<your-app>.onrender.com/` every 5 min with UptimeRobot
(and note free disks are wiped on restart, so a few jobs may repeat after redeploys).

## Config (.env)
| Var | Meaning |
|---|---|
| `SUPABASE_URL` | Supabase project URL; set with `SUPABASE_KEY` to use Supabase instead of SQLite |
| `SUPABASE_KEY` | Server-side Supabase secret/service-role key; keep private and never use a browser/publishable key |
| `DB_PATH` | SQLite file path used when Supabase is not configured (default `data/jobs.db`) |
| `LOG_LEVEL` | Logging detail: `DEBUG`, `INFO` (default), `WARNING`, or `ERROR` |
| `LOG_FILE` | Rotating log file path (default `logs/agent.log`; up to 5 MB per file, 3 backups) |
| `KEYWORDS` | comma list, e.g. `full stack developer,mern stack developer` |
| `LOCATIONS` | comma list of cities |
| `POLL_INTERVAL_SECONDS` | default 60 |
| `LOOKBACK_SECONDS` | default 120 (= "posted in last 2 min"); overlap is safe, duplicates are blocked by the DB |
| `MAX_MIN_YEARS` | job passes if its *minimum* required experience ≤ this (default 2) |
| `UNSPECIFIED_EXPERIENCE` | `skip` (default) or `send` when a post states no experience |
| `TITLE_BLOCKLIST` | titles with these words (senior, lead…) are dropped before the LLM → saves cost |

### Supabase setup
1. Create a Supabase project and copy its project URL and a server-side secret/service-role key into `SUPABASE_URL` and `SUPABASE_KEY`.
2. Run [`supabase/schema.sql`](supabase/schema.sql) in the Supabase SQL Editor to create the `jobs` table.
3. Restart the agent. If both Supabase variables are set, the job dedupe store uses Supabase; if neither is set, it uses the local SQLite database. Setting only one is an error.

The key is privileged and must stay server-side. The schema enables row-level security without public policies; the server-side key is required for this app's access.

At the default `INFO` level, the logs show readable search progress, jobs found, alerts sent, errors, and elapsed durations. Set `LOG_LEVEL=DEBUG` for technical details such as individual database lookups and HTTP requests.

### Troubleshooting
- **Supabase `Invalid API key`:** `SUPABASE_KEY` must be a Supabase API secret/service-role key from the project's **API Keys** settings. Do not use the database password, pooler hostname, or PostgreSQL connection string.
- **Telegram `chat not found`:** Telegram does not let a bot initiate a private conversation. Open the bot and send any message (the `/start` command is not required), then set `TELEGRAM_CHAT_ID` to that private chat's ID. Confirm the bot token belongs to the same bot. For a group, add the bot and use that group's chat ID. The agent exits if its startup notification cannot be delivered.
- **LinkedIn search returns 404:** the agent uses LinkedIn's public guest endpoint, which LinkedIn may change or block. Update the app if the endpoint changes again.

## How it avoids the 429 problem
- Only `keywords × locations` search calls per minute (3 by default) + one detail call per *new* job.
- Random delays between calls, rotating User-Agents, and a **dedupe DB** so a job is fetched/analysed once.
- On a 429, it enters an **exponential cooldown** (1 → 2 → 4 … up to 15 min) and resumes automatically.
- If you still get blocked often, run it from a VPS/home IP rather than a shared cloud IP, or raise `POLL_INTERVAL_SECONDS`.

## Notes
- Uses LinkedIn's public guest endpoints (no login). LinkedIn can change its HTML; selectors live in `app/linkedin.py`.
- Scraping may conflict with LinkedIn's Terms of Service — use for personal job hunting at your own risk.
