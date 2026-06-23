"""Tests for the iCIMS adapter (`icims.fetch`).

Stub-based, no network: `icims.requests` and `icims.time.sleep` are
monkeypatched per test. Mirrors the stub pattern in `tests/test_workday.py`.

The fixtures are synthetic payloads built to the real Jibe `/api/jobs` JSON
shape captured by the Phase 0 recon spike (see
specs/003-icims-adapter/research.md): a page is
``{"jobs": [{"data": {...}}, ...], "totalCount": N, "count": N}`` and the
adapter reads each job from ``jobs[].data``. Per the personal-data-privacy
rule, these are made-up postings, not raw captures of a real employer.

Fake-response interface (mirrors the `requests.Response` surface the adapter
uses):
    resp.json() -> dict
    resp.raise_for_status() -> None (or raises requests.HTTPError)

The fake `requests` stub exposes `.get(url, params=..., headers=..., timeout=...)`,
recording each call's args on a shared `calls` list for URL/page assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests as real_requests

from job_agent.adapters import icims
from job_agent.schema import Posting

TENANT = "examplecorp"
HOST = "careers.example.com"
SLUG = f"{TENANT}:{HOST}"


class _FakeResponse:
    """Stand-in for requests.Response with a canned JSON body and status."""

    def __init__(self, payload: object, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise real_requests.HTTPError("simulated HTTP error")


class _FakeRequests:
    """Stand-in for the `requests` module; records every GET made."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # Queued items may be a raw payload (convenience) or a _FakeResponse
        # (when a test needs to control status); both normalize to a
        # _FakeResponse, since real requests.get always returns one.
        self.get_responses: list = []

    @staticmethod
    def _as_response(item: object) -> _FakeResponse:
        return item if isinstance(item, _FakeResponse) else _FakeResponse(item)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self._as_response(self.get_responses.pop(0))


def _job(
    req_id: str = "11003",
    title: str = "Quantitative Developer",
    country_code: str = "US",
    description: str = "<p>Build low-latency trading systems.</p>",
    full_location: str = "Bala Cynwyd (Philadelphia Area), Pennsylvania",
    apply_url: str | None = None,
    posted_date: str = "2026-06-22T06:54:00+0000",
) -> dict:
    return {
        "data": {
            "req_id": req_id,
            "title": title,
            "country_code": country_code,
            "description": description,
            "full_location": full_location,
            "location_name": "ExampleCorp",
            "apply_url": apply_url or f"https://{HOST}/jobs/{req_id}/login",
            "posted_date": posted_date,
        }
    }


def _page(total: int, jobs: list[dict]) -> dict:
    return {"jobs": jobs, "totalCount": total, "count": total}


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    # Isolate company resolution from any real git-ignored companies.toml so
    # the default is a clean fallback-to-tenant.
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_requests(monkeypatch) -> _FakeRequests:
    fake = _FakeRequests()
    monkeypatch.setattr(icims, "requests", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(icims.time, "sleep", lambda *a, **k: calls.append(a))
    return calls


# --- slug splitting ------------------------------------------------------


def test_split_slug_tenant_host() -> None:
    assert icims._split_slug("sig:careers.sig.com") == ("sig", "careers.sig.com")


def test_split_slug_tenant_only_defaults_icims_host() -> None:
    assert icims._split_slug("acme") == ("acme", "acme.icims.com")


def test_split_slug_rejects_malformed_slug() -> None:
    with pytest.raises(ValueError):
        icims._split_slug("a:b:c")


# --- US1 T006: page parse -> Posting -------------------------------------


def test_fetch_parses_jobs_page_into_postings(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job()])]

    postings = icims.fetch(SLUG)

    assert len(postings) == 1
    p = postings[0]
    assert isinstance(p, Posting)
    assert p.source == "icims"
    assert p.company == TENANT  # no companies.toml -> fallback to tenant slug
    assert p.external_id == "11003"
    assert p.title == "Quantitative Developer"
    assert "Bala Cynwyd" in p.location
    assert "Build low-latency trading systems." in p.description
    assert p.url == f"https://{HOST}/jobs/11003/login"
    assert p.posted_at == "2026-06-22T06:54:00+0000"

    assert fake_requests.calls[0]["url"] == f"https://{HOST}/api/jobs"
    assert fake_requests.calls[0]["params"]["page"] == 1


# --- US1 T007: US-location filter ----------------------------------------


def test_us_filter_keeps_us_drops_nonus_retains_unparseable(fake_requests) -> None:
    jobs = [
        _job(req_id="1", country_code="US", title="US Role"),
        _job(req_id="2", country_code="AU", title="Sydney Role"),
        # Empty/missing country -> ambiguous -> retained (FR-004 keep-if-any).
        _job(req_id="3", country_code="", title="Ambiguous Role", full_location=""),
    ]
    fake_requests.get_responses = [_page(3, jobs)]

    postings = icims.fetch(SLUG)

    titles = {p.title for p in postings}
    assert "US Role" in titles
    assert "Ambiguous Role" in titles  # retained, not dropped
    assert "Sydney Role" not in titles  # positively non-US -> dropped
    assert len(postings) == 2


# --- US1 T008: stable dedupe across runs ---------------------------------


def test_dedupe_fingerprint_stable_across_runs(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job(req_id="42")])]
    first = icims.fetch(SLUG)
    fake_requests.get_responses = [_page(1, [_job(req_id="42")])]
    second = icims.fetch(SLUG)

    assert first[0].external_id == "42"  # numeric job id carried through
    assert first[0].fingerprint == second[0].fingerprint


# --- US1 T009: company display-name resolution ---------------------------


def test_company_resolves_display_name_from_companies_toml(data_dir: Path) -> None:
    (data_dir / "companies.toml").write_text('[display_names]\nexamplecorp = "Example Corp"\n')
    assert icims._resolve_company("examplecorp") == "Example Corp"


def test_company_falls_back_to_tenant_when_unmapped_or_file_missing(data_dir: Path) -> None:
    # No companies.toml written at all -> fallback to the slug.
    assert icims._resolve_company("examplecorp") == "examplecorp"

    (data_dir / "companies.toml").write_text('[display_names]\nexamplecorp = "Example Corp"\n')
    assert icims._resolve_company("some_other_tenant") == "some_other_tenant"
