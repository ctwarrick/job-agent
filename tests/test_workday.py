"""Red-phase tests for the Workday adapter (`workday.fetch`).

Stub-based, no network: `workday.requests` and `workday.time.sleep` are
monkeypatched per test. See docs/work/workday-adapter/plan.md for the
approved design this pins down.

Fake-response interface (mirrors the real `requests.Response` surface the
adapter is expected to use):
    resp.json() -> dict | list
    resp.raise_for_status() -> None (or raises requests.HTTPError)

The fake `requests` stub exposes `.post(url, json=..., headers=..., timeout=...)`
and `.get(url, headers=..., timeout=...)`, each recording its call args on a
shared `calls` list so URL/body/pagination assertions can inspect them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests as real_requests

from job_agent.adapters import workday
from job_agent.schema import Posting

TENANT = "chrobinson"
SITE = "CHRobinson"
HOST = "wd5"
SLUG = f"{TENANT}:{SITE}:{HOST}"


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
    """Stand-in for the `requests` module; records every call made."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # Queued items may be a raw payload (convenience) or a _FakeResponse
        # (when a test needs to control status); both are normalized to a
        # _FakeResponse, since real requests.post/get always return one.
        self.post_responses: list = []
        self.get_responses: list = []

    @staticmethod
    def _as_response(item: object) -> _FakeResponse:
        return item if isinstance(item, _FakeResponse) else _FakeResponse(item)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": "post", "url": url, "json": json, "timeout": timeout})
        return self._as_response(self.post_responses.pop(0))

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"method": "get", "url": url, "timeout": timeout})
        return self._as_response(self.get_responses.pop(0))


def _job_posting(
    external_path: str = "/job/Remote/Engineer_R-1",
    title: str = "Software Engineer",
    locations_text: str = "Seattle, WA",
    posted_on: str = "2026-06-01",
) -> dict:
    return {
        "externalPath": external_path,
        "title": title,
        "locationsText": locations_text,
        "postedOn": posted_on,
    }


def _jobs_page(total: int, postings: list[dict]) -> dict:
    return {"total": total, "jobPostings": postings}


def _detail_payload(description: str = "<p>Full job description.</p>") -> dict:
    return {"jobPostingInfo": {"jobDescription": description}}


@pytest.fixture
def fake_requests(monkeypatch) -> _FakeRequests:
    fake = _FakeRequests()
    monkeypatch.setattr(workday, "requests", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(workday.time, "sleep", lambda *a, **k: calls.append(a))
    return calls


# --- 1-2: slug splitting -----------------------------------------------


def test_split_slug_parses_tenant_site_host() -> None:
    assert workday._split_slug("chrobinson:CHRobinson:wd5") == (
        "chrobinson",
        "CHRobinson",
        "wd5",
    )


def test_split_slug_rejects_malformed_slug() -> None:
    with pytest.raises(ValueError):
        workday._split_slug("chrobinson:CHRobinson")


# --- 3: URL shape --------------------------------------------------------


def test_fetch_builds_correct_cxs_urls_from_slug(fake_requests) -> None:
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]
    fake_requests.get_responses = [_detail_payload()]

    workday.fetch(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}/jobs"
    )
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}"
        "/job/Remote/Engineer_R-1"
    )


# --- 4-5: pagination ------------------------------------------------------


def test_pagination_walks_offset_until_total_reached(fake_requests) -> None:
    # total=45, limit=20 -> pages at offset 0, 20, 40 (3 POSTs); 45 jobs total
    # so detail GETs == 45.
    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20, 40)])
    page3 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(40, 45)])
    fake_requests.post_responses = [page1, page2, page3]
    fake_requests.get_responses = [_detail_payload() for _ in range(45)]

    postings = workday.fetch(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    assert len(post_calls) == 3
    offsets = [c["json"]["offset"] for c in post_calls]
    assert offsets == [0, 20, 40]
    assert len(postings) == 45


def test_pagination_stops_at_empty_page(fake_requests) -> None:
    # total is overstated at 100, but the second page is empty -> must stop
    # after the empty page rather than trying to walk to offset 100+.
    page1 = _jobs_page(100, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(100, [])
    fake_requests.post_responses = [page1, page2]
    fake_requests.get_responses = [_detail_payload() for _ in range(20)]

    postings = workday.fetch(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    assert len(post_calls) == 2
    assert len(postings) == 20


# --- 6: field mapping through normalize() --------------------------------


def test_detail_fetch_maps_through_normalize(fake_requests, monkeypatch, tmp_path: Path) -> None:
    # No companies.toml at all -> company falls back to tenant.
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))

    fake_requests.post_responses = [
        _jobs_page(
            1,
            [
                _job_posting(
                    external_path="/job/Remote/Engineer_R-1",
                    title="Software Engineer",
                    locations_text="Seattle, WA",
                    posted_on="2026-06-01",
                )
            ],
        )
    ]
    fake_requests.get_responses = [_detail_payload("<p>Full job description.</p>")]

    postings = workday.fetch(SLUG)

    assert len(postings) == 1
    posting = postings[0]
    assert isinstance(posting, Posting)
    assert posting.source == "workday"
    assert posting.company == TENANT
    assert posting.external_id  # present, non-empty
    assert posting.title == "Software Engineer"
    assert posting.location == "Seattle, WA"
    assert "Full job description." in posting.description
    assert posting.url.startswith("https://")
    assert posting.posted_at == "2026-06-01"


# --- 7-8: per-employer cap -----------------------------------------------


def test_per_employer_cap_limits_results(fake_requests, monkeypatch) -> None:
    # Cap = 5, advertised total = 45 (limit 20/page). The cap must bound
    # round-trips: with a 20-per-page POST size, reaching 5 postings happens
    # within the FIRST page, so only 1 jobs-POST and exactly 5 detail-GETs
    # should occur -- not a full 20-job page worth of GETs.
    monkeypatch.setenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", "5")

    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    fake_requests.post_responses = [page1]
    fake_requests.get_responses = [_detail_payload() for _ in range(5)]

    postings = workday.fetch(SLUG)

    assert len(postings) == 5
    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 1
    assert len(get_calls) == 5


def test_cap_unset_returns_all(fake_requests, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", raising=False)

    page1 = _jobs_page(7, [_job_posting(external_path=f"/job/{i}") for i in range(7)])
    fake_requests.post_responses = [page1]
    fake_requests.get_responses = [_detail_payload() for _ in range(7)]

    postings = workday.fetch(SLUG)

    assert len(postings) == 7


# --- 9: politeness --------------------------------------------------------


def test_politeness_sleep_called(fake_requests, fake_sleep) -> None:
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]
    fake_requests.get_responses = [_detail_payload()]

    workday.fetch(SLUG)

    assert len(fake_sleep) >= 1


# --- 10: HTTP error propagation -------------------------------------------


def test_fetch_raises_on_http_error_so_run_records_failure(fake_requests) -> None:
    fake_requests.post_responses = [_FakeResponse({}, status_ok=False)]

    with pytest.raises(real_requests.HTTPError):
        workday.fetch(SLUG)


# --- 11: US country facet -------------------------------------------------


def test_jobs_post_body_carries_us_country_facet(fake_requests) -> None:
    fake_requests.post_responses = [_jobs_page(0, [])]

    workday.fetch(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    body = post_calls[0]["json"]
    applied_facets = body["appliedFacets"]
    assert workday.USA_COUNTRY_FACET_KEY in applied_facets
    assert workday.USA_COUNTRY_WID in applied_facets[workday.USA_COUNTRY_FACET_KEY]


# --- 12-13: company display-name resolution -------------------------------


def test_company_comes_from_company_kwarg(fake_requests, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]
    fake_requests.get_responses = [_detail_payload()]

    postings = workday.fetch(SLUG, company="Some Display Name")

    assert len(postings) == 1
    assert postings[0].company == "Some Display Name"


def test_company_defaults_to_tenant_when_absent(fake_requests, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]
    fake_requests.get_responses = [_detail_payload()]

    postings = workday.fetch(SLUG)

    assert len(postings) == 1
    assert postings[0].company == TENANT
