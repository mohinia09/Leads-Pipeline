"""Processed-store: status transitions, resume-change invalidation, rescore age."""

from __future__ import annotations

from datetime import date, timedelta

from jobpipe.models import JobPosting, MatchResult
from jobpipe.store import ProcessedStore, resume_fingerprint


def job(i: int) -> JobPosting:
    return JobPosting(source="jobspipe", source_id=str(i), title=f"Solutions Architect {i}",
                      company="Acme", url=f"u{i}", location="Bengaluru")


def result(label="Good Match", score=80) -> MatchResult:
    return MatchResult(label=label, reason="r", quality_score=score)


def test_new_job_scores(tmp_path):
    st = ProcessedStore.load(tmp_path / "s.json", "hash1")
    assert st.status(job(1), rescore_after_days=7, today=date(2026, 9, 1)) == "score"


def test_scored_then_cached_then_skip(tmp_path):
    p = tmp_path / "s.json"
    st = ProcessedStore.load(p, "hash1")
    j = job(1)
    st.record(j, result(), date(2026, 9, 1))
    st.save()

    st2 = ProcessedStore.load(p, "hash1")
    assert st2.status(j, 7, date(2026, 9, 2)) == "cached"        # scored, not written
    assert st2.cached_result(j).label == "Good Match"

    st2.mark_written(j.primary_key)
    st2.save()
    st3 = ProcessedStore.load(p, "hash1")
    assert st3.status(j, 7, date(2026, 9, 2)) == "skip"          # now written


def test_resume_change_forces_rescore(tmp_path):
    p = tmp_path / "s.json"
    st = ProcessedStore.load(p, "hashA")
    j = job(1)
    st.record(j, result(), date(2026, 9, 1))
    st.save()

    st2 = ProcessedStore.load(p, "hashB")        # different resume hash
    assert st2.resume_changed is True
    assert st2.status(j, 7, date(2026, 9, 2)) == "score"


def test_rescore_after_days(tmp_path):
    p = tmp_path / "s.json"
    st = ProcessedStore.load(p, "h")
    j = job(1)
    st.record(j, result(), date(2026, 9, 1))
    st.save()

    st2 = ProcessedStore.load(p, "h")
    assert st2.status(j, 7, date(2026, 9, 6)) == "cached"        # 5 days old
    assert st2.status(j, 7, date(2026, 9, 10)) == "score"        # 9 days old
    assert st2.status(j, 0, date(2027, 1, 1)) == "cached"        # 0 = never rescore


def test_composite_match(tmp_path):
    p = tmp_path / "s.json"
    st = ProcessedStore.load(p, "h")
    j = job(1)
    st.record(j, result(), date(2026, 9, 1))
    st.mark_written(j.primary_key)
    st.save()

    st2 = ProcessedStore.load(p, "h")
    reposted = JobPosting(source="jobspipe", source_id="999",   # new id, same company/title/loc
                          title=j.title, company="Acme", url="uX", location="Bengaluru")
    assert st2.status(reposted, 7, date(2026, 9, 2)) == "skip"


def test_fingerprint_ignores_whitespace():
    assert resume_fingerprint("a b   c\n\n") == resume_fingerprint("A  B c")
