"""Referral-contact discovery from legitimate, authorised sources.

* People come ONLY from search-API results (Brave / Google CSE / SerpAPI) whose
  titles+snippets describe public professional profiles. Nothing is invented and
  LinkedIn is never scraped or logged into.
* Employment at the company is judged from that public evidence and recorded;
  "former/ex-" mentions are rejected. The evidence strength is stated honestly.
* Emails come from an authorised verification service (Hunter) or from an address
  that literally appears in public evidence next to the person's name. MX/domain
  checks alone never make an address "verified".
* Exactly three slots are attempted (hr, senior_swe, swe); unfilled slots stay
  unfilled and are reported.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .store import Store
from .util import get_logger, iso, norm_text, utcnow

log = get_logger("contacts")
SLOTS = ("hr", "senior_swe", "swe")
SLOT_LABEL = {"hr": "HR / Recruiter", "senior_swe": "Senior Software Engineer", "swe": "Software Engineer"}

_LINKEDIN_URL = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#\s]+", re.I)
_HR = re.compile(r"\b(recruiter|recruiting|talent acquisition|talent partner|human resources|hr\b|people (?:operations|partner|team)|campus (?:hiring|recruit))", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|lead|principal|sde[- ]?(?:iii|3|ii|2)|sde-?iii)\b.*\b(software|engineer|developer|sde)\b|\bsoftware engineer (?:iii|3)\b", re.I)
_SWE = re.compile(r"\b(software engineer|software developer|backend (?:engineer|developer)|sde[- ]?(?:i|1|ii|2)?|application developer|full[- ]?stack (?:engineer|developer))\b", re.I)
_NOT_SWE = re.compile(r"\b(senior|sr\.?|staff|lead|principal|manager|director|head|vp|architect|intern|recruit|talent|hr)\b", re.I)
_FORMER = re.compile(r"\b(former|formerly|ex-|previously|past)\b", re.I)
_COMPANY_NOISE = re.compile(r"\b(inc|incorporated|ltd|limited|pvt|private|llp|llc|corp|corporation|co|technologies|technology|solutions|software|india|services|group)\b\.?", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SYNTAX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


@dataclass
class Contact:
    slot: str
    name: str
    position: str
    company: str
    profile_url: str
    email: str = ""
    email_status: str = "none"          # verified_service | public_evidence | accept_all_unverified | unverified | none
    email_notes: list[str] = field(default_factory=list)
    affiliation_evidence: list[str] = field(default_factory=list)
    affiliation_strength: str = "search-snippet only (not independently confirmed)"
    source_urls: list[str] = field(default_factory=list)
    reason: str = ""
    found_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- search APIs
class SearchProvider:
    name = "none"

    def search(self, query: str) -> list[dict]:   # -> [{"title","url","snippet"}]
        return []


class BraveSearch(SearchProvider):
    name = "brave"

    def __init__(self, key: str, session=None):
        self.key, self.session = key, session or requests.Session()

    def search(self, query: str) -> list[dict]:
        r = self.session.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": 10},
                             headers={"X-Subscription-Token": self.key, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Brave search HTTP {r.status_code}")
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
                for x in (r.json().get("web", {}).get("results", []))]


class GoogleCSE(SearchProvider):
    name = "google_cse"

    def __init__(self, key: str, cx: str, session=None):
        self.key, self.cx, self.session = key, cx, session or requests.Session()

    def search(self, query: str) -> list[dict]:
        r = self.session.get("https://www.googleapis.com/customsearch/v1",
                             params={"key": self.key, "cx": self.cx, "q": query, "num": 10}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Google CSE HTTP {r.status_code}")
        return [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")}
                for x in r.json().get("items", [])]


class SerpApi(SearchProvider):
    name = "serpapi"

    def __init__(self, key: str, session=None):
        self.key, self.session = key, session or requests.Session()

    def search(self, query: str) -> list[dict]:
        r = self.session.get("https://serpapi.com/search.json", params={"engine": "google", "q": query, "api_key": self.key, "num": 10}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"SerpAPI HTTP {r.status_code}")
        return [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")}
                for x in r.json().get("organic_results", [])]


def make_search_provider(s) -> SearchProvider:
    if s.contact_search_provider == "brave" and s.brave_api_key:
        return BraveSearch(s.brave_api_key)
    if s.contact_search_provider == "google_cse" and s.google_cse_api_key and s.google_cse_cx:
        return GoogleCSE(s.google_cse_api_key, s.google_cse_cx)
    if s.contact_search_provider == "serpapi" and s.serpapi_api_key:
        return SerpApi(s.serpapi_api_key)
    return SearchProvider()


# ------------------------------------------------------------- email services
class HunterClient:
    """Authorised email-finding/verification service (hunter.io). Key sent as a header."""

    def __init__(self, key: str, session=None):
        self.key, self.session = key, session or requests.Session()

    def _get(self, path: str, params: dict) -> dict:
        r = self.session.get(f"https://api.hunter.io/v2/{path}", params=params, headers={"X-API-KEY": self.key}, timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"Hunter {path} HTTP {r.status_code}")
        return r.json().get("data", {})

    def domain_for_company(self, company: str) -> str | None:
        return self._get("domain-search", {"company": company, "limit": 1}).get("domain")

    def find_email(self, domain: str, first: str, last: str) -> dict:
        return self._get("email-finder", {"domain": domain, "first_name": first, "last_name": last})

    def verify(self, email: str) -> dict:
        return self._get("email-verifier", {"email": email})


# ------------------------------------------------------------------ parsing
def company_tokens(company: str) -> list[str]:
    c = _COMPANY_NOISE.sub(" ", norm_text(company))
    return [t for t in re.findall(r"[a-z0-9]+", c) if len(t) > 1] or re.findall(r"[a-z0-9]+", norm_text(company))


def mentions_company(text: str, company: str) -> bool:
    toks = company_tokens(company)
    t = norm_text(text)
    return bool(toks) and all(re.search(r"\b" + re.escape(x) + r"\b", t) for x in toks)


def parse_linkedin_result(res: dict) -> tuple[str, str] | None:
    """('Name', 'Headline') from a result titled 'Name - Headline | LinkedIn'. None if not a profile."""
    if not _LINKEDIN_URL.match(res.get("url", "")):
        return None
    title = re.sub(r"\s*[|\u2013-]\s*LinkedIn.*$", "", res.get("title", "")).strip()
    parts = [p.strip() for p in re.split(r"\s+[-\u2013\u2014]\s+", title) if p.strip()]
    if len(parts) < 2:
        return None
    name = parts[0]
    if not re.match(r"^[A-Za-z][A-Za-z.'\u2019 -]{1,60}$", name) or len(name.split()) < 2:
        return None
    return name, " - ".join(parts[1:])


def classify_role(headline: str) -> str | None:
    if _HR.search(headline):
        return "hr"
    if _SENIOR.search(headline):
        return "senior_swe"
    if _SWE.search(headline) and not _NOT_SWE.search(headline):
        return "swe"
    return None


def evaluate_candidate(res: dict, company: str, wanted_slot: str) -> Contact | None:
    parsed = parse_linkedin_result(res)
    if not parsed:
        return None
    name, headline = parsed
    blob = f"{res.get('title', '')} {res.get('snippet', '')}"
    if classify_role(headline) != wanted_slot:
        return None
    if not mentions_company(blob, company):
        return None
    # reject people whose evidence says they LEFT the company
    window_hit = False
    for m in _FORMER.finditer(blob):
        around = blob[max(0, m.start() - 60): m.end() + 80]
        if mentions_company(around, company):
            window_hit = True
    if window_hit:
        return None
    return Contact(slot=wanted_slot, name=name, position=headline.split(" - ")[0][:120], company=company,
                   profile_url=res["url"].split("?")[0],
                   affiliation_evidence=[clean(res.get("title", "")), clean(res.get("snippet", ""))],
                   source_urls=[res["url"]],
                   reason=f"public profile headline matches '{SLOT_LABEL[wanted_slot]}' and names {company}")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:300]


def email_syntax_ok(email: str) -> bool:
    return bool(_SYNTAX.match(email or "")) and len(email) <= 254


def domain_consistent(email: str, company_domain: str) -> bool:
    d = email.rsplit("@", 1)[-1].lower()
    c = company_domain.lower()
    return d == c or d.endswith("." + c)


def mx_exists(domain: str) -> bool | None:
    """True/False if dnspython is installed, else None (unchecked). Never proves a mailbox exists."""
    try:
        import dns.resolver  # type: ignore

        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ service
class ContactService:
    def __init__(self, settings, store: Store, search: SearchProvider | None = None, hunter: HunterClient | None = None,
                 now=utcnow, base_dir: str | Path | None = None):
        self.s, self.store, self.now = settings, store, now
        self.search = search if search is not None else make_search_provider(settings)
        self.hunter = hunter if hunter is not None else (HunterClient(settings.hunter_api_key) if settings.hunter_api_key else None)
        p = settings.path(settings.company_domains_file, base_dir)
        self.domain_overrides = {k.lower(): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}

    def _queries(self, company: str, slot: str) -> list[str]:
        loc = f' "{self.s.contact_location_hint}"' if self.s.contact_location_hint else ""
        q = {"hr": f'site:linkedin.com/in "{company}" (recruiter OR "talent acquisition" OR "human resources"){loc}',
             "senior_swe": f'site:linkedin.com/in "{company}" "Senior Software Engineer"{loc}',
             "swe": f'site:linkedin.com/in "{company}" "Software Engineer" -senior -lead -staff -manager{loc}'}
        return [q[slot]]

    def find(self, job_id: str, company: str) -> tuple[dict[str, Contact], list[str]]:
        """Return ({slot: Contact}, problems). Missing slots are simply absent."""
        problems: list[str] = []
        found: dict[str, Contact] = {}
        if isinstance(self.search, SearchProvider) and self.search.name == "none":
            return {}, ["no contact search provider configured (set CONTACT_SEARCH_PROVIDER and its API key)"]
        used: set[str] = set()
        for slot in SLOTS:
            for q in self._queries(company, slot):
                if not self.store.quota_try_consume("search", self.s.search_daily_cap, self.now()):
                    problems.append("daily search quota reached; remaining slots left unfilled")
                    return self._enrich(found, company, problems), problems
                try:
                    results = self.search.search(q)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"search failed for {slot}: {type(exc).__name__}: {str(exc)[:100]}")
                    continue
                for res in results:
                    c = evaluate_candidate(res, company, slot)
                    if c and c.profile_url not in used:
                        used.add(c.profile_url)
                        found[slot] = c
                        break
            if slot not in found:
                problems.append(f"no verified {SLOT_LABEL[slot]} found for {company}")
        return self._enrich(found, company, problems), problems

    # -- email -------------------------------------------------------------
    def company_domain(self, company: str, problems: list[str]) -> str | None:
        key = company.lower()
        if key in self.domain_overrides:
            return self.domain_overrides[key]
        cached = self.store.kv_get(f"domain:{key}")
        if cached:
            return cached
        if self.hunter and self.store.quota_try_consume("hunter", self.s.hunter_daily_cap, self.now()):
            try:
                d = self.hunter.domain_for_company(company)
                if d:
                    self.store.kv_set(f"domain:{key}", d)
                    return d
            except Exception as exc:  # noqa: BLE001
                problems.append(f"domain lookup failed: {type(exc).__name__}")
        return None

    def _enrich(self, found: dict[str, Contact], company: str, problems: list[str]) -> dict[str, Contact]:
        if not found:
            return found
        domain = self.company_domain(company, problems)
        for c in found.values():
            c.found_at = iso(self.now())
            self._resolve_email(c, domain, problems)
        return found

    def _resolve_email(self, c: Contact, domain: str | None, problems: list[str]) -> None:
        blob = " ".join(c.affiliation_evidence)
        public = [m for m in _EMAIL.findall(blob) if domain and domain_consistent(m, domain)]
        if public and email_syntax_ok(public[0]):
            c.email, c.email_status = public[0], "public_evidence"
            c.email_notes.append("address appears in public evidence next to the person's name")
            return
        if not domain:
            c.email_notes.append("company domain unknown: no email lookup attempted")
            return
        if not self.hunter:
            c.email_notes.append("no email-verification service configured (HUNTER_API_KEY): address not looked up")
            return
        first, _, last = c.name.partition(" ")
        last = c.name.split()[-1]
        if not self.store.quota_try_consume("hunter", self.s.hunter_daily_cap, self.now()):
            c.email_notes.append("daily email-lookup quota reached")
            return
        try:
            data = self.hunter.find_email(domain, first, last)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"email lookup failed for {c.name}: {type(exc).__name__}")
            c.email_notes.append("email lookup failed")
            return
        email = (data.get("email") or "").strip()
        if not email or not email_syntax_ok(email) or not domain_consistent(email, domain):
            c.email_notes.append("service returned no usable work address")
            return
        c.email = email
        status = ((data.get("verification") or {}).get("status") or "").lower()
        if status not in {"valid", "accept_all", "invalid", "unknown"} and self.store.quota_try_consume("hunter", self.s.hunter_daily_cap, self.now()):
            try:
                status = (self.hunter.verify(email).get("status") or "").lower()
            except Exception:  # noqa: BLE001
                status = ""
        mx = mx_exists(domain)
        c.email_notes.append(f"service verification: {status or 'not reported'}; MX check: {'ok' if mx else 'unchecked' if mx is None else 'failed'}")
        if status == "valid":
            c.email_status = "verified_service"
        elif status == "accept_all":
            c.email_status = "accept_all_unverified"
        elif status == "invalid":
            c.email, c.email_status = "", "none"
            c.email_notes.append("service says the address is invalid: discarded")
        else:
            c.email_status = "unverified"
