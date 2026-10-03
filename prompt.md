# LinkedIn Easy Apply Monitor & Auto-Apply MVP

> Model preference: Claude Opus 5 — https://kie.ai/claude-opus-5

## Overview
This project is a low-volume, supervised job application assistant designed to monitor LinkedIn Easy Apply listings, prioritize fresh opportunities, and route them through a human approval flow before applying. The system is intentionally simple at the MVP stage: a Playwright-based browser session, a SQLite-backed state store, a WhatsApp alert layer, and a lightweight webhook server to accept user replies.

> Important: LinkedIn's user agreement and platform policies do not permit automated application workflows in many cases. This project should be treated as a risk-aware prototype for learning, testing, and internal automation under strict human oversight, not as a guarantee of compliant usage.

---

## Objective
Build a small but reliable automation stack that can:

- maintain a persistent LinkedIn session without repeated login churn
- scrape newly posted Easy Apply jobs by search criteria
- skip duplicates and track previously seen jobs
- alert the user quickly on fresh roles through WhatsApp
- wait for human approval before starting the application flow
- fill standard Easy Apply form fields from a candidate profile
- log every action for debugging and operational review
- run continuously on a cheap VPS or local always-on machine

---

## MVP Architecture

### Core components
- Automation: Python + Playwright
- Browser mode: real Chrome, not headless, for the most realistic session stability
- Scheduler: simple loop with sleep, or cron/systemd service
- State layer: SQLite database
- Messaging: Twilio WhatsApp sandbox for initial testing
- Webhook endpoint: FastAPI service
- Candidate config: JSON file containing profile values and answers
- Logging: local file logs for audit trail and troubleshooting

### Why this stack
- Playwright handles modern JavaScript-heavy LinkedIn pages better than older automation stacks.
- SQLite keeps the MVP easy to deploy and inspect without adding infrastructure complexity.
- Twilio sandbox allows fast WhatsApp message testing without a full production messaging setup.
- FastAPI is lightweight and ideal for receiving inbound webhook events.

---

## Design Principles

1. Persistent session over repeated logins
   - The browser should log in once manually in headed mode.
   - Save the session state to disk using Playwright storage_state.
   - On subsequent runs, restore the session instead of forcing fresh authentication.

2. Human review before auto-apply
   - The system should wait for explicit approval before it submits any application.
   - This reduces accidental application spam and keeps the workflow manageable.

3. Dedupe-first behavior
   - Every scraped job ID should be checked against a local SQLite table.
   - Duplicate or already-applied jobs must be filtered before any alert or application attempt.

4. Freshness-based prioritization
   - Newly posted jobs are the highest-signal opportunities.
   - Jobs posted in the last minute should be treated as urgent and routed immediately.
   - Older jobs can be logged for review or batch processing.

5. Fail-safe automation
   - One broken selector or a LinkedIn UI change should not crash the entire cycle.
   - Each stage should be wrapped in error handling and logged.

---

## System Workflow

### 1. Persistent LinkedIn session bootstrap
- Launch Playwright in headed mode.
- Log in to LinkedIn manually.
- Solve any MFA or security challenge as needed.
- Save browser cookies and local storage using storage_state.
- On later runs, restore this state before scraping.

This is essential because repeated login attempts are more likely to trigger account scrutiny than a single persistent session.

### 2. Job discovery and scraping
- Open the LinkedIn jobs search URL with filters such as:
  - keyword(s)
  - location
  - Easy Apply filter
  - recency or date sort
- Parse job cards for:
  - job ID
  - title
  - company name
  - job link
  - posted time indicator
- Use the relative “Posted X minutes/hours ago” text as the freshness signal instead of relying solely on the URL time filter.

### 3. Deduplication and state tracking
Every poll should compare incoming jobs against a SQLite table such as:

- jobs_seen
  - job_id
  - title
  - company
  - url
  - posted_at
  - first_seen_at
  - status

- jobs_applied
  - job_id
  - applied_at
  - result
  - notes

This helps avoid duplicate alerts and applications.

### 4. Alerting via WhatsApp
When a job is new and fresh enough to warrant action:

- send a WhatsApp message with:
  - job title
  - company
  - job link
  - a short prompt such as “Reply YES to apply or NO to skip.”
- use a webhook receiver to capture replies from Twilio
- interpret replies like “yes”, “apply”, or “y” as confirmation
- treat any other response as skip or ignore

### 5. Easy Apply application flow
If the user confirms:

- open the job posting in the browser
- click Easy Apply if present
- walk through the modal step by step
- populate text fields, dropdowns, and radio answers using the candidate profile JSON
- match questions by keyword and map them to known answers
- stop before the final submit step so the user can review or the developer can validate the flow

The key principle is to automate as much as possible without blindly submitting without a human check. This reduces damage from bad selector mappings or unexpected modal layouts.

### 6. Poll loop orchestration
The main scheduling loop should behave like this:

scrape → dedupe → classify → notify → wait for response → apply → log result → mark job state

Pseudo-flow:

1. Poll LinkedIn search feed.
2. Extract job metadata.
3. Compare against SQLite.
4. If job is already seen, skip.
5. If job is new and fresh, generate WhatsApp alert.
6. If no response within timeout, log and skip.
7. If approved, open job and run Easy Apply.
8. Record result in SQLite.
9. Log all exceptions and continue to next poll.

### 7. Deployment and 24/7 operation
- Run the system on a small always-on VPS or home server.
- Use cron or systemd to keep the loop alive.
- Run the FastAPI webhook on the same machine or behind ngrok during development.
- Store logs in a dedicated directory with timestamps.
- Monitor for LinkedIn DOM changes, selector failures, and account throttling patterns.

---

## Candidate Profile Data Model
The profile should live in a JSON file, for example:

```json
{
  "name": "Jane Doe",
  "resume_path": "/path/to/resume.pdf",
  "email": "jane@example.com",
  "phone": "+1-555-123-4567",
  "location": "New York, NY",
  "years_experience": "5",
  "visa_status": "US citizen",
  "salary_expectation": "$120,000",
  "work_authorization": "Authorized to work in the US",
  "professional_summary": "Experienced software engineer focused on backend systems and distributed platforms.",
  "questions": {
    "years_of_experience": "5",
    "willing_to_relocate": "Yes",
    "open_to_remote": "Yes",
    "need_sponsorship": "No"
  }
}
```

This profile should be easy to update without modifying code.

---

## SQLite Schema (MVP)

```sql
CREATE TABLE jobs_seen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    title TEXT,
    company TEXT,
    url TEXT,
    posted_at TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'new'
);

CREATE TABLE jobs_applied (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    title TEXT,
    company TEXT,
    url TEXT,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
    result TEXT,
    notes TEXT
);
```

Optional future tables:

- alerts_sent
- candidate_profile_history
- application_attempts
- scraper_errors

---

## Operational Rules

- Never flag every job as urgent; prioritize freshness and relevance.
- Do not submit applications blindly at the first pass.
- Max out at a controlled, low-volume pace to reduce risk.
- Keep one robust account profile and one clean browser session.
- Pause the loop automatically if the browser session is blocked or the account enters a verification prompt.
- Log all selectors, errors, exceptions, and layout failures for rapid debugging.

---

## MVP Milestones

### Milestone 1: Session + scraper
- launch Playwright in headed mode
- save browser state
- load state on next run
- extract listing metadata from LinkedIn search pages
- store job IDs and metadata in SQLite

### Milestone 2: Freshness + dedupe logic
- filter new jobs only
- classify jobs by freshness
- maintain seen jobs and applied jobs tables
- avoid duplicate processing

### Milestone 3: WhatsApp approval flow
- wire Twilio WhatsApp sandbox
- send alerts with job details
- receive webhook replies
- parse “yes” and “no” responses

### Milestone 4: Easy Apply engine
- open each confirmed job
- handle Easy Apply modal steps
- fill profile values into forms
- validate final review page before submission

### Milestone 5: loop + deployment
- unify all steps into one scheduler loop
- run the service 24/7
- capture logs and errors
- test resilience against UI drift

---

## Risks and Caveats

This project operates in a high-risk area because LinkedIn’s platform rules and security systems are designed to discourage automated application behavior. Even a well-implemented script can encounter:

- selector drift from LinkedIn UI updates
- security challenges or account verification prompts
- rate limiting or temporary blocks
- inconsistent Easy Apply modal layouts across job types

The project should therefore be designed to be conservative, observable, and configurable rather than aggressive.

---

## Success Criteria for the MVP

The MVP is successful if it can reliably do all of the following:

- maintain a persistent browser session
- detect fresh Easy Apply jobs with a consistent scraper
- avoid duplicates across runs
- notify the user quickly through WhatsApp
- wait for approval before applying
- fill common form fields from a stored candidate profile
- log actions in a way that supports debugging
- run without manual intervention for a full workday

---

## Final Recommendation
Start with the smallest version that proves three things:

1. the browser session can be restored reliably
2. fresh jobs can be discovered and tracked correctly
3. a human approval flow works before any application attempt is made

Once those three pieces are stable, the Easy Apply engine can be expanded to handle more job types and edge cases. The real technical challenge is not just automation — it is resilience against LinkedIn’s constantly changing UI and security controls.