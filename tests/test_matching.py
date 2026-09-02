"""Tests for JD text cleaning (no network / no API calls)."""

from __future__ import annotations

from jobpipe.matching import clean_jd

JD = """\
Senior Solutions Architect

About Acme Corp
Acme is a fast-growing leader in cloud widgets, founded in 2012, backed by top VCs.
We serve thousands of customers worldwide and are proud of our culture.

Responsibilities
- Own the reference architecture for enterprise deployments
- Requires 10+ years of experience in solutions architecture

What we offer
- Health insurance
- Flexible schedule
- Paid time off
- 401(k) matching

Equal Opportunity
Acme is an equal opportunity employer. We celebrate diversity.

How to apply
Send your resume to jobs@acme.example and we'll be in touch.
"""


def test_strips_boilerplate_keeps_requirements():
    out = clean_jd(JD, cap=5000, strip=True)
    assert "Own the reference architecture" in out
    assert "10+ years of experience" in out
    assert "About Acme Corp" not in out
    assert "equal opportunity employer" not in out
    assert "Send your resume to" not in out
    assert "Health insurance" not in out


def test_no_strip_when_disabled():
    out = clean_jd(JD, cap=5000, strip=False)
    assert "equal opportunity employer" in out


def test_cap_keeps_head_and_tail():
    text = "HEAD " + ("x " * 4000) + "TAILMARKER"
    out = clean_jd(text, cap=1000, strip=False)
    assert out.startswith("HEAD")
    assert "TAILMARKER" in out
    assert len(out) < 1100


def test_guard_reverts_when_strip_too_aggressive():
    # almost entirely a benefits block - stripping would gut it, so keep original
    text = "Benefits\n- Health insurance\n- Dental insurance\n- Vision insurance\n- 401(k)"
    out = clean_jd(text, cap=5000, strip=True)
    assert "Health insurance" in out
