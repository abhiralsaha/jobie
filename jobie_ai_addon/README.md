# Jobie AI add-on

Drop-in module for the **Jobie** project (`app/fresh_watcher.py`, Telegram alerts, Supabase/Render).
It adds, behind one hook function:

1. **AI-verified 0-2 year filtering** on the *full* job description (AI proposes, deterministic code can only veto).
2. **Job alerts to every configured job-alert destination** (not capped at three).
3. **Job-specific LaTeX resumes** built from *your* template, compiled, validated, and sent **only** to the three allow-listed chats
   (`1 min`, `Fresher bot`, `5 min and 30 min ago`) - fail-closed.
4. **Referral contacts** (HR, Senior SWE, SWE) from legitimate search/verification services, with honest verification status.
5. **Personalised referral e-mails that can only be sent after you tap Approve** in your private Telegram control bot.
6. Persistence, idempotency, retries with backoff, non-overlapping scheduler, dry-run mode, and 134 automated tests.

> **Read this first - what has and has not been verified.**
> This package was written against the *project overview* you pasted, **not against your actual source files** (I could not open them).
> Everything inside `app/ai_pipeline/` is self-contained and tested (134 tests, real `pdflatex` compiles included), but the
> *three places where it touches your existing code* (see `INTEGRATION.md`) are small snippets you / your coding agent must apply.
> I have **not** diagnosed your Fresher-Watcher bug (no code access); see "Watcher notes" below for a concrete, testable hypothesis.
> Postgres mode is written to the SQL subset both engines share but was only exercised on SQLite here.
> No real Telegram, SMTP, LLM, Hunter or search API was called (none are reachable from where this was built); they are covered by mocks.

## Quick start (about 10 minutes)

```bash
unzip jobie_ai_addon.zip -d /path/to/your/jobie     # merges into your repo; overwrites nothing of yours
pip install -r requirements-addon.txt
python -m app.ai_pipeline.cli demo                  # offline walk-through: proves the flow on your machine, sends nothing
cp config/telegram_destinations.example.json config/telegram_destinations.json
cp config/resume_profile.example.json        config/resume_profile.json       # fill with YOUR real facts
cp config/referral_template.example.txt      config/referral_template.txt     # paste YOUR e-mail template
python scripts/inspect_env.py                       # lists your existing Telegram env var NAMES
python -m app.ai_pipeline.cli check-config
python -m app.ai_pipeline.cli check-destinations    # verifies bot/chat access WITHOUT sending
```
Then follow **INTEGRATION.md** (3 small edits) and keep `AI_DRY_RUN=true` until everything looks right.
If you use a coding agent with access to your repo, paste **docs/AGENT_PROMPT.md** into it - it does the wiring for you.

Requires Python 3.9+ (tested on 3.12). LaTeX tools: `latexmk`/`pdflatex` + `pdftotext` (poppler) for resumes.

## Layout

```
app/ai_pipeline/
  hook.py          <- the ONLY entry point your code calls (on_new_job, start_background, set_http_get)
  pipeline.py      stages + retries + recovery        classifier.py  AI + deterministic validation
  jd_fetch.py      full description retrieval          delivery.py    alerts + resume PDFs (idempotent)
  destinations.py  Telegram registry + 3-chat allowlist telegram.py   Bot API client (429, splitting, no token leaks)
  resume.py        fact-bank tailoring + LaTeX         contacts.py    people + email verification
  referral.py      personalised drafts                 approval.py    Approve/Reject state machine + poller
  mailer.py        SMTP with honest outcomes           llm.py         Anthropic / OpenAI-compatible / Gemini
  store.py         SQLite or Postgres                  scheduler.py, freshness.py   watcher utilities
  fakes.py, demo.py, cli.py
config/   *.example.* files          templates/resume_template.tex (sample - replace with yours)
tests_ai/ 134 tests                  deploy/Dockerfile.addon (Render has no LaTeX)
docs/AGENT_PROMPT.md                 INTEGRATION.md
```

## How a job flows

```
fresh_watcher inserts job -> ai_hook.on_new_job()  (returns immediately; work runs on a thread pool)
  title is obviously senior? -> excluded (no fetch, no AI spend)
  fetch FULL description (retry/backoff; shared 429 cool-down) -> truncated/missing => never verified
  regex pre-screen: every stated requirement > 2 years (and no fresher wording) => excluded without calling the AI
  AI -> JSON schema -> deterministic validation -> include | exclude | review      (cached per description hash)
  include  -> alert to ALL job-alert destinations (per-destination idempotent)
           -> resume: AI selects/rephrases from your fact bank -> render into your template -> latexmk -> validate PDF
           -> PDF + job info to the 3 allow-listed chats ONLY
           -> contacts (HR / Senior SWE / SWE) -> e-mail drafts -> control bot: summary + 3 approval cards
  you tap Approve -> exact draft + exact PDF sent via your SMTP account; Reject / no tap -> nothing is ever sent
```

## Eligibility rules (defaults, all overridable)

* `include` only if **all** hold: description fully retrieved (checked by us, not just claimed by the AI); the AI's evidence quotes occur
  **verbatim** in the description; at least one quote is an experience statement; the AI's numbers agree with an independent regex read of the
  text; the range fits **0-2 years** (`min <= 2` and `max <= 2`; `AI_MAX_REQUIRED_MAX_YEARS=3` would also accept "1-3 years").
* `exclude`: states more than 2 years (3+, 4+, senior/lead/staff/principal titles).
* `review` (stored for audit, **never alerted**): no experience statement, contradictory statements, unverifiable evidence, incomplete description, AI/regex disagreement.
* The posting date is never sent to the AI, so publication age cannot be mistaken for required experience.
* AI outage / malformed JSON / rate limits => the job is retried with exponential backoff; it is never accepted by default.
* Fail-closed trade-off: a sentence like "our company has 10 years of experience" can send a job to `review`. That is deliberate.

## Resume integrity (how "never invent anything" is enforced)

* `config/resume_profile.json` is the only source of facts. The AI returns **JSON selecting/ordering bank items**, never free LaTeX.
* Skills must exist in your bank. A rewritten bullet that adds a number or a known technology not in its source bullet is **rejected and the original
  wording is used**. A summary with new numbers, years-of-experience claims or new technologies falls back to your base summary.
* Only text between `% JOBIE:BEGIN/END <SECTION>` markers in your template is replaced; the rest of your design is untouched. The original template is
  never modified (checked by hash in tests). Output: `resumes/<Company>_<Title>_<job_id>.pdf` + `.tex`.
* Validation: valid PDF header/trailer, page limit (default 1), extractable text, your name present, required headings present, no `U+FFFD`/`(cid:`/`??`,
  no large Overfull-hbox in the log. Compile failure => retry with your untailored defaults => otherwise reported, **never sent**.
* Limit: the tech-term check uses `app/ai_pipeline/data/tech_lexicon.txt` (about 120 terms). Add terms you care about.

## Telegram routing

* Alerts -> every destination with `"job_alerts": true`. Resume PDFs -> only destinations named exactly `1 min`, `Fresher bot`, `5 min and 30 min ago`
  and flagged `"resume_pdf": true`. If any of the three is missing, duplicated, unresolved, shares a chat with another, or **another** destination is flagged,
  `resume_destinations()` raises and **no PDF is sent** (tested). `TelegramClient.send_document` independently refuses non-flagged destinations.
* Tokens are never written to JSON - the file holds environment-variable **names**.
* The control bot receives the review package (job, contacts, verification status, drafts, resume filename + SHA-256) but **not the PDF itself**
  (you asked for PDFs to go to the three chats only; the same file/SHA was delivered there). Approve sends the exact attached file.

## Referral e-mails: approval guarantees (all tested)

Only `AI_TELEGRAM_AUTHORIZED_USER_ID` in the control chat can act. Buttons carry draft id + 12 hex of the content hash. `pending -> sending` is one atomic
UPDATE, so double taps, replayed Telegram updates and parallel workers cannot send twice. If the subject/body/recipient or the PDF changed after the request
the draft becomes `stale` and nothing is sent. Rejection, silence, expiry (`AI_APPROVAL_EXPIRE_DAYS`), restarts and API errors never approve anything.
"Sent" means *the SMTP server accepted the message* - it is not an inbox-delivery guarantee. A connection lost mid-transfer becomes `send_unknown`
(check your Sent folder; never auto-resent). A refusal before submission offers a **Retry** button (your explicit tap again).
Contacts whose e-mail is not `verified_service` / `public_evidence` get a draft but **no Approve button**.

## Contacts: what it can and cannot do

* People come only from a search API you configure (Brave / Google CSE / SerpAPI) returning public profile titles/snippets - no LinkedIn login or scraping.
  Current employment is judged from that snippet (former/ex- mentions rejected) and labelled "search-snippet only (not independently confirmed)".
* E-mails: Hunter (authorised service) verification, or an address literally present in public evidence. MX checks never upgrade a status.
* Free-tier quotas are real: caps `SEARCH_DAILY_CAP`, `HUNTER_DAILY_CAP`, `REFERRAL_DAILY_JOB_CAP` are enforced; remaining jobs still get alerts and resumes.
* Without a search provider configured you simply get no contacts (and the package says so) - nothing is made up.

## Watcher notes (hypothesis, not a diagnosis)

From the overview: polling pauses 5/15/30 min after HTTP 429 (circuit breaker) but the look-back stays `f_TPR=r120`. Any job posted *during* a pause is
older than 120 s when polling resumes and is never seen. `freshness.catchup_lookback_seconds()` widens the window to reach back to the last successful
poll (capped), and `seen_jobs` de-duplicates. Also check: relative-time rounding ("2 minutes ago" can mean up to 2m59s), jitter pushing gaps beyond the window,
and the 2-minute freshness filter being applied *after* parsing. `scheduler.FixedRateScheduler` gives you non-overlapping, no-burst-after-stall polling.
Verify against your logs before concluding anything.

## Responsible-use notes

Fetching each new posting adds requests to LinkedIn's guest endpoint, which is already rate-limiting you: route it through your existing HTTP client
(`ai_hook.set_http_get`) so the circuit breaker is shared, and keep the title pre-filter on. Cold outreach should stay low-volume - that is what the approval gate is for.

## Commands

```bash
python -m app.ai_pipeline.cli check-config | check-destinations | init-db | demo | report | worker
python -m app.ai_pipeline.cli classify job.json     # {"job_id","title","company","description"}
scripts/run_tests.sh                                # or: python -m unittest discover -s tests_ai -t .
```
