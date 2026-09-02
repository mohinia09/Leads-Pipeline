"""Append matched jobs to a Google Sheet, skipping anything already known.

The caller passes `blocked_keys` (primary + composite keys already written,
from the processed store). This function also reads the sheet's own 'Dedup Key'
column as a second source of truth. A job is skipped if either of its keys is
in that combined set.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from .config import Config
from .models import JobPosting, MatchResult

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry(fn, *, what: str, tries: int = 4, log=print):
    """Call fn(); retry on transient Google API errors (429/5xx) with backoff."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in _RETRY_STATUS or attempt == tries:
                raise
            wait = 2 ** attempt
            log(f"  {what}: {status} from Google, retrying in {wait}s ({attempt}/{tries - 1})")
            time.sleep(wait)

COLUMNS = [
    "Date Added",
    "Match Label",
    "Score",
    "Role Relevance",
    "Title",
    "Company",
    "Location",
    "Work Arrangement",
    "Posted",
    "Status",
    "Salary",
    "Years Req (parsed)",
    "Technical Fit",
    "Seniority Fit",
    "Company Notes",
    "Reason",
    "Role Tier",
    "Matched Terms",
    "URL",
    "Source",
    "Dedup Key",
]
_DEDUP_COL = COLUMNS.index("Dedup Key") + 1  # 1-based for gspread


class SheetsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Row building
# --------------------------------------------------------------------------- #
def _salary_text(job: JobPosting) -> str:
    if job.salary_string:
        return job.salary_string
    if job.salary_min or job.salary_max:
        return f"{job.salary_min or '?'}-{job.salary_max or '?'} {job.salary_currency or ''}".strip()
    return ""


def build_row(job: JobPosting, mr: MatchResult, years_req: str, run_day: date) -> list[str]:
    values = {
        "Date Added": run_day.isoformat(),
        "Match Label": mr.label,
        "Score": str(mr.quality_score),
        "Role Relevance": mr.relevance,
        "Title": job.title,
        "Company": job.company,
        "Location": job.location or job.short_location or "",
        "Work Arrangement": job.work_arrangement or ("remote" if job.remote else ""),
        "Posted": job.date_posted.isoformat() if job.date_posted else "",
        "Status": job.status or "",
        "Salary": _salary_text(job),
        "Years Req (parsed)": years_req,
        "Technical Fit": mr.technical_fit,
        "Seniority Fit": mr.seniority_alignment,
        "Company Notes": mr.company_assessment,
        "Reason": mr.reason,
        "Role Tier": job.matched_tier or "",
        "Matched Terms": ", ".join(job.matched_role_terms),
        "URL": job.url,
        "Source": job.source,
        "Dedup Key": job.primary_key,
    }
    return [str(values[c]) for c in COLUMNS]


# --------------------------------------------------------------------------- #
# Sheet access
# --------------------------------------------------------------------------- #
def _open_worksheet(cfg: Config, log=print):
    gs = cfg.output.google_sheet
    key = cfg.spreadsheet_key
    if not key:
        raise SheetsError(
            "no Google Sheet id - set GOOGLE_SHEET_ID in .env or spreadsheet_key in config.yaml"
        )

    sa_path = Path(gs.service_account_file)
    if not sa_path.is_absolute():
        sa_path = cfg.path.parent / sa_path
    if not sa_path.exists():
        # fall back to the only *.json in secrets/
        secrets_dir = cfg.path.parent / "secrets"
        jsons = sorted(secrets_dir.glob("*.json")) if secrets_dir.is_dir() else []
        if len(jsons) == 1:
            sa_path = jsons[0]
        else:
            raise SheetsError(
                f"service account file not found: {sa_path} "
                f"(and {'no' if not jsons else 'multiple'} .json in secrets/ to fall back to)"
            )

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:  # pragma: no cover
        raise SheetsError("gspread / google-auth not installed (pip install -r requirements.txt)") from exc

    creds = Credentials.from_service_account_file(str(sa_path), scopes=SCOPES)
    client = gspread.authorize(creds)
    try:
        sheet = _retry(lambda: client.open_by_key(key), what="open sheet", log=log)
    except Exception as exc:  # noqa: BLE001
        raise SheetsError(
            f"could not open spreadsheet {key}: {exc}. "
            "Is it shared with the service account's client_email as Editor?"
        ) from exc

    try:
        ws = _retry(lambda: sheet.worksheet(gs.worksheet), what="open tab", log=log)
    except Exception:  # noqa: BLE001 - worksheet missing
        ws = _retry(
            lambda: sheet.add_worksheet(title=gs.worksheet, rows=1000, cols=len(COLUMNS)),
            what="create tab", log=log,
        )

    header = _retry(lambda: ws.row_values(1), what="read header", log=log)
    if header != COLUMNS:
        if not header:
            _retry(lambda: ws.update(range_name="A1", values=[COLUMNS]),
                   what="write header", log=log)
        else:
            raise SheetsError(
                f"worksheet '{gs.worksheet}' has an unexpected header row. "
                f"Expected {COLUMNS[:3]}... - clear it or point to a fresh tab."
            )
    return ws


def existing_keys(ws, log=print) -> set[str]:
    try:
        col = _retry(lambda: ws.col_values(_DEDUP_COL), what="read dedup column", log=log)
    except Exception:  # noqa: BLE001
        return set()
    return {v for v in col[1:] if v}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def append_results(cfg: Config, pairs, years_reqs: dict[str, str], *,
                   blocked_keys: set[str], dry_run: bool, log=print):
    """pairs: list[(JobPosting, MatchResult)] already sorted & capped.
    blocked_keys: primary/composite keys already written (from the processed store).
    Returns (written_primary_keys: list[str], skipped: int)."""
    run_day = date.today()

    if dry_run:
        new = [(j, m) for j, m in pairs if not (blocked_keys & set(j.dedup_keys()))]
        log(f"  [dry-run] would append {len(new)}, skip {len(pairs) - len(new)} already-known")
        return [j.primary_key for j, _ in new], len(pairs) - len(new)

    ws = _open_worksheet(cfg, log=log)
    blocked = set(blocked_keys) | existing_keys(ws, log=log)

    rows, written = [], []
    skipped = 0
    for job, mr in pairs:
        keys = set(job.dedup_keys())
        if blocked & keys:
            skipped += 1
            continue
        rows.append(build_row(job, mr, years_reqs.get(job.primary_key, ""), run_day))
        blocked |= keys
        written.append(job.primary_key)

    if rows:
        _retry(lambda: ws.append_rows(rows, value_input_option="USER_ENTERED"),
               what="append rows", log=log)
    log(f"  appended {len(rows)} row(s), skipped {skipped} already-known")
    return written, skipped
