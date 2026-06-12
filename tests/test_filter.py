"""Pure unit tests for the deterministic pre-LLM relevance gate.

No stubbing, no network, no DB: `classify()` is a pure function over plain
posting dicts and a `Criteria` value object, per
specs/002-scoring-spend-efficiency/contracts/filter-criteria.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_agent import filter as job_filter

CRITERIA = job_filter.Criteria(
    denylist_title_keywords=("sales", "accountant", "recruiter"),
    allowlist_title_keywords=("engineer", "engineering", "software", "developer"),
    age_max_days=30,
    location_remote_ok=True,
    location_regions=("WA", "OR", "PA"),
    location_metros=("Remote", "Bay Area"),
)


def _posting(**overrides: object) -> dict:
    """Build a minimal posting dict with sensible plausible defaults."""
    base = {
        "title": "Software Engineer",
        "location": "Remote",
        "posted_at": None,
    }
    base.update(overrides)
    return base


# --- Function denylist (hard reject) ---------------------------------------


def test_denylist_title_keyword_rejects_case_insensitively() -> None:
    posting = _posting(title="Senior Sales Engineer")
    reason = job_filter.classify(posting, CRITERIA)
    assert reason is not None
    assert reason.startswith("function_denylist:")
    assert "sales" in reason.lower()


def test_denylist_match_is_case_insensitive_on_title_case() -> None:
    posting = _posting(title="SALES Development Representative")
    reason = job_filter.classify(posting, CRITERIA)
    assert reason is not None
    assert reason.startswith("function_denylist:")


# --- Allowlist (advisory only, never rejects alone) -------------------------


def test_title_off_allowlist_and_not_denylisted_is_not_rejected() -> None:
    posting = _posting(title="Operations Lead", location="Remote")
    assert job_filter.classify(posting, CRITERIA) is None


def test_allowlisted_title_passes() -> None:
    posting = _posting(title="Senior Software Engineer", location="Remote")
    assert job_filter.classify(posting, CRITERIA) is None


# --- Posting-age gate --------------------------------------------------------


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_age_gate_rejects_posting_older_than_max_days() -> None:
    posting = _posting(title="Software Engineer", location="Remote", posted_at=_iso_days_ago(47))
    reason = job_filter.classify(posting, CRITERIA)
    assert reason is not None
    assert reason.startswith("age:")
    assert reason.endswith("d")


def test_age_gate_keeps_posting_within_max_days() -> None:
    posting = _posting(title="Software Engineer", location="Remote", posted_at=_iso_days_ago(5))
    assert job_filter.classify(posting, CRITERIA) is None


def test_age_gate_fails_open_on_missing_posted_at() -> None:
    posting = _posting(title="Software Engineer", location="Remote", posted_at=None)
    assert job_filter.classify(posting, CRITERIA) is None


def test_age_gate_fails_open_on_unparseable_posted_at() -> None:
    posting = _posting(
        title="Software Engineer", location="Remote", posted_at="not-a-real-date"
    )
    assert job_filter.classify(posting, CRITERIA) is None


# --- Location gate (region-token semantics) ---------------------------------


def test_location_in_region_suburb_renton_wa_is_kept() -> None:
    posting = _posting(title="Software Engineer", location="Renton, WA")
    assert job_filter.classify(posting, CRITERIA) is None


def test_location_in_region_suburb_king_of_prussia_pa_is_kept() -> None:
    posting = _posting(title="Software Engineer", location="King of Prussia, PA")
    assert job_filter.classify(posting, CRITERIA) is None


def test_location_with_trailing_country_token_seattle_wa_usa_is_kept() -> None:
    posting = _posting(title="Software Engineer", location="Seattle, WA, USA")
    assert job_filter.classify(posting, CRITERIA) is None


def test_location_out_of_region_austin_tx_is_rejected() -> None:
    posting = _posting(title="Software Engineer", location="Austin, TX")
    reason = job_filter.classify(posting, CRITERIA)
    assert reason is not None
    assert reason.startswith("location:")
    assert "Austin, TX" in reason


def test_location_washington_dc_decoy_is_rejected_not_substring_matched() -> None:
    """`regions` matches the comma TOKEN ('DC'), never a raw substring of
    'WA' against 'Washington, DC' -- the key region-token assertion."""
    posting = _posting(title="Software Engineer", location="Washington, DC")
    reason = job_filter.classify(posting, CRITERIA)
    assert reason is not None
    assert reason.startswith("location:")
    assert "Washington, DC" in reason


def test_location_remote_is_kept_when_remote_ok() -> None:
    posting = _posting(title="Software Engineer", location="Remote")
    assert job_filter.classify(posting, CRITERIA) is None


def test_location_missing_fails_open() -> None:
    posting = _posting(title="Software Engineer", location="")
    assert job_filter.classify(posting, CRITERIA) is None


def test_location_unspecified_default_fails_open() -> None:
    posting = _posting(title="Software Engineer", location="Unspecified")
    assert job_filter.classify(posting, CRITERIA) is None


# --- load_criteria ------------------------------------------------------------


VALID_TOML = """
[denylist]
title_keywords = ["sales", "accountant", "recruiter"]

[allowlist]
title_keywords = ["engineer", "engineering", "software"]

[age]
max_days = 30

[location]
remote_ok = true
regions = ["WA", "OR", "PA"]
metros = ["Remote", "Bay Area"]
"""


def test_load_criteria_round_trips_valid_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "filter.toml"
    toml_path.write_text(VALID_TOML)

    criteria = job_filter.load_criteria(str(toml_path))

    assert criteria.denylist_title_keywords == ("sales", "accountant", "recruiter")
    assert criteria.allowlist_title_keywords == ("engineer", "engineering", "software")
    assert criteria.age_max_days == 30
    assert criteria.location_remote_ok is True
    assert criteria.location_regions == ("WA", "OR", "PA")
    assert criteria.location_metros == ("Remote", "Bay Area")


def test_load_criteria_raises_on_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.toml"

    with pytest.raises(Exception):
        job_filter.load_criteria(str(missing_path))


def test_load_criteria_raises_on_non_int_max_days(tmp_path: Path) -> None:
    toml_path = tmp_path / "filter.toml"
    toml_path.write_text(
        """
[denylist]
title_keywords = ["sales"]

[allowlist]
title_keywords = []

[age]
max_days = "thirty"

[location]
remote_ok = true
regions = []
metros = []
"""
    )

    with pytest.raises(Exception):
        job_filter.load_criteria(str(toml_path))


def test_load_criteria_raises_on_non_list_keyword_field(tmp_path: Path) -> None:
    toml_path = tmp_path / "filter.toml"
    toml_path.write_text(
        """
[denylist]
title_keywords = "sales"

[allowlist]
title_keywords = []

[age]
max_days = 30

[location]
remote_ok = true
regions = []
metros = []
"""
    )

    with pytest.raises(Exception):
        job_filter.load_criteria(str(toml_path))
