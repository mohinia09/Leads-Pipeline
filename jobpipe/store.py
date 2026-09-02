"""Processed-jobs store.

Holds a record for every job that has ever been sent to the AI matcher, keyed by
JobPosting.primary_key, so the pipeline never pays to score the same posting
twice. Also stores a fingerprint of the resume text - if the resume changes,
cached scores are treated as stale and the jobs are re-scored.

status(job) -> one of:
  "skip"   - already written to the sheet, ignore entirely
  "cached" - scored before, not yet written: reuse the stored label/score, no AI
  "score"  - new, or resume changed, or older than rescore_after_days: send to AI
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .models import JobPosting, MatchResult

_MATCH_FIELDS = {
    "label", "reason", "relevance", "technical_fit", "seniority_alignment",
    "overall_quality", "company_assessment", "quality_score",
}


def resume_fingerprint(resume_text: str) -> str:
    """SHA-256 of the whitespace-normalised, lowercased resume text."""
    norm = re.sub(r"\s+", " ", resume_text or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _age_days(iso: str, today: date) -> int:
    try:
        return (today - date.fromisoformat(iso[:10])).days
    except (ValueError, TypeError):
        return 10 ** 6


@dataclass
class Entry:
    label: str
    score: int
    reason: str
    composite: str
    first_seen: str
    last_scored: str
    written: bool
    match: dict = field(default_factory=dict)   # full MatchResult fields for faithful reuse

    def to_result(self) -> MatchResult:
        data = {k: v for k, v in self.match.items() if k in _MATCH_FIELDS}
        data.setdefault("label", self.label)
        data.setdefault("reason", self.reason)
        data.setdefault("quality_score", self.score)
        return MatchResult(**data)


class ProcessedStore:
    def __init__(self, path: Path, resume_hash: str) -> None:
        self.path = Path(path)
        self.resume_hash = resume_hash
        self.resume_changed = False
        self.jobs: dict[str, Entry] = {}
        self._by_composite: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path, resume_hash: str) -> "ProcessedStore":
        st = cls(path, resume_hash)
        p = Path(path)
        if not p.exists():
            return st
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return st
        prev = data.get("resume_sha256")
        st.resume_changed = bool(prev) and prev != resume_hash
        for key, e in (data.get("jobs") or {}).items():
            entry = Entry(
                label=e.get("label", ""), score=int(e.get("score", 0) or 0),
                reason=e.get("reason", ""), composite=e.get("composite", ""),
                first_seen=e.get("first_seen", ""), last_scored=e.get("last_scored", ""),
                written=bool(e.get("written", False)), match=e.get("match", {}) or {},
            )
            st.jobs[key] = entry
            if entry.composite:
                st._by_composite[entry.composite] = key
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "resume_sha256": self.resume_hash,
            "updated": date.today().isoformat(),
            "jobs": {k: asdict(v) for k, v in sorted(self.jobs.items())},
        }
        self.path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ #
    def _find(self, job: JobPosting) -> Entry | None:
        e = self.jobs.get(job.primary_key)
        if e is not None:
            return e
        pk = self._by_composite.get(job.composite_key)
        return self.jobs.get(pk) if pk else None

    def status(self, job: JobPosting, rescore_after_days: int, today: date) -> str:
        e = self._find(job)
        if e is None:
            return "score"
        if e.written:
            return "skip"
        if self.resume_changed:
            return "score"
        if rescore_after_days and _age_days(e.last_scored, today) > rescore_after_days:
            return "score"
        return "cached"

    def cached_result(self, job: JobPosting) -> MatchResult:
        e = self._find(job)
        return e.to_result() if e else MatchResult(label="Low Match", reason="(cache miss)")

    def record(self, job: JobPosting, mr: MatchResult, today: date) -> None:
        key = job.primary_key
        prev = self.jobs.get(key)
        entry = Entry(
            label=mr.label, score=mr.quality_score, reason=mr.reason,
            composite=job.composite_key,
            first_seen=prev.first_seen if prev else today.isoformat(),
            last_scored=today.isoformat(),
            written=prev.written if prev else False,
            match=asdict(mr),
        )
        self.jobs[key] = entry
        if entry.composite:
            self._by_composite[entry.composite] = key

    def mark_written(self, primary_key: str) -> None:
        e = self.jobs.get(primary_key)
        if e is not None:
            e.written = True

    def written_keys(self) -> set[str]:
        out: set[str] = set()
        for key, e in self.jobs.items():
            if e.written:
                out.add(key)
                if e.composite:
                    out.add(e.composite)
        return out
