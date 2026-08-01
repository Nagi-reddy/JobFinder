#!/usr/bin/env python3
"""
fetch_jobs.py - free job aggregator over public ATS JSON APIs
(Greenhouse, Lever, Ashby, SmartRecruiters, Workday)

No API keys, no paid services. Reads companies.json, hits each
company's public postings endpoint, normalizes everything into one
schema, filters to US-based postings, and writes jobs.json for the
website to read.

Run it:
    pip install requests
    python fetch_jobs.py

Run it every 20 minutes automatically: see README.md (Windows Task
Scheduler / cron / GitHub Actions examples).

Every fetch_* function is defensive: a bad slug, a 404, a network
hiccup, a crash while parsing one weird posting, or an unexpected
response shape just gets logged and skipped rather than killing the
whole run.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
COMPANIES_FILE = HERE / "companies.json"
OUTPUT_FILE = HERE / "jobs.json"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (personal-job-tracker/1.0)"}

# Only keep postings newer than this many days (None = keep everything)
MAX_AGE_DAYS = 21

# ---------------------------------------------------------------------------
# US-location filtering. There's no reliable single field across every ATS,
# so this is a best-effort heuristic: explicit non-US markers exclude a
# posting; explicit US markers (state names/abbreviations, "United States",
# a plain "Remote" with no country attached) include it. Ambiguous/empty
# location text defaults to included, since every company in companies.json
# is itself US-based - adjust DEFAULT_WHEN_UNKNOWN below if that's too loose
# for your list.
# ---------------------------------------------------------------------------

DEFAULT_WHEN_UNKNOWN = True

US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming",
}

NON_US_MARKERS = [
    "united kingdom", "canada", "india", "germany", "france", "spain", "italy",
    "netherlands", "poland", "ireland", "australia", "singapore", "japan", "china",
    "brazil", "mexico", "emea", "apac", "latam", "uk)", " uk ", "toronto", "vancouver",
    "montreal", "london", "manchester", "bangalore", "bengaluru", "hyderabad", "mumbai",
    "pune", "delhi", "berlin", "munich", "hamburg", "paris", "dublin", "cork",
    "sydney", "melbourne", "tel aviv", "israel", "philippines", "romania", "portugal",
    "sweden", "switzerland", "austria", "belgium", "denmark", "finland", "norway",
    "new zealand", "south africa", "hong kong", "taiwan", "korea", "vietnam", "indonesia",
    "malaysia", "thailand", "argentina", "chile", "colombia", "costa rica", "peru",
    "united arab emirates", "dubai", "saudi arabia", "egypt", "nigeria", "kenya",
    "scotland", "wales", "england", "amsterdam", "barcelona", "madrid", "milan", "rome",
    "warsaw", "prague", "vienna", "zurich", "stockholm", "copenhagen", "oslo", "helsinki",
]

US_MARKERS = ["united states", "usa", "u.s.", "u.s.a"]


def looks_us(location_text, country_code=None):
    """Best-effort US-location check. See module docstring above."""
    if country_code:
        cc = country_code.strip().lower()
        return cc in ("us", "usa", "united states")

    text = (location_text or "").strip().lower()
    if not text:
        return DEFAULT_WHEN_UNKNOWN

    for marker in NON_US_MARKERS:
        if marker in text:
            return False

    if any(marker in text for marker in US_MARKERS):
        return True

    for abbr in US_STATE_ABBR:
        if f", {abbr}" in text.upper() or text.upper().strip().endswith(abbr):
            return True

    for name in US_STATE_NAMES:
        if name in text:
            return True

    if "remote" in text:
        return True  # no country attached and nothing non-US matched either

    return DEFAULT_WHEN_UNKNOWN


# ---------------------------------------------------------------------------
# Experience-level classification. No ATS exposes "years of experience"
# as a field, so this is a title-text heuristic, same spirit as looks_us()
# above: best-effort, not exact. Buckets:
#   Entry (0-2 yrs)  - "Junior", "Jr", "Entry", "Associate", "Intern",
#                      "New Grad", or a bare title ending in roman numeral I
#   Mid (2-4 yrs)    - a bare title ending in roman numeral II, or no
#                      level signal at all in the title (the common case -
#                      most postings just say "Software Engineer")
#   Senior (4-7 yrs) - "Senior", "Sr", or roman numeral III
#   Staff+ (7+ yrs)  - "Staff", "Principal", "Distinguished", "Fellow",
#                      "Director", "VP", "Head of", "Chief"
# Checked in that order (staff first) so "Senior Staff Engineer" lands in
# Staff+, not Senior. If a title doesn't clearly say anything, it defaults
# to Mid rather than "Not Specified" - untitled/generic postings are far
# more often mid-level than entry or staff, so this keeps the entry and
# staff buckets meaningfully precise instead of catching everything.
# ---------------------------------------------------------------------------

STAFF_RE = re.compile(r"\b(staff|principal|distinguished|fellow|director|vp|vice president|head of|chief)\b", re.I)
SENIOR_RE = re.compile(r"\b(senior|sr)\b", re.I)
SENIOR_NUMERAL_RE = re.compile(r"\biii\b", re.I)
ENTRY_KEYWORDS_RE = re.compile(r"\b(junior|jr|entry[\s-]?level|associate|intern(ship)?|new grad(uate)?)\b", re.I)
ENTRY_NUMERAL_RE = re.compile(r"\bI\b")  # capital-only: avoids matching lowercase "i" inside other words
MID_NUMERAL_RE = re.compile(r"\bII\b")

def classify_experience(title):
    t = title or ""
    if STAFF_RE.search(t):
        return "Staff+ (7+ yrs)"
    if SENIOR_RE.search(t) or SENIOR_NUMERAL_RE.search(t):
        return "Senior (4-7 yrs)"
    if ENTRY_KEYWORDS_RE.search(t) or ENTRY_NUMERAL_RE.search(t):
        return "Entry (0-2 yrs)"
    if MID_NUMERAL_RE.search(t):
        return "Mid (2-4 yrs)"
    return "Mid (2-4 yrs)"  # no signal in the title - treat as the default/unlabeled bucket


def log(msg):
    print(msg, flush=True)


def safe_get(url, **kwargs):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.json(), None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Per-ATS fetchers. Each returns (jobs, error, filtered_count). jobs is a
# list of dicts: {title, company, category, posted_date (full ISO string),
# apply_link, source, location}. posted_date is kept as a full timestamp
# (not just the date) wherever the ATS provides one, so same-day postings
# still sort by actual recency instead of falling back to companies.json's
# list order. experience_level gets added later, once per job, in main()
# via classify_experience() - no need to set it per-fetcher.
# ---------------------------------------------------------------------------

def fetch_greenhouse(company):
    slug = company["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    data, err = safe_get(url)
    if err:
        return [], err, 0
    jobs = data.get("jobs", [])
    out = []
    filtered = 0
    for j in jobs:
        loc = (j.get("location") or {}).get("name", "")
        if not looks_us(loc):
            filtered += 1
            continue
        out.append({
            "title": j.get("title", ""),
            "company": company["name"],
            "category": company["category"],
            "posted_date": j.get("updated_at") or j.get("created_at") or "",
            "apply_link": j.get("absolute_url", ""),
            "source": "Greenhouse",
            "location": loc,
        })
    return out, None, filtered


def fetch_lever(company):
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data, err = safe_get(url)
    if err:
        return [], err, 0
    if not isinstance(data, list):
        return [], "unexpected response shape", 0
    out = []
    filtered = 0
    for j in data:
        loc = (j.get("categories") or {}).get("location", "")
        if not looks_us(loc):
            filtered += 1
            continue
        ts = j.get("createdAt")
        posted = ""
        if ts:
            try:
                posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            except Exception:
                posted = ""
        out.append({
            "title": j.get("text", ""),
            "company": company["name"],
            "category": company["category"],
            "posted_date": posted,
            "apply_link": j.get("hostedUrl", ""),
            "source": "Lever",
            "location": loc,
        })
    return out, None, filtered


def fetch_ashby(company):
    slug = company["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data, err = safe_get(url)
    if err:
        return [], err, 0
    jobs = data.get("jobs", [])
    out = []
    filtered = 0
    for j in jobs:
        loc = j.get("location", "") or ""
        if not loc and j.get("isRemote"):
            loc = "Remote"
        if not looks_us(loc):
            filtered += 1
            continue
        out.append({
            "title": j.get("title", ""),
            "company": company["name"],
            "category": company["category"],
            "posted_date": j.get("publishedAt") or "",
            "apply_link": j.get("jobUrl") or j.get("applyUrl", ""),
            "source": "Ashby",
            "location": loc,
        })
    return out, None, filtered


def fetch_smartrecruiters(company):
    slug = company["slug"]
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    data, err = safe_get(url)
    if err:
        return [], err, 0
    jobs = data.get("content", [])
    out = []
    filtered = 0
    for j in jobs:
        loc_obj = j.get("location") or {}
        country = loc_obj.get("country") if isinstance(loc_obj, dict) else None
        loc_text = ", ".join(filter(None, [
            loc_obj.get("city") if isinstance(loc_obj, dict) else None,
            loc_obj.get("region") if isinstance(loc_obj, dict) else None,
            country,
        ]))
        if not looks_us(loc_text, country_code=country):
            filtered += 1
            continue

        apply_link = j.get("applyUrl", "")
        if not apply_link:
            ref = j.get("ref")
            if isinstance(ref, dict):
                apply_link = ref.get("jobAd", "")

        out.append({
            "title": j.get("name", ""),
            "company": company["name"],
            "category": company["category"],
            "posted_date": j.get("releasedDate") or "",
            "apply_link": apply_link,
            "source": "SmartRecruiters",
            "location": loc_text,
        })
    return out, None, filtered


def fetch_workday(company):
    # slug format: "<tenant-host>/<tenant>/<site>"
    # e.g. "salesforce.wd1.myworkdayjobs.com/External_Career_Site"
    parts = company["slug"].split("/", 1)
    if len(parts) != 2:
        return [], "slug must be '<host>/<tenant>/<site>'", 0
    host, tenant_site = parts
    tenant = host.split(".")[0]
    url = f"https://{host}/wday/cxs/{tenant}/{tenant_site}/jobs"
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code} (Workday often needs the exact tenant/site - check DevTools)", 0
        data = r.json()
    except Exception as e:
        return [], str(e), 0

    postings = data.get("jobPostings", [])
    out = []
    filtered = 0
    for j in postings:
        loc = j.get("locationsText", "") or ""
        if not looks_us(loc):
            filtered += 1
            continue
        out.append({
            "title": j.get("title", ""),
            "company": company["name"],
            "category": company["category"],
            # Workday's list endpoint usually only gives relative text like
            # "Posted 3 Days Ago" here, not a real timestamp, so this won't
            # sort precisely against the other sources' ISO timestamps.
            "posted_date": j.get("postedOn", ""),
            "apply_link": f"https://{host}{j.get('externalPath', '')}",
            "source": "Workday",
            "location": loc,
        })
    return out, None, filtered


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def within_max_age(date_str):
    if not MAX_AGE_DAYS or not date_str:
        return True
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days <= MAX_AGE_DAYS
    except Exception:
        return True  # keep it if we can't parse the date - better than silently dropping


def main():
    if not COMPANIES_FILE.exists():
        log(f"ERROR: {COMPANIES_FILE} not found")
        sys.exit(1)

    config = json.loads(COMPANIES_FILE.read_text())
    companies = config["companies"]

    all_jobs = []
    failures = []
    empties = []
    total_filtered_non_us = 0

    for company in companies:
        ats = company["ats"]
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            failures.append((company["name"], f"no fetcher for ats={ats}"))
            continue

        try:
            jobs, err, filtered_out = fetcher(company)
        except Exception as e:
            failures.append((company["name"], f"crashed: {e}"))
            continue

        total_filtered_non_us += filtered_out

        if err:
            failures.append((company["name"], err))
            continue
        if not jobs:
            if filtered_out:
                empties.append(f"{company['name']} (all {filtered_out} postings were non-US)")
            else:
                empties.append(company["name"])
            continue

        all_jobs.extend(jobs)
        log(f"  {company['name']:40s} ({ats:16s}) -> {len(jobs)} US jobs"
            + (f" ({filtered_out} non-US skipped)" if filtered_out else ""))
        time.sleep(0.3)  # be polite - avoid hammering these free public endpoints

    # Filter by age, dedupe by apply_link, tag with a heuristic experience level
    seen = set()
    deduped = []
    for j in all_jobs:
        if not within_max_age(j["posted_date"]):
            continue
        key = j["apply_link"] or (j["title"] + j["company"])
        if key in seen:
            continue
        seen.add(key)
        j["experience_level"] = classify_experience(j["title"])
        deduped.append(j)

    # Sort by full posted_date timestamp, newest first. ISO-format strings
    # sort correctly as plain text, so this reflects true recency (down to
    # the minute, where the source ATS provides it) instead of grouping by
    # whatever order companies.json happens to list companies in.
    deduped.sort(key=lambda j: j["posted_date"], reverse=True)

    OUTPUT_FILE.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_count": len(deduped),
        "jobs": deduped,
    }, indent=2))

    log("")
    log(f"Wrote {len(deduped)} US jobs -> {OUTPUT_FILE}")
    log(f"Filtered out {total_filtered_non_us} non-US postings")
    if empties:
        log(f"\n{len(empties)} companies returned zero (US) jobs (may be a wrong/stale slug):")
        for name in empties:
            log(f"  - {name}")
    if failures:
        log(f"\n{len(failures)} companies failed (fix or remove from companies.json):")
        for name, err in failures:
            log(f"  - {name}: {err}")


if __name__ == "__main__":
    main()