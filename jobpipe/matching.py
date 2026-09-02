"""AI matching. Runs only on jobs that passed every hard filter.

Asks Claude to judge role relevance, technical/experience fit, seniority
alignment (secondary signal - the hard filter already gated on years), a short
company read, and an overall label + reason. Output is JSON, parsed into
MatchResult. The AI cannot resurrect a hard-filter failure - it never sees
those jobs.
"""

from __future__ import annotations

import json
import re

from .config import Config
from .models import JobPosting, MatchResult

_LABELS = ("Good Match", "Low Match", "Not Viable")

# Section headings that mark non-essential JD text. Matched case-insensitively
# against the first line of a blank-line-separated block; the block is dropped
# only if it also contains none of _TRIGGERS.
_DEFAULT_JD_BOILERPLATE = (
    "about us", "about the company", "about our company", "about the team",
    "who we are", "our company", "our team", "our mission", "our vision",
    "our values", "our culture", "life at", "why join", "why work",
    "what we offer", "what you'll get", "what's in it for you", "whats in it for you",
    "benefits", "perks", "perks and benefits", "compensation and benefits",
    "our benefits", "benefits and perks", "the benefits",
    "equal opportunity", "equal employment", "we are an equal", "eeo",
    "diversity", "diversity and inclusion", "diversity, equity",
    "accommodation", "reasonable accommodation",
    "how to apply", "to apply", "application process", "hiring process",
    "recruitment process", "interview process", "what to expect",
    "disclaimer", "privacy notice", "privacy policy", "e-verify", "background check",
)
_TRIGGERS = (
    "year", "experience", "require", "responsib", "qualif", "you have", "you'll",
    "you will", "must have", "skill", "proficien", "degree", "bachelor",
    "expertise", "familiar", "knowledge of",
)
_ABOUT_ROLE_OK = {
    "about the role", "about this role", "about the job", "about this job",
    "about the position", "about this position", "about the opportunity",
}
# unheaded benefit lists (common on Indeed): drop a block when several of its
# lines are just these, and it carries no requirement triggers.
_BENEFIT_LINE = (
    "insurance", "reimbursement", "paid time off", "pto", "401(k)", "401k",
    "parental leave", "flexible schedule", "flexible hours", "stock option",
    "equity", "rsu", "gym", "wellness", "commuter", "relocation",
    "provident fund", "gratuity", "work from home", "food provided",
    "cell phone reimbursement", "health savings account",
)


class MatchingError(RuntimeError):
    pass


def clean_jd(text: str, *, extra_markers=(), cap: int, strip: bool) -> str:
    """Optionally drop boilerplate sections, then cap length keeping head + tail."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    if strip and text.strip():
        markers = tuple(m.lower() for m in (*_DEFAULT_JD_BOILERPLATE, *extra_markers))
        blocks = re.split(r"\n\s*\n", text)
        kept: list[str] = []
        skip_next = False
        for blk in blocks:
            if skip_next:
                skip_next = False
                continue
            first = blk.strip().splitlines()[0].strip().lower() if blk.strip() else ""
            head = first.lstrip("#*-• ").rstrip(":").strip()
            is_boiler = any(head == m or head.startswith(m + " ") or head.startswith(m) for m in markers)
            if head.startswith("about ") and head not in _ABOUT_ROLE_OK:
                is_boiler = True  # "About <Company>" blurb
            blk_lines = [ln.strip().lstrip("-*•").strip().lower() for ln in blk.splitlines() if ln.strip()]
            if len(blk_lines) >= 3 and sum(
                any(b in ln for b in _BENEFIT_LINE) for ln in blk_lines
            ) >= max(3, len(blk_lines) - 1):
                is_boiler = True  # unheaded benefits list
            if is_boiler and not any(t in blk.lower() for t in _TRIGGERS):
                if len(blk.strip().splitlines()) <= 1:   # bare heading -> also drop the next block
                    skip_next = True
                continue
            kept.append(blk)
        candidate = "\n\n".join(kept).strip()
        # keep the stripped version only if it still holds the requirement text
        # (and isn't suspiciously tiny)
        orig_has_trigger = any(t in text.lower() for t in _TRIGGERS)
        cand_has_trigger = any(t in candidate.lower() for t in _TRIGGERS)
        if candidate and (cand_has_trigger or not orig_has_trigger) and len(candidate) >= 0.15 * len(text):
            text = candidate

    text = text.strip()
    if len(text) <= cap:
        return text
    head = int(cap * 0.8)
    tail = max(0, cap - head - 20)
    return text[:head].rstrip() + "\n[...trimmed...]\n" + (text[-tail:].lstrip() if tail else "")


_SYSTEM = (
    "You assess how well a single job fits ONE candidate. Be strict and concise. "
    "Consider: (1) role-content relevance to the candidate's background, "
    "(2) technical and experience fit, (3) seniority alignment, "
    "(4) a brief company read (what the company does, stage/size if inferable), "
    "(5) an overall verdict.\n"
    'Reply with ONLY a JSON object, no prose, no code fence:\n'
    '{"label": "Good Match" | "Low Match" | "Not Viable", '
    '"role_relevance": "High" | "Medium" | "Low", '
    '"technical_fit": "<=120 chars", '
    '"seniority_fit": "<=120 chars", '
    '"company_note": "<=160 chars", '
    '"quality_score": <integer 0-100>, '
    '"reason": "<=240 chars, why this label"}\n'
    "Use 'Not Viable' only when the role is genuinely off-target for this candidate. "
    "Use 'Low Match' for weak-but-plausible fits. Do not inflate scores."
)


def _client(cfg: Config):
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise MatchingError("anthropic is not installed (pip install -r requirements.txt)") from exc
    if not cfg.secrets.anthropic_api_key:
        raise MatchingError("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic(api_key=cfg.secrets.anthropic_api_key)


def _candidate_block(resume_text: str, my_years: int, cfg: Config) -> str:
    roles = cfg.roles
    return (
        f"CANDIDATE\n"
        f"- Total years of experience: {my_years}\n"
        f"- Target roles (primary): {', '.join(roles.primary)}\n"
        f"- Target roles (adjacent): {', '.join(roles.strong_adjacent)}\n"
        f"- Target roles (exploratory): {', '.join(roles.exploratory)}\n"
        f"- Resume (full text):\n{resume_text[: cfg.ai_matching.resume_max_chars]}"
    )


def _job_block(job: JobPosting, cfg: Config) -> str:
    sal = job.salary_string or ""
    if not sal and (job.salary_min or job.salary_max):
        sal = f"{job.salary_min or '?'}-{job.salary_max or '?'} {job.salary_currency or ''}".strip()
    desc = clean_jd(
        job.description or "",
        extra_markers=cfg.ai_matching.jd_extra_boilerplate_markers,
        cap=cfg.ai_matching.description_max_chars,
        strip=cfg.ai_matching.strip_jd_boilerplate,
    )
    return (
        f"JOB\n"
        f"- Title: {job.title}\n"
        f"- Company: {job.company}\n"
        f"- Location: {job.location or job.short_location or '?'} "
        f"(country={job.country_code or '?'}, arrangement={job.work_arrangement or '?'}, remote={job.remote})\n"
        f"- Posted: {job.date_posted or '?'}  Status: {job.status or '?'}\n"
        f"- Salary: {sal or 'not disclosed'}\n"
        f"- Matched role tier: {job.matched_tier or '?'} ({', '.join(job.matched_role_terms) or 'n/a'})\n"
        f"- Description:\n{desc}"
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise MatchingError(f"could not parse AI response as JSON: {text[:200]}")


def _to_result(data: dict) -> MatchResult:
    label = str(data.get("label", "")).strip()
    if label not in _LABELS:
        low = label.lower()
        label = "Good Match" if "good" in low else "Not Viable" if "not" in low else "Low Match"
    try:
        score = int(round(float(data.get("quality_score", 0))))
    except (TypeError, ValueError):
        score = 0
    return MatchResult(
        label=label,
        reason=str(data.get("reason", "")).strip()[:240],
        relevance=str(data.get("role_relevance", "")).strip(),
        technical_fit=str(data.get("technical_fit", "")).strip()[:120],
        seniority_alignment=str(data.get("seniority_fit", "")).strip()[:120],
        overall_quality=str(data.get("role_relevance", "")).strip(),
        company_assessment=str(data.get("company_note", "")).strip()[:160],
        quality_score=max(0, min(100, score)),
    )


# USD per 1M tokens (input, output) - update if pricing changes
_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
}


def assess_job(client, job: JobPosting, cfg: Config, resume_text: str, my_years: int):
    """Returns (MatchResult, input_tokens, output_tokens)."""
    msg = client.messages.create(
        model=cfg.ai_matching.model,
        max_tokens=cfg.ai_matching.max_tokens,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"{_candidate_block(resume_text, my_years, cfg)}\n\n{_job_block(job, cfg)}",
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    u = getattr(msg, "usage", None)
    return _to_result(_extract_json(text)), getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0)


def assess_all(jobs, cfg: Config, resume_text: str, my_years: int, log=print, inr_per_usd: float = 88.0):
    """Returns (pairs, stats). pairs = list[(job, MatchResult)]. stats has token
    counts and estimated cost. A per-job error becomes a 'Low Match' rather than
    aborting the run."""
    client = _client(cfg)
    pairs: list[tuple[JobPosting, MatchResult]] = []
    in_tok = out_tok = calls = errors = 0
    for i, job in enumerate(jobs, 1):
        try:
            result, ti, to = assess_job(client, job, cfg, resume_text, my_years)
            in_tok += ti
            out_tok += to
            calls += 1
        except Exception as exc:  # noqa: BLE001 - keep the run alive
            result = MatchResult(label="Low Match", reason=f"AI assess failed: {exc}"[:240])
            errors += 1
        log(f"  [{i}/{len(jobs)}] {result.label:<10} {result.quality_score:>3}  {job.title} @ {job.company}")
        pairs.append((job, result))

    in_rate, out_rate = _PRICING.get(cfg.ai_matching.model, (1.0, 5.0))
    usd = in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
    stats = {
        "model": cfg.ai_matching.model, "calls": calls, "errors": errors,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "usd": round(usd, 5), "inr": round(usd * inr_per_usd, 2),
        "per_job_inr": round(usd * inr_per_usd / calls, 3) if calls else 0.0,
    }
    log(f"  AI: {calls} call(s), {in_tok} in + {out_tok} out tokens, "
        f"~${usd:.4f} ~Rs {usd * inr_per_usd:.2f}"
        + (f" ({errors} error(s))" if errors else ""))
    return pairs, stats
