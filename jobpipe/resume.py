"""Read the resume file to plain text and work out total years of experience."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import Config


class ResumeError(RuntimeError):
    pass


def extract_text(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise ResumeError(f"resume file not found: {p}")
    suffix = p.suffix.lower()

    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError as exc:  # pragma: no cover
            raise ResumeError("pdfplumber is not installed (pip install -r requirements.txt)") from exc
        parts: list[str] = []
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)

    if suffix in (".docx",):
        try:
            import docx
        except ImportError as exc:  # pragma: no cover
            raise ResumeError("python-docx is not installed (pip install -r requirements.txt)") from exc
        document = docx.Document(str(p))
        return "\n".join(para.text for para in document.paragraphs)

    if suffix in (".txt", ".md", ".text"):
        return p.read_text(encoding="utf-8", errors="ignore")

    raise ResumeError(f"unsupported resume format: {suffix} (use .pdf, .docx, .txt or .md)")


_EXPLICIT_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*years?\s+(?:of\s+)?(?:relevant\s+|professional\s+|overall\s+|total\s+|work\s+|industry\s+)?experience",
    re.IGNORECASE,
)
_YEAR_TOKEN = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")


def derive_years_experience(text: str, *, today: date | None = None) -> int | None:
    """Best-effort: an explicit 'N years of experience' phrase wins; otherwise
    span from the earliest 19xx/20xx year mentioned to now."""
    today = today or date.today()

    explicit = [int(m.group(1)) for m in _EXPLICIT_YEARS.finditer(text)]
    if explicit:
        return max(explicit)

    years = [int(y) for y in _YEAR_TOKEN.findall(text)]
    years = [y for y in years if 1985 <= y <= today.year]
    if years:
        span = today.year - min(years)
        if 0 < span <= 45:
            return span
    return None


def resolve_my_years(cfg: Config, resume_text: str) -> tuple[int, str]:
    """Return (years, how-it-was-determined). Raises if 'auto' and undetectable."""
    configured = cfg.hard_filters.seniority.my_years_experience
    if not (isinstance(configured, str) and configured.lower() == "auto"):
        return int(configured), "config (hard_filters.seniority.my_years_experience)"

    derived = derive_years_experience(resume_text)
    if derived is None:
        raise ResumeError(
            "could not derive years of experience from the resume. "
            "Set hard_filters.seniority.my_years_experience to a number in config.yaml."
        )
    return derived, "auto-derived from resume"
