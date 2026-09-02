"""Deterministic hard filters. No AI here. A job must pass every check to reach
the AI step; a failure here can never be overridden later.

All thresholds come from Config (hard_filters.*). Every check returns a
RuleResult so the run log can show exactly why a job was dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .models import JobPosting

_ACTIVE = {"active", "open", "live", "published", "hiring"}
_CLOSED = {"closed", "expired", "filled", "gone", "stale", "inactive", "archived"}


@dataclass
class RuleResult:
    rule: str
    passed: bool
    detail: str = ""


@dataclass
class FilterOutcome:
    job: JobPosting
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def first_fail(self) -> str | None:
        for r in self.results:
            if not r.passed:
                return f"{r.rule}: {r.detail}"
        return None


# --------------------------------------------------------------------------- #
# Years-of-experience parsing
# --------------------------------------------------------------------------- #
_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*years?", re.IGNORECASE)
_OPEN = re.compile(
    r"(?:(?:minimum|min\.?|at\s*least|atleast)\s*(?:of\s*)?)?(\d{1,2})\s*\+\s*years?"
    r"|(?:minimum|min\.?|at\s*least|atleast)\s*(?:of\s*)?(\d{1,2})\s*years?",
    re.IGNORECASE,
)
_EXACT = re.compile(
    r"(\d{1,2})\s*years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|work\s+|industry\s+|hands-on\s+)?experience",
    re.IGNORECASE,
)


def parse_years_requirement(text: str) -> tuple[float, float] | None:
    """Return (lo, hi) where hi may be math.inf, or None if no requirement stated.
    First match wins (range > open-ended > exact 'N years experience').
    Values above 30 are treated as a misparse and ignored."""
    if not text:
        return None
    m = _RANGE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        lo, hi = min(lo, hi), max(lo, hi)
        return (lo, hi) if lo <= 30 else None
    m = _OPEN.search(text)
    if m:
        n = int(m.group(1) or m.group(2))
        return (n, math.inf) if n <= 30 else None
    m = _EXACT.search(text)
    if m:
        n = int(m.group(1))
        return (n, n) if n <= 30 else None
    return None


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def check_recency(job: JobPosting, cfg: Config, today: date) -> RuleResult:
    rc = cfg.hard_filters.recency
    status = (job.status or "").strip().lower()

    if rc.prefer_status_over_date and status:
        if status in _CLOSED:
            return RuleResult("recency", False, f"status is '{status}'")
        if status in _ACTIVE:
            return RuleResult("recency", True, f"status '{status}'")

    if job.date_posted:
        age = (today - job.date_posted).days
        if age > rc.days:
            return RuleResult("recency", False, f"posted {age}d ago (> {rc.days})")
        return RuleResult("recency", True, f"posted {age}d ago")

    if status in _ACTIVE:
        return RuleResult("recency", True, f"status '{status}', no date")
    return RuleResult("recency", True, "no status or date - kept (unverifiable)")


def check_seniority(job: JobPosting, cfg: Config, my_years: int) -> RuleResult:
    sc = cfg.hard_filters.seniority
    win_lo, win_hi = sc.accept_window(my_years)
    req = parse_years_requirement(f"{job.title}\n{job.description}")

    if req is None:
        raw = (job.seniority_raw or "").strip().lower()
        if raw in {"entry", "entry_level", "junior", "intern", "internship", "associate"}:
            return RuleResult("seniority", False, f"source seniority '{raw}', no years stated")
        if raw in {"mid", "mid_level", "midlevel"}:
            return RuleResult("seniority", False, f"source seniority '{raw}', no years stated")
        if sc.no_requirement_action == "reject":
            return RuleResult("seniority", False, "no years requirement stated")
        return RuleResult("seniority", True, f"no years stated (raw='{raw or 'n/a'}') - AI to judge")

    lo, hi = req
    hi_txt = "inf" if hi == math.inf else str(int(hi))
    label = f"JD wants {int(lo)}-{hi_txt}y vs window {win_lo}-{win_hi}"
    if lo > win_hi:
        return RuleResult("seniority", False, f"too senior: {label}")
    if hi <= win_lo:   # a bounded range topping out at/under the floor = aimed below me
        return RuleResult("seniority", False, f"too junior: {label}")
    return RuleResult("seniority", True, label)


_RUPEE_HINT = re.compile(r"(₹|\bINR\b|\brs\.?\b|\blpa\b|lakh|crore|per annum)", re.IGNORECASE)
_NUM = re.compile(r"(\d[\d,]*\.?\d*)\s*(k|l|lakh|lac|cr|crore|m|mn|million)?", re.IGNORECASE)
_PERIOD = re.compile(r"(per\s*year|/\s*yr|/\s*year|p\.?a\.?|annum|per\s*month|/\s*mo|month|per\s*week|week|per\s*hour|hour|/\s*hr)", re.IGNORECASE)
_MULT = {"year": 1, "annum": 1, "pa": 1, "yr": 1, "month": 12, "mo": 12, "week": 52, "hour": 2080, "hr": 2080}


def _annualise_raw(text: str) -> tuple[float, str] | None:
    """Parse 'salary_string' into (annual_amount, currency_guess). Needs a period word."""
    pm = _PERIOD.search(text)
    if not pm:
        return None
    period = pm.group(1).lower()
    mult = 1
    for key, factor in _MULT.items():
        if key in period:
            mult = factor
            break
    nums: list[float] = []
    for m in _NUM.finditer(text):
        val = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if unit in ("k",):
            val *= 1_000
        elif unit in ("l", "lakh", "lac"):
            val *= 100_000
        elif unit in ("cr", "crore"):
            val *= 10_000_000
        elif unit in ("m", "mn", "million"):
            val *= 1_000_000
        if val >= 1000 or unit:
            nums.append(val)
    if not nums:
        return None
    currency = "INR" if _RUPEE_HINT.search(text) else ("USD" if "$" in text else "")
    return max(nums) * mult, currency


def check_salary(job: JobPosting, cfg: Config) -> RuleResult:
    sc = cfg.hard_filters.salary
    if not sc.enabled:
        return RuleResult("salary", True, "filter disabled")

    amount = job.salary_max if sc.compare_on == "max" else job.salary_min
    if amount is None:
        amount = job.salary_min if sc.compare_on == "max" else job.salary_max
    currency = (job.salary_currency or "").upper()

    if amount is None and sc.parse_raw_string and job.salary_string:
        parsed = _annualise_raw(job.salary_string)
        if parsed:
            amount, guess = parsed
            currency = currency or guess

    if amount is None:
        # only_when_disclosed=True  -> undisclosed salary is NOT a reason to exclude
        return RuleResult("salary", sc.only_when_disclosed,
                          "not disclosed" + (" - kept" if sc.only_when_disclosed else " - excluded"))

    if not currency:
        if (job.country_code or "").upper() == "IN" or _RUPEE_HINT.search(job.salary_string or ""):
            currency = "INR"
    rate = sc.inr_per_unit.get(currency)
    if rate is None:
        return RuleResult("salary", sc.only_when_disclosed,
                          f"currency '{currency or '?'}' has no FX rate - treated as undisclosed")

    lpa = amount * rate / 100_000
    if lpa < sc.min_lpa:
        return RuleResult("salary", False, f"~{lpa:.1f} LPA (< {sc.min_lpa})")
    return RuleResult("salary", True, f"~{lpa:.1f} LPA")


def check_geography(job: JobPosting, cfg: Config) -> RuleResult:
    gc = cfg.geography
    if not cfg.hard_filters.geography.require_match:
        return RuleResult("geography", True, "filter disabled")

    hay = " ".join(x for x in (job.location, job.short_location) if x).lower()
    india_match = (job.country_code or "").upper() == "IN" or any(
        loc.lower() in hay for loc in gc.india_locations
    )
    is_remote = bool(job.remote) or (job.work_arrangement or "").lower() == "remote"

    if india_match:
        return RuleResult("geography", True, "India location")
    if is_remote and gc.allow_international_remote:
        return RuleResult("geography", True, "international remote")
    if (job.country_code or "").upper() in {c.upper() for c in gc.countries}:
        return RuleResult("geography", True, f"country {job.country_code}")
    return RuleResult("geography", False, f"loc='{job.location or '?'}' country='{job.country_code or '?'}' remote={is_remote}")


def check_work_arrangement(job: JobPosting, cfg: Config) -> RuleResult:
    wa = (job.work_arrangement or "").lower()
    if not wa:
        return RuleResult("work_arrangement", True, "unspecified - kept")
    if wa in cfg.geography.work_arrangements:
        return RuleResult("work_arrangement", True, wa)
    return RuleResult("work_arrangement", False, f"'{wa}' not in {cfg.geography.work_arrangements}")


def check_employment_type(job: JobPosting, cfg: Config) -> RuleResult:
    et_raw = job.employment_type
    et = " ".join(et_raw).lower() if isinstance(et_raw, list) else str(et_raw or "").lower()
    if not et:
        return RuleResult("employment_type", True, "unspecified - kept")
    wanted = {t.lower().replace("-", " ").replace("_", " ") for t in cfg.employment.types}
    norm = et.replace("-", " ").replace("_", " ")
    if any(w in norm for w in wanted):
        return RuleResult("employment_type", True, et)
    return RuleResult("employment_type", False, f"'{et}' not in {sorted(wanted)}")


def check_excluded_categories(job: JobPosting, cfg: Config) -> RuleResult:
    cats = cfg.hard_filters.excluded_categories
    if not cats:
        return RuleResult("excluded_categories", True, "list empty")
    hay = f"{job.title}\n{job.description}".lower()
    hits = [c for c in cats if c.lower() in hay]
    if hits:
        return RuleResult("excluded_categories", False, f"matched {hits}")
    return RuleResult("excluded_categories", True, "no excluded term")


# --------------------------------------------------------------------------- #
def run_hard_filters(job: JobPosting, cfg: Config, my_years: int, today: date | None = None) -> FilterOutcome:
    today = today or date.today()
    outcome = FilterOutcome(job=job)
    outcome.results.append(check_recency(job, cfg, today))
    outcome.results.append(check_seniority(job, cfg, my_years))
    outcome.results.append(check_salary(job, cfg))
    outcome.results.append(check_geography(job, cfg))
    outcome.results.append(check_work_arrangement(job, cfg))
    outcome.results.append(check_employment_type(job, cfg))
    outcome.results.append(check_excluded_categories(job, cfg))
    return outcome
