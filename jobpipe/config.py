"""Load and validate config.yaml into typed dataclasses.

Rules:
- Nothing in the pipeline is hardcoded; every knob comes from here.
- A bad config fails loudly with a specific message (which key, what was wrong)
  rather than blowing up somewhere deep later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when config.yaml is missing keys or has invalid values."""


# --------------------------------------------------------------------------- #
# Small validation helpers - each raises ConfigError with a dotted key path.
# --------------------------------------------------------------------------- #
def _req(d: dict, key: str, path: str) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"Missing required key: {path}")
    return d[key]


def _opt(d: dict, key: str, default: Any) -> Any:
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def _as_dict(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping, got {type(value).__name__}")
    return value


def _as_list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list, got {type(value).__name__}")
    return value


def _as_str_list(value: Any, path: str) -> list[str]:
    lst = _as_list(value, path)
    for i, item in enumerate(lst):
        if not isinstance(item, str):
            raise ConfigError(f"{path}[{i}] must be a string, got {type(item).__name__}")
    return lst


def _as_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path} must be >= {minimum}, got {value}")
    return value


def _as_number(value: Any, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{path} must be >= {minimum}, got {value}")
    return float(value)


def _as_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false, got {value!r}")
    return value


def _one_of(value: Any, options: tuple[str, ...], path: str) -> str:
    if value not in options:
        raise ConfigError(f"{path} must be one of {options}, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Typed config sections
# --------------------------------------------------------------------------- #
@dataclass
class RunCfg:
    daily_cap: int
    posted_within_days: int
    timezone: str

    @classmethod
    def parse(cls, d: dict) -> "RunCfg":
        d = _as_dict(d, "run")
        return cls(
            daily_cap=_as_int(_req(d, "daily_cap", "run.daily_cap"), "run.daily_cap", minimum=1),
            posted_within_days=_as_int(
                _req(d, "posted_within_days", "run.posted_within_days"),
                "run.posted_within_days",
                minimum=1,
            ),
            timezone=str(_opt(d, "timezone", "Asia/Kolkata")),
        )


@dataclass
class RolesCfg:
    primary: list[str]
    strong_adjacent: list[str]
    exploratory: list[str]
    excluded_title_terms: list[str]

    @property
    def all_terms(self) -> list[str]:
        return [*self.primary, *self.strong_adjacent, *self.exploratory]

    def tier_of(self, term: str) -> str | None:
        if term in self.primary:
            return "primary"
        if term in self.strong_adjacent:
            return "strong_adjacent"
        if term in self.exploratory:
            return "exploratory"
        return None

    @classmethod
    def parse(cls, d: dict) -> "RolesCfg":
        d = _as_dict(d, "roles")
        roles = cls(
            primary=_as_str_list(_req(d, "primary", "roles.primary"), "roles.primary"),
            strong_adjacent=_as_str_list(
                _req(d, "strong_adjacent", "roles.strong_adjacent"), "roles.strong_adjacent"
            ),
            exploratory=_as_str_list(
                _req(d, "exploratory", "roles.exploratory"), "roles.exploratory"
            ),
            excluded_title_terms=_as_str_list(
                _opt(d, "excluded_title_terms", []), "roles.excluded_title_terms"
            ),
        )
        if not roles.all_terms:
            raise ConfigError("roles: at least one role term is required across the three tiers")
        return roles


@dataclass
class GeographyCfg:
    countries: list[str]
    india_locations: list[str]
    allow_international_remote: bool
    work_arrangements: list[str]

    @classmethod
    def parse(cls, d: dict) -> "GeographyCfg":
        d = _as_dict(d, "geography")
        arrangements = _as_str_list(
            _req(d, "work_arrangements", "geography.work_arrangements"),
            "geography.work_arrangements",
        )
        for a in arrangements:
            _one_of(a, ("remote", "hybrid", "onsite"), "geography.work_arrangements[]")
        return cls(
            countries=_as_str_list(_req(d, "countries", "geography.countries"), "geography.countries"),
            india_locations=_as_str_list(
                _opt(d, "india_locations", []), "geography.india_locations"
            ),
            allow_international_remote=_as_bool(
                _opt(d, "allow_international_remote", True),
                "geography.allow_international_remote",
            ),
            work_arrangements=arrangements,
        )


@dataclass
class EmploymentCfg:
    types: list[str]

    @classmethod
    def parse(cls, d: dict) -> "EmploymentCfg":
        d = _as_dict(d, "employment")
        return cls(types=_as_str_list(_req(d, "types", "employment.types"), "employment.types"))


@dataclass
class RecencyCfg:
    days: int
    prefer_status_over_date: bool

    @classmethod
    def parse(cls, d: dict) -> "RecencyCfg":
        d = _as_dict(d, "hard_filters.recency")
        return cls(
            days=_as_int(_req(d, "days", "hard_filters.recency.days"), "hard_filters.recency.days", minimum=1),
            prefer_status_over_date=_as_bool(
                _opt(d, "prefer_status_over_date", True),
                "hard_filters.recency.prefer_status_over_date",
            ),
        )


@dataclass
class SeniorityCfg:
    my_years_experience: Any            # "auto" or an int
    min_years: int
    window_below: int
    window_above: int
    no_requirement_action: str          # "pass" | "reject"

    @property
    def years_is_auto(self) -> bool:
        return isinstance(self.my_years_experience, str) and self.my_years_experience.lower() == "auto"

    def accept_window(self, my_years: int) -> tuple[int, int]:
        low = max(self.min_years, my_years - self.window_below)
        high = my_years + self.window_above
        return low, high

    @classmethod
    def parse(cls, d: dict) -> "SeniorityCfg":
        d = _as_dict(d, "hard_filters.seniority")
        raw_years = _req(d, "my_years_experience", "hard_filters.seniority.my_years_experience")
        if isinstance(raw_years, str):
            if raw_years.lower() != "auto":
                raise ConfigError(
                    "hard_filters.seniority.my_years_experience must be 'auto' or an integer"
                )
            years: Any = "auto"
        else:
            years = _as_int(raw_years, "hard_filters.seniority.my_years_experience", minimum=0)
        return cls(
            my_years_experience=years,
            min_years=_as_int(
                _opt(d, "min_years", 0), "hard_filters.seniority.min_years", minimum=0
            ),
            window_below=_as_int(
                _opt(d, "window_below", 3), "hard_filters.seniority.window_below", minimum=0
            ),
            window_above=_as_int(
                _opt(d, "window_above", 4), "hard_filters.seniority.window_above", minimum=0
            ),
            no_requirement_action=_one_of(
                _opt(d, "no_requirement_action", "pass"),
                ("pass", "reject"),
                "hard_filters.seniority.no_requirement_action",
            ),
        )


@dataclass
class SalaryCfg:
    enabled: bool
    min_lpa: float
    only_when_disclosed: bool
    compare_on: str                    # "min" | "max"
    parse_raw_string: bool
    inr_per_unit: dict[str, float]

    @classmethod
    def parse(cls, d: dict) -> "SalaryCfg":
        d = _as_dict(d, "hard_filters.salary")
        fx_raw = _as_dict(
            _opt(d, "inr_per_unit", {"INR": 1}), "hard_filters.salary.inr_per_unit"
        )
        fx: dict[str, float] = {}
        for code, rate in fx_raw.items():
            fx[str(code).upper()] = _as_number(
                rate, f"hard_filters.salary.inr_per_unit.{code}", minimum=0
            )
        if "INR" not in fx:
            fx["INR"] = 1.0
        return cls(
            enabled=_as_bool(_opt(d, "enabled", True), "hard_filters.salary.enabled"),
            min_lpa=_as_number(
                _req(d, "min_lpa", "hard_filters.salary.min_lpa"),
                "hard_filters.salary.min_lpa",
                minimum=0,
            ),
            only_when_disclosed=_as_bool(
                _opt(d, "only_when_disclosed", True),
                "hard_filters.salary.only_when_disclosed",
            ),
            compare_on=_one_of(
                _opt(d, "compare_on", "max"), ("min", "max"), "hard_filters.salary.compare_on"
            ),
            parse_raw_string=_as_bool(
                _opt(d, "parse_raw_string", True), "hard_filters.salary.parse_raw_string"
            ),
            inr_per_unit=fx,
        )


@dataclass
class GeoFilterCfg:
    require_match: bool

    @classmethod
    def parse(cls, d: dict) -> "GeoFilterCfg":
        d = _as_dict(d, "hard_filters.geography")
        return cls(
            require_match=_as_bool(
                _opt(d, "require_match", True), "hard_filters.geography.require_match"
            )
        )


@dataclass
class HardFiltersCfg:
    recency: RecencyCfg
    seniority: SeniorityCfg
    salary: SalaryCfg
    geography: GeoFilterCfg
    excluded_categories: list[str]

    @classmethod
    def parse(cls, d: dict) -> "HardFiltersCfg":
        d = _as_dict(d, "hard_filters")
        return cls(
            recency=RecencyCfg.parse(_req(d, "recency", "hard_filters.recency")),
            seniority=SeniorityCfg.parse(_req(d, "seniority", "hard_filters.seniority")),
            salary=SalaryCfg.parse(_req(d, "salary", "hard_filters.salary")),
            geography=GeoFilterCfg.parse(_req(d, "geography", "hard_filters.geography")),
            excluded_categories=_as_str_list(
                _opt(d, "excluded_categories", []), "hard_filters.excluded_categories"
            ),
        )


_SENIORITY_VALUES = ("entry_level", "mid_level", "senior", "director", "executive")


@dataclass
class JobspipeCfg:
    mode: str                          # "sandbox" | "live"
    per_query_limit: int               # max results (= credits) requested per search
    max_pages: int
    max_requests_per_run: int          # hard ceiling on API calls per run (free-tier credits)
    fetch_posted_within_days: int      # API-side recency window (small for daily runs)
    seniority_or: list[str]            # job_seniority_or sent to the API ([] = don't send)

    @classmethod
    def parse(cls, d: dict) -> "JobspipeCfg":
        d = _as_dict(d, "jobspipe")
        seniority = _as_str_list(_opt(d, "seniority_or", []), "jobspipe.seniority_or")
        for s in seniority:
            _one_of(s, _SENIORITY_VALUES, "jobspipe.seniority_or[]")
        return cls(
            mode=_one_of(
                _opt(d, "mode", "sandbox"), ("sandbox", "live"), "jobspipe.mode"
            ),
            per_query_limit=_as_int(
                _opt(d, "per_query_limit", 50), "jobspipe.per_query_limit", minimum=1
            ),
            max_pages=_as_int(_opt(d, "max_pages", 3), "jobspipe.max_pages", minimum=1),
            max_requests_per_run=_as_int(
                _opt(d, "max_requests_per_run", 5),
                "jobspipe.max_requests_per_run",
                minimum=1,
            ),
            fetch_posted_within_days=_as_int(
                _opt(d, "fetch_posted_within_days", 7),
                "jobspipe.fetch_posted_within_days",
                minimum=1,
            ),
            seniority_or=seniority,
        )


@dataclass
class AiMatchingCfg:
    enabled: bool
    model: str
    max_tokens: int
    include_low_match: bool
    drop_not_viable: bool
    resume_max_chars: int              # safety bound on resume text sent (full resume is well under)
    description_max_chars: int         # cap on JD text sent to the model
    strip_jd_boilerplate: bool         # drop About/Benefits/EEO/how-to-apply sections before the cap
    jd_extra_boilerplate_markers: list[str]

    @classmethod
    def parse(cls, d: dict) -> "AiMatchingCfg":
        d = _as_dict(d, "ai_matching")
        return cls(
            enabled=_as_bool(_opt(d, "enabled", True), "ai_matching.enabled"),
            model=str(_opt(d, "model", "claude-haiku-4-5")),
            max_tokens=_as_int(_opt(d, "max_tokens", 500), "ai_matching.max_tokens", minimum=1),
            include_low_match=_as_bool(
                _opt(d, "include_low_match", True), "ai_matching.include_low_match"
            ),
            drop_not_viable=_as_bool(
                _opt(d, "drop_not_viable", True), "ai_matching.drop_not_viable"
            ),
            resume_max_chars=_as_int(
                _opt(d, "resume_max_chars", 40000), "ai_matching.resume_max_chars", minimum=1000
            ),
            description_max_chars=_as_int(
                _opt(d, "description_max_chars", 5000),
                "ai_matching.description_max_chars",
                minimum=500,
            ),
            strip_jd_boilerplate=_as_bool(
                _opt(d, "strip_jd_boilerplate", True), "ai_matching.strip_jd_boilerplate"
            ),
            jd_extra_boilerplate_markers=_as_str_list(
                _opt(d, "jd_extra_boilerplate_markers", []),
                "ai_matching.jd_extra_boilerplate_markers",
            ),
        )


@dataclass
class GoogleSheetCfg:
    spreadsheet_key: str
    worksheet: str
    rejected_worksheet: str
    service_account_file: str

    @classmethod
    def parse(cls, d: dict) -> "GoogleSheetCfg":
        d = _as_dict(d, "output.google_sheet")
        return cls(
            spreadsheet_key=str(_opt(d, "spreadsheet_key", "")),
            worksheet=str(_opt(d, "worksheet", "Jobs")),
            rejected_worksheet=str(_opt(d, "rejected_worksheet", "")),
            service_account_file=str(
                _opt(d, "service_account_file", "secrets/sheet-credentials.json")
            ),
        )


@dataclass
class DedupCfg:
    processed_store: str
    rescore_after_days: int

    @classmethod
    def parse(cls, d: dict) -> "DedupCfg":
        d = _as_dict(d, "output.dedup")
        return cls(
            processed_store=str(_opt(d, "processed_store", "data/processed_jobs.json")),
            rescore_after_days=_as_int(
                _opt(d, "rescore_after_days", 7),
                "output.dedup.rescore_after_days",
                minimum=0,
            ),
        )


@dataclass
class OutputCfg:
    google_sheet: GoogleSheetCfg
    dedup: DedupCfg

    @classmethod
    def parse(cls, d: dict) -> "OutputCfg":
        d = _as_dict(d, "output")
        return cls(
            google_sheet=GoogleSheetCfg.parse(_req(d, "google_sheet", "output.google_sheet")),
            dedup=DedupCfg.parse(_opt(d, "dedup", {})),
        )


@dataclass
class Secrets:
    jobspipe_api_key: str | None
    anthropic_api_key: str | None
    google_sheet_id: str | None        # GOOGLE_SHEET_ID in .env, overrides config

    @classmethod
    def load(cls) -> "Secrets":
        load_dotenv()
        return cls(
            jobspipe_api_key=os.getenv("JOBSPIPE_API_KEY") or None,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            google_sheet_id=(
                os.getenv("GOOGLE_SHEET_ID") or os.getenv("GOOGLE_SHEET_KEY") or None
            ),
        )


@dataclass
class Config:
    run: RunCfg
    resume_path: str
    roles: RolesCfg
    geography: GeographyCfg
    employment: EmploymentCfg
    hard_filters: HardFiltersCfg
    jobspipe: JobspipeCfg
    ai_matching: AiMatchingCfg
    output: OutputCfg
    secrets: Secrets
    path: Path

    @property
    def spreadsheet_key(self) -> str:
        """GOOGLE_SHEET_ID in .env wins; otherwise config.yaml."""
        return self.secrets.google_sheet_id or self.output.google_sheet.spreadsheet_key

    # ------------------------------------------------------------------ #
    def summary(self) -> str:
        """Human-readable resolved view, for `run --check-config`."""
        lines: list[str] = []
        lines.append(f"config file        : {self.path}")
        lines.append(f"daily cap          : {self.run.daily_cap}")
        lines.append(f"posted within      : {self.run.posted_within_days} days")
        lines.append(
            "role terms         : "
            f"{len(self.roles.primary)} primary / {len(self.roles.strong_adjacent)} adjacent / "
            f"{len(self.roles.exploratory)} exploratory"
        )
        lines.append(f"countries          : {', '.join(self.geography.countries)}")
        lines.append(
            f"work arrangements  : {', '.join(self.geography.work_arrangements)}"
            + ("  (+ international remote)" if self.geography.allow_international_remote else "")
        )
        lines.append(f"employment types   : {', '.join(self.employment.types)}")
        sen = self.hard_filters.seniority
        yrs = "auto (from resume)" if sen.years_is_auto else str(sen.my_years_experience)
        lines.append(
            f"seniority filter   : my years = {yrs}; "
            f"window -{sen.window_below}/+{sen.window_above}; floor {sen.min_years}; "
            f"no-requirement -> {sen.no_requirement_action}"
        )
        sal = self.hard_filters.salary
        if sal.enabled:
            lines.append(
                f"salary filter      : >= {sal.min_lpa} LPA on {sal.compare_on}; "
                f"disclosed-only={sal.only_when_disclosed}; parse-raw={sal.parse_raw_string}; "
                f"fx={sal.inr_per_unit}"
            )
        else:
            lines.append("salary filter      : disabled")
        lines.append(
            f"recency filter     : {self.hard_filters.recency.days} days; "
            f"prefer status={self.hard_filters.recency.prefer_status_over_date}"
        )
        lines.append(
            f"excluded categories: {self.hard_filters.excluded_categories or '(none)'}"
        )
        jp = self.jobspipe
        lines.append(
            f"jobspipe           : mode={jp.mode}; per_query_limit={jp.per_query_limit}; "
            f"max_pages={jp.max_pages}; max_requests_per_run={jp.max_requests_per_run}"
        )
        lines.append(
            f"jobspipe fetch     : posted<={jp.fetch_posted_within_days}d; "
            f"seniority_or={jp.seniority_or or '(not sent)'}; "
            f"international_remote={self.geography.allow_international_remote}"
        )
        ai = self.ai_matching
        lines.append(
            f"ai matching        : {'on' if ai.enabled else 'off'}; model={ai.model}; "
            f"include_low_match={ai.include_low_match}; drop_not_viable={ai.drop_not_viable}"
        )
        lines.append(
            f"ai text limits     : resume<={ai.resume_max_chars}c (full); "
            f"jd<={ai.description_max_chars}c; strip_jd_boilerplate={ai.strip_jd_boilerplate}"
        )
        gs = self.output.google_sheet
        key_src = (
            "from .env" if self.secrets.google_sheet_id
            else ("from config" if gs.spreadsheet_key else "MISSING")
        )
        lines.append(
            f"google sheet       : key={key_src}; tab={gs.worksheet}; creds={gs.service_account_file}"
        )
        lines.append(
            f"dedup / store      : {self.output.dedup.processed_store}; "
            f"rescore_after_days={self.output.dedup.rescore_after_days}"
        )
        lines.append(
            "secrets            : "
            f"JOBSPIPE_API_KEY={'set' if self.secrets.jobspipe_api_key else 'not set'}, "
            f"ANTHROPIC_API_KEY={'set' if self.secrets.anthropic_api_key else 'not set'}, "
            f"GOOGLE_SHEET_ID={'set' if self.secrets.google_sheet_id else 'not set'}"
        )
        return "\n".join(lines)

    def warnings(self) -> list[str]:
        """Non-fatal issues worth surfacing during --check-config."""
        w: list[str] = []
        if self.jobspipe.mode == "live" and not self.secrets.jobspipe_api_key:
            w.append("jobspipe.mode is 'live' but JOBSPIPE_API_KEY is not set in .env")
        if self.ai_matching.enabled and not self.secrets.anthropic_api_key:
            w.append("ai_matching.enabled is true but ANTHROPIC_API_KEY is not set in .env")
        if not self.spreadsheet_key:
            w.append("no Google Sheet id (set GOOGLE_SHEET_ID in .env or spreadsheet_key in config)")
        sa = Path(self.output.google_sheet.service_account_file)
        if not sa.is_absolute():
            sa = self.path.parent / sa
        if not sa.exists():
            w.append(f"service account file not found at {self.output.google_sheet.service_account_file}")
        resume = Path(self.resume_path)
        if not resume.is_absolute():
            resume = self.path.parent / resume
        if not resume.exists():
            w.append(f"resume file not found at {self.resume_path}")
        return w


# --------------------------------------------------------------------------- #
def load_config(path: str | Path = "config.yaml") -> Config:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path}. Copy config.example.yaml to config.yaml."
        )
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough of parser detail
        raise ConfigError(f"Could not parse {cfg_path} as YAML:\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path} did not parse to a mapping at the top level")

    return Config(
        run=RunCfg.parse(_req(raw, "run", "run")),
        resume_path=str(_req(_as_dict(_req(raw, "resume", "resume"), "resume"), "path", "resume.path")),
        roles=RolesCfg.parse(_req(raw, "roles", "roles")),
        geography=GeographyCfg.parse(_req(raw, "geography", "geography")),
        employment=EmploymentCfg.parse(_req(raw, "employment", "employment")),
        hard_filters=HardFiltersCfg.parse(_req(raw, "hard_filters", "hard_filters")),
        jobspipe=JobspipeCfg.parse(_req(raw, "jobspipe", "jobspipe")),
        ai_matching=AiMatchingCfg.parse(_req(raw, "ai_matching", "ai_matching")),
        output=OutputCfg.parse(_req(raw, "output", "output")),
        secrets=Secrets.load(),
        path=cfg_path.resolve(),
    )
