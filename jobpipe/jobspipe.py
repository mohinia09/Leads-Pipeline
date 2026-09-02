"""JobsPipe client: build a search from the config, call the API, return
normalised JobPosting objects.

Sandbox mode hits POST /v1/sandbox/jobs/search (no key, no quota) and returns
sample data in the live schema - use it to confirm wiring before spending quota.
Live mode hits POST /v1/jobs/search with a Bearer key.

The exact response field names are only partly documented; `_map_job` reads
each value through a list of candidate keys and keeps the untouched payload in
`JobPosting.raw` so a real response can be inspected and the mapping tightened.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import requests

from .config import Config
from .models import JobPosting

try:  # use the OS trust store (Windows) so TLS verifies like curl / pip do
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - fall back to certifi
    pass

BASE_URL = "https://api.jobspipe.dev"
LIVE_PATH = "/v1/jobs/search"
SANDBOX_PATH = "/v1/sandbox/jobs/search"
TIMEOUT = 30


class JobsPipeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Request building
# --------------------------------------------------------------------------- #
def _base_filter(cfg: Config) -> dict[str, Any]:
    f: dict[str, Any] = {
        "job_title_or": cfg.roles.all_terms,
        "posted_at_max_age_days": cfg.jobspipe.fetch_posted_within_days,
    }
    if cfg.roles.excluded_title_terms:
        f["job_title_not"] = cfg.roles.excluded_title_terms
    if cfg.employment.types:
        f["employment_type_or"] = cfg.employment.types
    if cfg.jobspipe.seniority_or:
        f["job_seniority_or"] = cfg.jobspipe.seniority_or
    # Only constrain work arrangement if the user narrowed it.
    if set(cfg.geography.work_arrangements) != {"remote", "hybrid", "onsite"}:
        f["work_arrangement_or"] = cfg.geography.work_arrangements
    return f


def _search_bodies(cfg: Config) -> list[dict[str, Any]]:
    """One body targeting the configured countries, plus (optionally) one for
    international remote roles."""
    bodies: list[dict[str, Any]] = []

    in_country = _base_filter(cfg)
    if cfg.geography.countries:
        in_country["job_country_code_or"] = cfg.geography.countries
    bodies.append(in_country)

    if cfg.geography.allow_international_remote:
        remote_any = _base_filter(cfg)
        remote_any["remote"] = True
        bodies.append(remote_any)

    return bodies


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _endpoint(cfg: Config) -> str:
    path = SANDBOX_PATH if cfg.jobspipe.mode == "sandbox" else LIVE_PATH
    return BASE_URL + path


def _headers(cfg: Config) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if cfg.jobspipe.mode == "live":
        key = cfg.secrets.jobspipe_api_key
        if not key:
            raise JobsPipeError(
                "jobspipe.mode is 'live' but JOBSPIPE_API_KEY is not set in .env"
            )
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _post(cfg: Config, body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = requests.post(
            _endpoint(cfg), json=body, headers=_headers(cfg), timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        raise JobsPipeError(f"request to JobsPipe failed: {exc}") from exc

    if resp.status_code == 401:
        raise JobsPipeError("401 from JobsPipe - missing or invalid API key")
    if resp.status_code == 402:
        raise JobsPipeError("402 from JobsPipe - monthly quota exhausted")
    if resp.status_code == 429:
        raise JobsPipeError("429 from JobsPipe - rate limited; slow down and retry")
    if resp.status_code >= 400:
        raise JobsPipeError(f"{resp.status_code} from JobsPipe: {resp.text[:400]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise JobsPipeError(f"JobsPipe returned non-JSON: {resp.text[:400]}") from exc


class _Budget:
    """Hard ceiling on API calls for one run (protects free-tier credits)."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.used = 0

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True


def _paginate(cfg: Config, body: dict[str, Any], budget: _Budget) -> Iterable[dict[str, Any]]:
    cursor: str | None = None
    for _ in range(cfg.jobspipe.max_pages):
        if not budget.take():
            return
        page_body = dict(body, limit=cfg.jobspipe.per_query_limit)
        if cursor:
            page_body["cursor"] = cursor
        payload = _post(cfg, page_body)
        data = payload.get("data") or payload.get("jobs") or payload.get("results") or []
        for raw in data:
            yield raw
        meta = payload.get("metadata") or payload.get("meta") or {}
        cursor = meta.get("next_cursor") or meta.get("cursor")
        if not cursor or not data:
            break


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def _first(raw: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value)).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (
        lambda t: datetime.fromisoformat(t).date(),
        lambda t: datetime.strptime(t[:10], "%Y-%m-%d").date(),
        lambda t: datetime.strptime(t, "%d %b %Y").date(),
    ):
        try:
            return parser(text)
        except (ValueError, TypeError):
            continue
    return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _map_job(raw: dict[str, Any]) -> JobPosting:
    company_obj = raw.get("company_object") or raw.get("company_data") or {}
    domain = _first(raw, "company_domain", "company_website")
    if not isinstance(company_obj, dict) or not company_obj:
        company_obj = {"domain": domain} if domain else {}
    company = _first(raw, "company", "company_name") or (
        company_obj.get("name") if isinstance(company_obj, dict) else None
    )

    # Salary: prefer native currency fields; fall back to the *_usd variants,
    # which the API has already converted to USD.
    smin = _to_float(_first(raw, "min_annual_salary", "salary_min", "min_salary"))
    smax = _to_float(_first(raw, "max_annual_salary", "salary_max", "max_salary"))
    currency = _first(raw, "salary_currency", "currency")
    if smin is None and smax is None:
        smin = _to_float(_first(raw, "min_annual_salary_usd"))
        smax = _to_float(_first(raw, "max_annual_salary_usd"))
        if (smin is not None or smax is not None) and not currency:
            currency = "USD"

    return JobPosting(
        source="jobspipe",
        source_id=str(
            _first(raw, "id", "job_id", "uuid")
            or _first(raw, "final_url", "url", "source_url")
            or ""
        ),
        title=str(_first(raw, "job_title", "title", "normalized_title") or ""),
        company=str(company or ""),
        url=str(_first(raw, "final_url", "url", "source_url", "apply_url", "job_url") or ""),
        description=str(
            _first(raw, "description", "job_description", "description_text", "text", "body") or ""
        ),
        normalized_title=_first(raw, "normalized_title"),
        company_meta=company_obj if isinstance(company_obj, dict) else {},
        location=_first(raw, "location", "long_location", "job_location"),
        short_location=_first(raw, "short_location", "location"),
        country_code=_first(raw, "country_code", "job_country_code"),
        remote=_first(raw, "remote", "is_remote"),
        hybrid=_first(raw, "hybrid", "is_hybrid"),
        work_arrangement=_first(raw, "work_arrangement", "workplace_type"),
        date_posted=_to_date(_first(raw, "date_posted", "posted_at", "published_at", "discovered_at")),
        status=_first(raw, "status", "job_status"),
        salary_min=smin,
        salary_max=smax,
        salary_currency=currency,
        salary_string=_first(raw, "salary_string", "salary", "salary_text"),
        seniority_raw=_first(raw, "seniority", "seniority_level"),
        is_manager=_first(raw, "is_manager", "manager"),
        employment_type=_first(raw, "employment_type", "employment_statuses", "job_type"),
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _title_excluded(title: str, terms: list[str]) -> bool:
    t = (title or "").lower()
    return any(term.lower() in t for term in terms)


def search_jobs(cfg: Config, log=None) -> list[JobPosting]:
    """Run each search body (paginated), map, de-duplicate on primary key.
    Never makes more than jobspipe.max_requests_per_run HTTP calls."""
    budget = _Budget(cfg.jobspipe.max_requests_per_run)
    excl = cfg.roles.excluded_title_terms
    seen: set[str] = set()
    out: list[JobPosting] = []
    dropped = 0
    for body in _search_bodies(cfg):
        if budget.left <= 0:
            break
        for raw in _paginate(cfg, body, budget):
            job = _map_job(raw)
            if job.primary_key in seen:
                continue
            seen.add(job.primary_key)
            if _title_excluded(job.title, excl):   # backstop for job_title_not
                dropped += 1
                continue
            out.append(job)
    if log:
        log(f"      JobsPipe API calls used: {budget.used}/{cfg.jobspipe.max_requests_per_run}"
            + (f"; {dropped} dropped on excluded title terms" if dropped else ""))
    return out
