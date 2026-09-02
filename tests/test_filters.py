"""Tests for the deterministic pieces: years parsing, resume year derivation,
and each hard-filter check."""

from __future__ import annotations

import math
from datetime import date

import pytest

from jobpipe.config import load_config
from jobpipe.filters import (
    check_geography,
    check_recency,
    check_salary,
    check_seniority,
    parse_years_requirement,
    run_hard_filters,
)
from jobpipe.models import JobPosting
from jobpipe.resume import derive_years_experience

CFG = load_config("config.yaml")
TODAY = date(2026, 9, 1)


def job(**kw) -> JobPosting:
    base = dict(source="jobspipe", source_id="x", title="Solutions Architect", company="C", url="u")
    base.update(kw)
    return JobPosting(**base)


# --- years parsing --------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        ("10-12 years of experience", (10, 12)),
        ("8 to 15 years", (8, 15)),
        ("minimum 12 years experience", (12, math.inf)),
        ("8+ years", (8, math.inf)),
        ("15 years of experience required", (15, 15)),
        ("great team, fast pace", None),
    ],
)
def test_parse_years_requirement(text, expected):
    assert parse_years_requirement(text) == expected


# --- seniority window (my_years = 13 -> [10, 17]) ------------------------- #
@pytest.mark.parametrize(
    "desc, should_pass",
    [
        ("10-12 years of experience", True),
        ("8-15 years of experience", True),
        ("14-17 years of experience", True),
        ("5-7 years of experience", False),   # too junior
        ("5-10 years of experience", False),  # tops out at the floor -> too junior
        ("20+ years of experience", False),   # too senior
        ("no explicit requirement here", True),  # no_requirement_action: pass
    ],
)
def test_check_seniority(desc, should_pass):
    r = check_seniority(job(description=desc), CFG, my_years=13)
    assert r.passed is should_pass


def test_seniority_source_label_blocks_junior():
    r = check_seniority(job(seniority_raw="entry", description="fun role"), CFG, my_years=13)
    assert r.passed is False


# --- salary -------------------------------------------------------------- #
def test_salary_inr_below_threshold_excluded():
    r = check_salary(job(salary_max=2_000_000, salary_currency="INR"), CFG)
    assert r.passed is False and "LPA" in r.detail


def test_salary_inr_above_threshold_ok():
    r = check_salary(job(salary_max=3_000_000, salary_currency="INR"), CFG)
    assert r.passed is True


def test_salary_usd_converted():
    r = check_salary(job(salary_max=120_000, salary_currency="USD"), CFG)
    assert r.passed is True  # 120k * 88 / 1e5 = 105.6 LPA


def test_salary_undisclosed_not_excluded():
    r = check_salary(job(), CFG)
    assert r.passed is True and "not disclosed" in r.detail


def test_salary_monthly_raw_string_annualised():
    r = check_salary(job(salary_string="INR 3,00,000 per month"), CFG)
    assert r.passed is True  # 3L * 12 = 36 LPA


# --- recency ----------------------------------------------------------- #
def test_recency_old_post_excluded():
    r = check_recency(job(date_posted=date(2026, 6, 1)), CFG, TODAY)
    assert r.passed is False


def test_recency_fresh_post_ok():
    r = check_recency(job(date_posted=date(2026, 8, 25)), CFG, TODAY)
    assert r.passed is True


def test_recency_closed_status_excluded():
    r = check_recency(job(status="closed", date_posted=date(2026, 8, 30)), CFG, TODAY)
    assert r.passed is False


# --- geography ------------------------------------------------------------ #
def test_geo_india_location_ok():
    r = check_geography(job(location="Bengaluru, India", country_code="IN"), CFG)
    assert r.passed is True


def test_geo_international_remote_rejected_when_disabled():
    # config now has allow_international_remote: false -> India-only
    r = check_geography(job(location="Remote - United States", country_code="US", remote=True), CFG)
    assert r.passed is CFG.geography.allow_international_remote


def test_geo_us_onsite_rejected():
    r = check_geography(job(location="Austin, TX", country_code="US", remote=False), CFG)
    assert r.passed is False


# --- resume derivation ------------------------------------------------- #
def test_derive_years_explicit_phrase():
    assert derive_years_experience("I have 13 years of experience in ML.") == 13


def test_derive_years_from_earliest_year():
    got = derive_years_experience("Worked since 2011 on stuff.", today=date(2026, 9, 1))
    assert got == 15


# --- full stack -------------------------------------------------------- #
def test_run_hard_filters_all_pass():
    good = job(
        location="Remote - India",
        country_code="IN",
        remote=True,
        date_posted=date(2026, 8, 28),
        description="We want 12-16 years of experience.",
        salary_max=4_000_000,
        salary_currency="INR",
    )
    outcome = run_hard_filters(good, CFG, my_years=13, today=TODAY)
    assert outcome.passed, outcome.first_fail
