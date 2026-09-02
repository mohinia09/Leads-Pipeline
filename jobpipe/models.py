"""Normalised, source-agnostic data structures used across the pipeline.

Every job source maps its own raw API payload into a `JobPosting`. Nothing
downstream (filters, matching, sheets) knows which source a job came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional


def _slug(value: str | None) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class JobPosting:
    """One job, normalised. `raw` keeps the original payload for debugging."""

    source: str                       # e.g. "jobspipe"
    source_id: str                    # stable id within that source
    title: str
    company: str
    url: str

    description: str = ""
    normalized_title: Optional[str] = None
    company_meta: dict[str, Any] = field(default_factory=dict)

    location: Optional[str] = None
    short_location: Optional[str] = None
    country_code: Optional[str] = None
    remote: Optional[bool] = None
    hybrid: Optional[bool] = None
    work_arrangement: Optional[str] = None      # remote | hybrid | onsite

    date_posted: Optional[date] = None
    status: Optional[str] = None                # e.g. "active" / "closed"

    salary_min: Optional[float] = None          # annual, in salary_currency units
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_string: Optional[str] = None         # raw text, if that is all we have

    seniority_raw: Optional[str] = None         # source's own label, if any
    is_manager: Optional[bool] = None
    employment_type: Optional[str] = None

    # Filled in by the pipeline while searching:
    matched_role_terms: list[str] = field(default_factory=list)
    matched_tier: Optional[str] = None          # primary | strong_adjacent | exploratory

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_key(self) -> str:
        """Exact identity: source + its own id."""
        return f"{self.source}:{self.source_id}"

    @property
    def composite_key(self) -> str:
        """Fuzzy identity: catches the same role resurfacing via another source."""
        title = self.normalized_title or self.title
        return "|".join(
            (_slug(self.company), _slug(title), _slug(self.short_location or self.location))
        )

    def dedup_keys(self) -> tuple[str, str]:
        return self.primary_key, self.composite_key


@dataclass
class MatchResult:
    """Output of the AI matching step for a single job."""

    label: str                        # "Good Match" | "Low Match" | "Not Viable"
    reason: str
    relevance: str = ""
    technical_fit: str = ""
    seniority_alignment: str = ""
    overall_quality: str = ""
    company_assessment: str = ""
    quality_score: int = 0            # 0-100, used only for ordering within a label

    LABELS = ("Good Match", "Low Match", "Not Viable")
