"""Straight-line orchestration:

    search JobsPipe  ->  tag role tier  ->  hard filters  ->  AI assess
    ->  drop 'Not Viable'  ->  sort + cap  ->  append to Google Sheet

`run_fetch` stops after the search and just prints what came back.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, fields
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .filters import parse_years_requirement, run_hard_filters
from .jobspipe import JobsPipeError, _map_job, search_jobs
from .models import JobPosting, MatchResult
from .resume import ResumeError, extract_text, resolve_my_years
from .store import ProcessedStore, resume_fingerprint

_LABEL_RANK = {"Good Match": 0, "Low Match": 1, "Not Viable": 2}
_FETCH_CACHE = "data/last_fetch.json"
_SCORED_CACHE = "data/last_scored.json"


def _cache_file(cfg: Config, name: str) -> Path:
    return cfg.path.parent / name


def _save_fetch(cfg: Config, jobs: list[JobPosting]) -> None:
    _cache_file(cfg, _FETCH_CACHE).write_text(
        json.dumps([j.raw for j in jobs], indent=1, default=str), encoding="utf-8"
    )


def _load_fetch(cfg: Config) -> list[JobPosting]:
    path = _cache_file(cfg, _FETCH_CACHE)
    if not path.exists():
        raise FileNotFoundError(f"no cached fetch at {path} - run once without --use-cached-fetch first")
    return [_map_job(r) for r in json.loads(path.read_text(encoding="utf-8"))]


def _save_scored(cfg: Config, pairs: list[tuple[JobPosting, MatchResult]]) -> None:
    _cache_file(cfg, _SCORED_CACHE).write_text(
        json.dumps([{"raw": j.raw, "match": asdict(m)} for j, m in pairs], indent=1, default=str),
        encoding="utf-8",
    )


def _load_scored(cfg: Config) -> list[tuple[JobPosting, MatchResult]]:
    path = _cache_file(cfg, _SCORED_CACHE)
    if not path.exists():
        raise FileNotFoundError(f"no cached scores at {path} - run once without --use-cached-scores first")
    valid = {f.name for f in fields(MatchResult)}
    out: list[tuple[JobPosting, MatchResult]] = []
    for row in json.loads(path.read_text(encoding="utf-8")):
        mr = MatchResult(**{k: v for k, v in row["match"].items() if k in valid})
        out.append((_map_job(row["raw"]), mr))
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tag_role_tier(job: JobPosting, cfg: Config) -> None:
    title = (job.title or "").lower()
    matched = [t for t in cfg.roles.all_terms if t.lower() in title]
    job.matched_role_terms = matched
    for tier in ("primary", "strong_adjacent", "exploratory"):
        if any(t in getattr(cfg.roles, tier) for t in matched):
            job.matched_tier = tier
            break


def _resume_path(cfg: Config) -> Path:
    p = Path(cfg.resume_path)
    return p if p.is_absolute() else cfg.path.parent / p


def _years_req_str(job: JobPosting) -> str:
    req = parse_years_requirement(f"{job.title}\n{job.description}")
    if req is None:
        return ""
    lo, hi = req
    return f"{int(lo)}+" if hi == float("inf") else (f"{int(lo)}" if lo == hi else f"{int(lo)}-{int(hi)}")


def _log_path(cfg: Config) -> Path:
    d = cfg.path.parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"run-{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def _write_log(cfg: Config, records: list[dict], summary: dict) -> None:
    path = _log_path(cfg)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "summary": summary}) + "\n")
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _print_table(pairs: list[tuple[JobPosting, MatchResult | None]]) -> None:
    for i, (job, mr) in enumerate(pairs, 1):
        tag = f"{mr.label} {mr.quality_score}" if mr else ""
        loc = job.location or job.short_location or "?"
        print(f"  {i:>2}. {tag:<15} {job.title[:48]:<48} | {job.company[:26]:<26} | {loc[:24]:<24} | {job.url}")


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def run_fetch(cfg: Config, limit: int | None = None) -> int:
    print(f"searching JobsPipe ({cfg.jobspipe.mode}) for {len(cfg.roles.all_terms)} role terms ...")
    try:
        jobs = search_jobs(cfg)
    except JobsPipeError as exc:
        print(f"jobspipe error: {exc}")
        return 1
    for job in jobs:
        _tag_role_tier(job, cfg)
    print(f"got {len(jobs)} unique job(s)\n")
    shown = jobs[:limit] if limit else jobs
    _print_table([(j, None) for j in shown])
    if limit and len(jobs) > limit:
        print(f"  ... {len(jobs) - limit} more not shown")
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run_pipeline(
    cfg: Config,
    dry_run: bool = False,
    use_cached_fetch: bool = False,
    use_cached_scores: bool = False,
    no_ai: bool = False,
) -> int:
    tags = [t for t, on in (("dry-run", dry_run), ("cached-fetch", use_cached_fetch),
                            ("cached-scores", use_cached_scores), ("no-ai", no_ai)) if on]
    print(f"=== job-search pipeline{('  [' + ', '.join(tags) + ']') if tags else ''} ===")

    # Fast path: reuse the last run's scored jobs, go straight to cap + append.
    if use_cached_scores:
        try:
            pairs = _load_scored(cfg)
        except FileNotFoundError as exc:
            print(f"      {exc}")
            return 1
        for j, _ in pairs:
            _tag_role_tier(j, cfg)
        print(f"[*] loaded {len(pairs)} scored job(s) from cache (0 JobsPipe, 0 Claude calls)")
        return _finish(cfg, pairs, jobs=[j for j, _ in pairs], records=[], my_years=0,
                       dry_run=dry_run, store=None)

    # 1. search (or reuse cached fetch) --------------------------------------
    if use_cached_fetch:
        try:
            jobs = _load_fetch(cfg)
        except FileNotFoundError as exc:
            print(f"      {exc}")
            return 1
        print(f"[1/5] using cached fetch: {len(jobs)} job(s) (0 JobsPipe calls)")
    else:
        print(f"[1/5] search JobsPipe ({cfg.jobspipe.mode}) ...")
        try:
            jobs = search_jobs(cfg, log=lambda m: print(m))
        except JobsPipeError as exc:
            print(f"      jobspipe error: {exc}")
            return 1
        _save_fetch(cfg, jobs)
        print(f"      {len(jobs)} unique job(s)  (cached to {_FETCH_CACHE})")
    for job in jobs:
        _tag_role_tier(job, cfg)

    # 2. resume + years ----------------------------------------------------------
    print("[2/5] read resume ...")
    try:
        resume_text = extract_text(_resume_path(cfg))
        my_years, how = resolve_my_years(cfg, resume_text)
    except ResumeError as exc:
        print(f"      resume error: {exc}")
        return 1
    print(f"      years of experience = {my_years} ({how})")

    # 3. hard filters ----------------------------------------------------------
    print("[3/5] hard filters ...")
    survivors: list[JobPosting] = []
    fail_reasons: Counter[str] = Counter()
    records: list[dict] = []
    for job in jobs:
        outcome = run_hard_filters(job, cfg, my_years)
        rec = {
            "title": job.title, "company": job.company, "url": job.url,
            "hard_pass": outcome.passed, "first_fail": outcome.first_fail,
            "rules": [(r.rule, r.passed, r.detail) for r in outcome.results],
        }
        if outcome.passed:
            survivors.append(job)
        else:
            fail_reasons[outcome.first_fail.split(":")[0]] += 1
        records.append(rec)
    for rule, n in fail_reasons.most_common():
        print(f"      rejected {n:>3}  ({rule})")
    print(f"      {len(survivors)} job(s) passed all hard filters")

    # 3b. dedup against the processed store --------------------------------
    today = date.today()
    store_path = cfg.path.parent / cfg.output.dedup.processed_store
    store = ProcessedStore.load(store_path, resume_fingerprint(resume_text))
    if store.resume_changed:
        print("      resume changed since last run - cached scores invalidated, re-scoring")

    rad = cfg.output.dedup.rescore_after_days
    to_score: list[JobPosting] = []
    cached_pairs: list[tuple[JobPosting, MatchResult]] = []
    n_skip = 0
    for job in survivors:
        st = store.status(job, rad, today)
        if st == "skip":
            n_skip += 1
        elif st == "cached":
            cached_pairs.append((job, store.cached_result(job)))
        else:
            to_score.append(job)
    print(f"      dedup: {n_skip} already in sheet, {len(cached_pairs)} reused from store, "
          f"{len(to_score)} new -> AI")

    # 4. AI matching (new jobs only) -------------------------------------
    run_ai = to_score and cfg.ai_matching.enabled and cfg.secrets.anthropic_api_key and not no_ai
    ai_stats: dict = {}
    if run_ai:
        print(f"[4/5] AI matching ({cfg.ai_matching.model}) on {len(to_score)} new job(s) ...")
        from .matching import assess_all
        inr = float(cfg.hard_filters.salary.inr_per_unit.get("USD", 88.0))
        fresh, ai_stats = assess_all(to_score, cfg, resume_text, my_years,
                                     log=lambda m: print(m), inr_per_usd=inr)
    elif to_score:
        why = ("--no-ai" if no_ai else
               "ai_matching.enabled=false" if not cfg.ai_matching.enabled else
               "ANTHROPIC_API_KEY not set")
        print(f"[4/5] AI matching skipped ({why}) - new jobs labelled 'Low Match' score 0")
        fresh = [(j, MatchResult(label="Low Match", reason=f"AI skipped: {why}", quality_score=0))
                 for j in to_score]
    else:
        print("[4/5] AI matching: no new jobs to score")
        fresh = []

    for j, m in fresh:
        store.record(j, m, today)
    if fresh or cached_pairs:
        _save_scored(cfg, fresh + cached_pairs)

    return _finish(cfg, fresh + cached_pairs, jobs=jobs, records=records,
                   my_years=my_years, dry_run=dry_run, store=store, ai_stats=ai_stats)


def _finish(cfg, pool, *, jobs, records, my_years, dry_run, store=None, ai_stats=None) -> int:
    """drop 'Not Viable' -> sort -> cap -> append -> update store -> run log."""
    pairs = list(pool)
    n_not_viable = sum(1 for _, m in pairs if m.label == "Not Viable")
    if cfg.ai_matching.drop_not_viable:
        pairs = [(j, m) for j, m in pairs if m.label != "Not Viable"]
        if n_not_viable:
            print(f"      set aside {n_not_viable} 'Not Viable' (run log only, not written)")
    if not cfg.ai_matching.include_low_match:
        pairs = [(j, m) for j, m in pairs if m.label != "Low Match"]

    pairs.sort(key=lambda pm: (_LABEL_RANK.get(pm[1].label, 9), -pm[1].quality_score))
    capped = pairs[: cfg.run.daily_cap]
    over = len(pairs) - len(capped)
    print(f"[5/5] {len(capped)} job(s) to write (cap {cfg.run.daily_cap})"
          + (f"; {over} viable held for next run" if over else ""))
    _print_table(capped)

    years_reqs = {j.primary_key: _years_req_str(j) for j, _ in capped}
    blocked = store.written_keys() if store else set()
    written_keys: list[str] = []
    skipped = 0
    from .sheets import SheetsError, append_results
    try:
        written_keys, skipped = append_results(
            cfg, capped, years_reqs, blocked_keys=blocked, dry_run=dry_run, log=lambda m: print(m)
        )
    except SheetsError as exc:
        print(f"      sheet step: {exc}")

    if store is not None:
        if not dry_run:
            for pk in written_keys:
                store.mark_written(pk)
        store.save()
        print(f"      processed store: {len(store.jobs)} job(s) tracked"
              f"{' (dry-run: scores saved, writes not marked)' if dry_run else ''}")

    in_sheet = set(written_keys)
    match_by_key = {j.primary_key: m for j, m in pool}
    aug: list[dict] = []
    for job, base in zip(jobs, records or [{} for _ in jobs]):
        mr = match_by_key.get(job.primary_key)
        aug.append({**base,
                    "ai_label": mr.label if mr else None,
                    "ai_score": mr.quality_score if mr else None,
                    "ai_reason": mr.reason if mr else None,
                    "written_now": job.primary_key in in_sheet})
    summary = {
        "mode": cfg.jobspipe.mode, "dry_run": dry_run, "fetched": len(jobs),
        "in_pool": len(pool), "to_write": len(capped), "held_for_next_run": over,
        "written": len(written_keys), "skipped_known": skipped, "my_years": my_years,
        "ai": ai_stats or {},
    }
    _write_log(cfg, aug, summary)
    print(f"      run log -> {_log_path(cfg).name}")
    return 0
