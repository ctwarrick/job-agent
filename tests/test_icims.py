"""Tests for the iCIMS adapter's two-phase contract (`list_postings` /
`fetch_description`).

Stub-based, no network: `icims.requests` and `icims.time.sleep` are
monkeypatched per test. Mirrors the stub pattern in `tests/test_workday.py`.

The fixtures are synthetic payloads built to the real Jibe `/api/jobs` JSON
shape captured by the Phase 0 recon spike (see
specs/003-icims-adapter/research.md): a page is
``{"jobs": [{"data": {...}}, ...], "totalCount": N, "count": N}`` and the
adapter reads each job from ``jobs[].data``. Per the personal-data-privacy
rule, these are made-up postings, not raw captures of a real employer.

Unlike Workday, iCIMS returns the full job description inline in the listing
JSON, so `list_postings` returns fully-populated stubs (no separate detail
round-trip) and `fetch_description` is a pure pass-through with zero network
calls -- see specs/006-resilient-fetch/plan.md.

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

    def __init__(self, payload: object, status_ok: bool = True, json_raises: bool = False) -> None:
        self._payload = payload
        self._status_ok = status_ok
        self._json_raises = json_raises

    def json(self) -> object:
        if self._json_raises:
            raise ValueError("simulated non-JSON body")
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
    # Isolate the adapter from any real JOBAGENT_DATA_DIR so the default is
    # a clean fallback-to-tenant (no `company` kwarg supplied).
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
    assert icims._split_slug("hooli:careers.hooli.com") == ("hooli", "careers.hooli.com")


def test_split_slug_tenant_only_defaults_icims_host() -> None:
    assert icims._split_slug("acme") == ("acme", "acme.icims.com")


def test_split_slug_rejects_malformed_slug() -> None:
    with pytest.raises(ValueError):
        icims._split_slug("a:b:c")


# --- list_postings: page parse -> fully-populated Posting stub -----------


def test_list_postings_parses_jobs_page_into_postings(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job()])]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 1
    p = postings[0]
    assert isinstance(p, Posting)
    assert p.source == "icims"
    assert p.company == TENANT  # no `company` kwarg -> fallback to tenant slug
    assert p.external_id == "11003"
    assert p.title == "Quantitative Developer"
    assert "Bala Cynwyd" in p.location
    assert "Build low-latency trading systems." in p.description  # inline, pre-populated
    assert p.url == f"https://{HOST}/jobs/11003/login"
    assert p.posted_at == "2026-06-22T06:54:00+0000"

    assert fake_requests.calls[0]["url"] == f"https://{HOST}/api/jobs"
    assert fake_requests.calls[0]["params"]["page"] == 1


def test_list_postings_paginates_until_total_reached(fake_requests) -> None:
    # total=25, PAGE_SIZE=10 -> pages at 1, 2, 3 (10 + 10 + 5 = 25, 3 GETs).
    page1 = _page(25, [_job(req_id=str(i)) for i in range(10)])
    page2 = _page(25, [_job(req_id=str(i)) for i in range(10, 20)])
    page3 = _page(25, [_job(req_id=str(i)) for i in range(20, 25)])
    fake_requests.get_responses = [page1, page2, page3]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 25
    assert len(fake_requests.calls) == 3
    assert [c["params"]["page"] for c in fake_requests.calls] == [1, 2, 3]


def test_list_postings_stops_at_empty_page(fake_requests) -> None:
    # totalCount overstates 100, but the second page is empty -> stop there
    # rather than walking on toward page 10+.
    page1 = _page(100, [_job(req_id=str(i)) for i in range(10)])
    page2 = _page(100, [])
    fake_requests.get_responses = [page1, page2]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 10
    assert len(fake_requests.calls) == 2


# --- list_postings: US-location filter ------------------------------------


def test_list_postings_us_filter_keeps_us_drops_nonus_retains_unparseable(
    fake_requests,
) -> None:
    jobs = [
        _job(req_id="1", country_code="US", title="US Role"),
        _job(req_id="2", country_code="AU", title="Sydney Role"),
        # Empty/missing country -> ambiguous -> retained (FR-004 keep-if-any).
        _job(req_id="3", country_code="", title="Ambiguous Role", full_location=""),
    ]
    fake_requests.get_responses = [_page(3, jobs)]

    postings = icims.list_postings(SLUG)

    titles = {p.title for p in postings}
    assert "US Role" in titles
    assert "Ambiguous Role" in titles  # retained, not dropped
    assert "Sydney Role" not in titles  # positively non-US -> dropped
    assert len(postings) == 2


# --- list_postings: dedupe fingerprint stability --------------------------


def test_list_postings_dedupe_fingerprint_stable_across_runs(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job(req_id="42")])]
    first = icims.list_postings(SLUG)
    fake_requests.get_responses = [_page(1, [_job(req_id="42")])]
    second = icims.list_postings(SLUG)

    assert first[0].external_id == "42"  # numeric job id carried through
    assert first[0].fingerprint == second[0].fingerprint


# --- list_postings: company display-name resolution -----------------------


def test_list_postings_company_comes_from_company_kwarg(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job()])]

    postings = icims.list_postings(SLUG, company="Example Corp")

    assert len(postings) == 1
    assert postings[0].company == "Example Corp"


def test_list_postings_company_defaults_to_tenant_when_absent(fake_requests) -> None:
    fake_requests.get_responses = [_page(1, [_job()])]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 1
    assert postings[0].company == TENANT


# --- list_postings: per-employer cap REMOVED -------------------------------


def test_list_postings_ignores_legacy_per_employer_cap(fake_requests, monkeypatch) -> None:
    # The legacy single-call adapter halted pagination once it hit
    # JOBAGENT_MAX_POSTINGS_PER_EMPLOYER. The two-phase list_postings no
    # longer reads that env var at all, so all N=5 survive even though the
    # cap (2) is set and N > cap.
    monkeypatch.setenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", "2")
    page1 = _page(5, [_job(req_id=str(i), country_code="US") for i in range(5)])
    fake_requests.get_responses = [page1]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 5  # cap (2) ignored entirely


def test_list_postings_cap_unset_returns_all_from_source(fake_requests, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", raising=False)
    page1 = _page(7, [_job(req_id=str(i), country_code="US") for i in range(7)])
    fake_requests.get_responses = [page1]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 7


# --- list_postings: empty/blank inline description excluded ---------------


def test_list_postings_excludes_empty_inline_description(fake_requests) -> None:
    # The scorer needs description text, so a posting with none is dropped
    # rather than stored as an empty-description row (data-model.md).
    jobs = [
        _job(req_id="1", description="<p>Real description.</p>"),
        _job(req_id="2", description=""),  # excluded
        _job(req_id="3", description="   "),  # whitespace-only -> excluded
    ]
    fake_requests.get_responses = [_page(3, jobs)]

    postings = icims.list_postings(SLUG)

    assert {p.external_id for p in postings} == {"1"}


# --- list_postings: malformed response is an empty board, not an error ----


def test_list_postings_malformed_response_returns_empty_without_raising(fake_requests) -> None:
    fake_requests.get_responses = [{}]

    assert icims.list_postings(SLUG) == []


# --- list_postings: per-page resilience vs whole-source failure -----------


def test_list_postings_raises_when_first_page_fails(fake_requests) -> None:
    # Nothing has succeeded yet, so a failure on page 1 is a whole-source
    # failure: list_postings raises and resilient.run_source records it via
    # SourceResult.error, rather than silently returning an empty list.
    fake_requests.get_responses = [_FakeResponse({}, status_ok=False)]

    with pytest.raises(real_requests.HTTPError):
        icims.list_postings(SLUG)


def test_list_postings_retains_earlier_pages_when_later_page_fails(fake_requests) -> None:
    # Page 1 succeeds with 10 jobs (total=25 demands further pages), but
    # page 2 fails. The 10 already-fetched postings are not discarded: the
    # already-successful page survives so the run is not aborted outright,
    # matching the "partial source" tolerance in resilient.run_source.
    page1 = _page(25, [_job(req_id=str(i)) for i in range(10)])
    fake_requests.get_responses = [page1, _FakeResponse({}, status_ok=False)]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 10
    assert {p.external_id for p in postings} == {str(i) for i in range(10)}


def test_list_postings_retains_earlier_pages_when_later_page_returns_non_json(
    fake_requests,
) -> None:
    # Page 1 succeeds with 10 jobs (total=25 demands further pages), but
    # page 2's body fails to parse as JSON (status is OK -- the failure is
    # in resp.json(), a stand-in for a read timeout/non-JSON body per
    # FR-005). The already-successful page survives rather than being
    # discarded.
    page1 = _page(25, [_job(req_id=str(i)) for i in range(10)])
    bad_page = _FakeResponse({}, status_ok=True, json_raises=True)
    fake_requests.get_responses = [page1, bad_page]

    postings = icims.list_postings(SLUG)

    assert len(postings) == 10
    assert {p.external_id for p in postings} == {str(i) for i in range(10)}


# --- fetch_description: pure pass-through, zero network calls -------------


def test_fetch_description_returns_inline_description_with_no_network_call(
    fake_requests,
) -> None:
    posting = Posting(
        source="icims",
        company=TENANT,
        external_id="11003",
        title="Quantitative Developer",
        location="Bala Cynwyd, Pennsylvania",
        description="<p>Build low-latency trading systems.</p>",
        url=f"https://{HOST}/jobs/11003/login",
        posted_at="2026-06-22T06:54:00+0000",
    )

    result = icims.fetch_description(posting)

    assert result == "<p>Build low-latency trading systems.</p>"
    assert fake_requests.calls == []  # pass-through: zero GETs issued
