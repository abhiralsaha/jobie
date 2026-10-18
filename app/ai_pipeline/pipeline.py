"""End-to-end orchestration with persistent, idempotent stages.

discovered -> (title prefilter) -> description -> classify -> alerts -> package
package = resume -> resume PDFs (3 allow-listed chats) -> contacts -> drafts -> approval requests

* ``submit()`` returns immediately; work runs on a small thread pool so the watcher
  loop is never blocked by a slow AI call or LaTeX compile.
* Every external side-effect is claimed in the DB first, so retries/restarts only do
  what is still missing. Failures are recorded with the exact error and retried with
  exponential backoff up to ``max_job_attempts``; they never cause an unverified job
  to be alerted.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from .classifier import Classification, ClassificationUnavailable
from .destinations import AllowlistError
from .jd_fetch import FetchError, FetchRateLimited
from .models import Job, desc_hash
from .resume import ResumeError, ResumeUnavailable, Profile
from .util import get_logger, iso, utcnow

log = get_logger("pipeline")
RETRYABLE = ("discovered", "alerts_pending", "package_pending")


def backoff_seconds(attempts: int) -> int:
    return int(min(900, 30 * (2 ** max(0, attempts - 1))))


class Pipeline:
    def __init__(self, settings, store, classifier, fetcher, deliverer, resume_svc, contact_svc, referral_svc,
                 approvals, now=utcnow, base_dir=None, workers: int | None = None):
        self.s, self.store, self.classifier, self.fetcher = settings, store, classifier, fetcher
        self.deliverer, self.resume_svc, self.contact_svc = deliverer, resume_svc, contact_svc
        self.referral_svc, self.approvals, self.now, self.base_dir = referral_svc, approvals, now, base_dir
        self._pool = ThreadPoolExecutor(max_workers=workers or settings.worker_threads, thread_name_prefix="jobie-ai")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ intake
    def submit(self, job: Job) -> bool:
        """Register a discovered job and process it in the background. False if already known."""
        desc = (job.description or "").strip() or None
        from .jd_fetch import is_complete

        new = self.store.insert_job_if_new({
            "job_id": job.job_id, "title": job.title, "company": job.company, "location": job.location, "url": job.url,
            "posted_at": job.posted_at, "description": desc, "desc_hash": desc_hash(desc) if desc else None,
            "desc_complete": bool(desc and is_complete(desc, self.s.min_desc_chars)), "source": job.source}, self.now())
        if not new:
            return False
        self._enqueue(job.job_id)
        return True

    def _enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._inflight:
                return
            self._inflight.add(job_id)
        self._pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        try:
            self.process(job_id)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error processing job %s", job_id)
            self._fail_attempt(job_id, "unhandled internal error (see logs)")
        finally:
            with self._lock:
                self._inflight.discard(job_id)

    def wait_idle(self, timeout: float = 60.0) -> None:
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._inflight:
                    return
            time.sleep(0.02)

    # ------------------------------------------------------------ retry / recovery
    def tick(self) -> int:
        """Called by the scheduler: re-queue jobs whose backoff has elapsed; expire old approvals."""
        n = 0
        for r in self.store.jobs_due(self.now(), RETRYABLE, self.s.max_job_attempts):
            self._enqueue(r["job_id"])
            n += 1
        self.approvals.expire_stale()
        return n

    def recover(self) -> int:
        self.approvals.recover_after_restart()
        rows = self.store.jobs_in_stages(RETRYABLE)
        for r in rows:
            self._enqueue(r["job_id"])
        return len(rows)

    def _fail_attempt(self, job_id: str, error: str, delay: float | None = None) -> None:
        row = self.store.get_job(job_id)
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= self.s.max_job_attempts:
            self.store.update_job(job_id, self.now(), attempts=attempts, stage="failed", last_error=error[:500], next_attempt_at=None)
            log.error("job=%s FAILED permanently after %d attempts: %s", job_id, attempts, error)
            self.approvals.notify_control(f"\u274C Job <code>{job_id}</code> failed after {attempts} attempts: {error[:300]}")
            return
        wait = max(delay or 0, backoff_seconds(attempts))
        self.store.update_job(job_id, self.now(), attempts=attempts, last_error=error[:500],
                              next_attempt_at=iso(self.now() + timedelta(seconds=wait)))
        log.warning("job=%s attempt %d failed: %s (retry in %ds)", job_id, attempts, error, wait)

    # ------------------------------------------------------------ the work
    def process(self, job_id: str) -> str:
        """Run every stage that is still outstanding for the job. Returns the resulting stage."""
        row = self.store.get_job(job_id)
        if not row or row["stage"] in ("excluded", "review", "done", "failed"):
            return row["stage"] if row else "unknown"
        if row["stage"] in ("discovered",):
            self._stage_classify(job_id)
            row = self.store.get_job(job_id)
        if row["stage"] == "alerts_pending":
            self._stage_alerts(job_id)
            row = self.store.get_job(job_id)
        if row["stage"] == "package_pending":
            self._stage_package(job_id)
            row = self.store.get_job(job_id)
        return row["stage"]

    def _stage_classify(self, job_id: str) -> None:
        row = self.store.get_job(job_id)
        job = Job.from_row(row)
        desc, complete = row.get("description"), bool(row.get("desc_complete"))
        if not complete:
            log.info("fetching full job description for 0-%g year AI screening job=%s",
                     self.s.max_required_max_years, job_id)
            try:
                desc, complete = self.fetcher.fetch(job_id)
            except FetchRateLimited as rl:
                self._fail_attempt(job_id, f"description fetch rate-limited ({rl})", delay=rl.retry_after)
                return
            except FetchError as exc:
                self._fail_attempt(job_id, f"description fetch failed: {exc}")
                return
            self.store.update_job(job_id, self.now(), description=desc, desc_hash=desc_hash(desc), desc_complete=1 if complete else 0)
            if not complete:
                log.warning("job=%s full description unavailable or truncated; AI screening deferred", job_id)
                attempts = self.store.get_job(job_id)["attempts"] + 1
                if attempts >= self.s.max_job_attempts:           # out of attempts: unverified, never alertable
                    self.classifier.classify(job, desc, False)    # records the 'not verified' audit row
                    self.store.update_job(job_id, self.now(), stage="review", attempts=attempts, next_attempt_at=None,
                                          last_error="full description not retrievable (empty/truncated)")
                    log.warning("job=%s moved to review: full description never retrievable", job_id)
                else:
                    self._fail_attempt(job_id, "full description not retrievable (empty/truncated)")
                return
        try:
            c = self.classifier.classify(job, desc, complete)
        except ClassificationUnavailable as exc:
            self._fail_attempt(job_id, f"AI classification unavailable: {exc}")
            return
        stage = {"include": "alerts_pending", "exclude": "excluded"}.get(c.decision, "review")
        self.store.update_job(job_id, self.now(), stage=stage, last_error=None)

    def _classification(self, job_id: str) -> Classification:
        r = self.store.latest_classification(job_id)
        import json

        return Classification.from_dict(json.loads(r["final_json"]))

    def _stage_alerts(self, job_id: str) -> None:
        job = Job.from_row(self.store.get_job(job_id))
        c = self._classification(job_id)
        if not c.included:                                        # defensive: never alert otherwise
            self.store.update_job(job_id, self.now(), stage="review")
            return
        rep = self.deliverer.send_alerts(job, c)
        if rep.failed:
            self._fail_attempt(job_id, "alert delivery failed for: " + "; ".join(f"{k}: {v}" for k, v in rep.failed.items()))
            return
        self.store.update_job(job_id, self.now(), stage="package_pending", attempts=0, last_error=None, next_attempt_at=None)

    def _stage_package(self, job_id: str) -> None:
        row = self.store.get_job(job_id)
        job = Job.from_row(row)
        c = self._classification(job_id)
        errors: list[str] = []
        # 1. resume
        try:
            res = self.resume_svc.build(job, row["description"])
        except (ResumeUnavailable, ResumeError) as exc:
            self._fail_attempt(job_id, f"resume: {exc}")
            return
        if not res.ok:
            self._fail_attempt(job_id, f"resume failed validation: {res.error}")
            return
        resume = {"status": "ok", "pdf_path": res.pdf_path, "pdf_sha256": res.sha256, "sha256": res.sha256, "filename": res.filename}
        # 2. resume PDFs -> the three allow-listed destinations only
        try:
            rep = self.deliverer.send_resume(job, c, resume)
            if rep.failed:
                errors.append("resume delivery failed for: " + "; ".join(f"{k}: {v}" for k, v in rep.failed.items()))
        except AllowlistError as exc:
            errors.append(f"resume PDFs NOT sent (fail-closed): {exc}")
        # 3. contacts (searched once per job; daily cap protects API quotas)
        contacts, problems = self._contacts(job)
        # 4. review package + approval requests
        try:
            profile = Profile.load(self.s.path(self.s.resume_profile_file, self.base_dir))
            self.approvals.send_review_summary(job, c, resume, contacts, problems + list(res.notes[:3]))
            for slot, contact in contacts.items():
                draft = self.referral_svc.draft_for(job, contact, profile, resume)
                self.approvals.request(draft, job, contact)
        except Exception as exc:  # noqa: BLE001 - recorded, retried; other steps already done stay done
            errors.append(f"referral preparation: {type(exc).__name__}: {exc}")
        if errors:
            self._fail_attempt(job_id, " | ".join(errors))
            return
        self.store.update_job(job_id, self.now(), stage="done", last_error=None, next_attempt_at=None)
        log.info("job=%s DONE (resume %s, %d contact(s))", job_id, res.filename, len(contacts))

    def _contacts(self, job: Job):
        from .contacts import Contact

        existing = self.store.get_contacts(job.job_id)
        if existing or self.store.kv_get(f"contacts_done:{job.job_id}"):
            return {k: Contact(**v) for k, v in existing.items()}, []
        if not self.store.quota_try_consume("referral_jobs", self.s.referral_daily_job_cap, self.now()):
            self.store.kv_set(f"contacts_done:{job.job_id}", "skipped_daily_cap")
            return {}, ["daily referral-job cap reached; contact search skipped for this job"]
        contacts, problems = self.contact_svc.find(job.job_id, job.company)
        for slot, c in contacts.items():
            self.store.save_contact(job.job_id, slot, c.to_dict(), self.now())
        self.store.kv_set(f"contacts_done:{job.job_id}", "ok")
        return contacts, problems
