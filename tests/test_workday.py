"""Red-phase tests for the two-phase Workday adapter.

`workday.fetch` is being split into `list_postings` (paginated listing only,
zero detail GETs, no per-employer cap) and `fetch_description` (one detail
GET per posting, called lazily downstream). Stub-based, no network:
`workday.requests` and `workday.time.sleep` are monkeypatched per test.

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

TENANT = "globex"
SITE = "Globex"
HOST = "wd5"
SLUG = f"{TENANT}:{SITE}:{HOST}"


class _FakeResponse:
    """Stand-in for requests.Response with a canned JSON body and status."""

    def __init__(
        self,
        payload: object,
        status_ok: bool = True,
        json_raises: bool = False,
        status_code: int | None = None,
    ) -> None:
        self._payload = payload
        self._status_ok = status_ok
        self._json_raises = json_raises
        # Mirror requests.Response.status_code so the adapter can distinguish a
        # 400 (a facet the tenant rejects) from other failures. A failing
        # response defaults to 500 so the existing "propagate on error" tests
        # keep exercising the non-400 path.
        self.status_code = status_code if status_code is not None else (200 if status_ok else 500)

    def json(self) -> object:
        if self._json_raises:
            raise ValueError("simulated non-JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise real_requests.HTTPError("simulated HTTP error", response=self)


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
    assert workday._split_slug("globex:Globex:wd5") == (
        "globex",
        "Globex",
        "wd5",
    )


def test_split_slug_rejects_malformed_slug() -> None:
    with pytest.raises(ValueError):
        workday._split_slug("globex:Globex")


# --- 3: URL shape ----------------------------------------------------------


def test_list_postings_builds_correct_jobs_post_url(fake_requests) -> None:
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]

    workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}/jobs"
    )
    assert len(get_calls) == 0


# --- 4-5: pagination, zero detail GETs --------------------------------------


def test_pagination_walks_offset_until_total_reached(fake_requests) -> None:
    # total=45, limit=20 -> pages at offset 0, 20, 40 (3 POSTs). Listing must
    # not issue any detail GETs at all.
    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20, 40)])
    page3 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(40, 45)])
    fake_requests.post_responses = [page1, page2, page3]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 3
    offsets = [c["json"]["offset"] for c in post_calls]
    assert offsets == [0, 20, 40]
    assert len(postings) == 45
    assert len(get_calls) == 0


def test_pagination_uses_first_page_total_when_later_pages_report_zero(fake_requests) -> None:
    # Workday reports a meaningful `total` only on the FIRST page; later pages
    # report total=0 while still serving full pages. The loop must remember the
    # first page's total (45) and keep paging, not stop early when page 2 says
    # 0 (the cap-at-40 bug). It walks offset 0, 20, 40 -> all 45 postings.
    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(0, [_job_posting(external_path=f"/job/{i}") for i in range(20, 40)])
    page3 = _jobs_page(0, [_job_posting(external_path=f"/job/{i}") for i in range(40, 45)])
    fake_requests.post_responses = [page1, page2, page3]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    assert [c["json"]["offset"] for c in post_calls] == [0, 20, 40]
    assert len(postings) == 45


def test_pagination_stops_at_empty_page(fake_requests) -> None:
    # total is overstated at 100, but the second page is empty -> must stop
    # after the empty page rather than trying to walk to offset 100+.
    page1 = _jobs_page(100, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(100, [])
    fake_requests.post_responses = [page1, page2]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 2
    assert len(postings) == 20
    assert len(get_calls) == 0


# --- 6: field mapping returns stubs (no detail call) ------------------------


def test_list_postings_returns_stubs_with_mapped_fields(
    fake_requests, monkeypatch, tmp_path: Path
) -> None:
    # No `company` kwarg supplied -> company falls back to tenant.
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

    postings = workday.list_postings(SLUG)

    assert len(postings) == 1
    posting = postings[0]
    assert isinstance(posting, Posting)
    assert posting.source == "workday"
    assert posting.company == TENANT
    assert posting.external_id == "/job/Remote/Engineer_R-1"
    assert posting.title == "Software Engineer"
    assert posting.location == "Seattle, WA"
    assert posting.description == ""
    assert posting.url == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/{SITE}/job/Remote/Engineer_R-1"
    )
    assert posting.url.startswith("https://")
    assert posting.url.endswith("/job/Remote/Engineer_R-1")
    assert posting.posted_at == "2026-06-01"

    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(get_calls) == 0


# --- 7-8: per-employer cap REMOVED for listing ------------------------------


def test_per_employer_cap_env_var_is_ignored_by_list_postings(fake_requests, monkeypatch) -> None:
    # A single page of 20 jobs, total=20. Even with the legacy cap env var
    # set to 5, list_postings must return all 20 -- the cap no longer
    # applies at the listing phase.
    monkeypatch.setenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", "5")

    page1 = _jobs_page(20, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    fake_requests.post_responses = [page1]

    postings = workday.list_postings(SLUG)

    assert len(postings) == 20
    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 1
    assert len(get_calls) == 0


def test_cap_unset_returns_all(fake_requests, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", raising=False)

    page1 = _jobs_page(7, [_job_posting(external_path=f"/job/{i}") for i in range(7)])
    fake_requests.post_responses = [page1]

    postings = workday.list_postings(SLUG)

    assert len(postings) == 7


# --- 9: HTTP error propagation (listing) ------------------------------------


def test_list_postings_raises_on_http_error_so_run_records_failure(fake_requests) -> None:
    fake_requests.post_responses = [_FakeResponse({}, status_ok=False)]

    with pytest.raises(real_requests.HTTPError):
        workday.list_postings(SLUG)


def test_list_postings_retains_earlier_pages_when_later_page_fails(fake_requests) -> None:
    # Page 1 (offset 0) succeeds with 20 jobs; total=45 demands a further
    # page at offset 20, but that second POST fails. The 20 already-fetched
    # postings are not discarded: the earlier page survives and pagination
    # simply stops at the failed page, matching the "partial source"
    # tolerance in resilient.run_source (FR-005/006).
    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    fake_requests.post_responses = [page1, _FakeResponse({}, status_ok=False)]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(postings) == 20
    assert {p.external_id for p in postings} == {f"/job/{i}" for i in range(20)}
    assert len(post_calls) == 2
    assert len(get_calls) == 0


def test_list_postings_retains_earlier_pages_when_later_page_returns_non_json(
    fake_requests,
) -> None:
    # Page 1 (offset 0) succeeds with 20 jobs; total=45 demands a further
    # page at offset 20, but that second POST returns a body that fails to
    # parse as JSON (status is OK -- the failure is in resp.json(), a
    # stand-in for a read timeout/non-JSON body per FR-005). The 20
    # already-fetched postings are not discarded.
    page1 = _jobs_page(45, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    bad_page = _FakeResponse({}, status_ok=True, json_raises=True)
    fake_requests.post_responses = [page1, bad_page]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(postings) == 20
    assert {p.external_id for p in postings} == {f"/job/{i}" for i in range(20)}
    assert len(post_calls) == 2
    assert len(get_calls) == 0


# --- 10: US country facet ---------------------------------------------------


def test_jobs_post_body_carries_us_country_facet(fake_requests) -> None:
    fake_requests.post_responses = [_jobs_page(0, [])]

    workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    body = post_calls[0]["json"]
    applied_facets = body["appliedFacets"]
    assert workday.USA_COUNTRY_FACET_KEY in applied_facets
    assert workday.USA_COUNTRY_WID in applied_facets[workday.USA_COUNTRY_FACET_KEY]


# --- 10b: facet fallback when a tenant rejects the US country facet ---------


def test_list_postings_retries_without_facet_on_400(fake_requests) -> None:
    # Some tenants don't expose the US country facet and 400 any request that
    # applies it. The first faceted POST 400s; the adapter must drop the facet
    # and retry the SAME page, then return its postings.
    bad = _FakeResponse({}, status_ok=False, status_code=400)
    ok_page = _jobs_page(1, [_job_posting(external_path="/job/0")])
    fake_requests.post_responses = [bad, ok_page]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(post_calls) == 2
    # Both attempts target the same first page (offset 0).
    assert [c["json"]["offset"] for c in post_calls] == [0, 0]
    # First attempt carried the US facet; the retry dropped it.
    assert workday.USA_COUNTRY_FACET_KEY in post_calls[0]["json"]["appliedFacets"]
    assert post_calls[1]["json"]["appliedFacets"] == {}
    assert len(postings) == 1
    assert len(get_calls) == 0


def test_list_postings_raises_when_400_persists_without_facet(fake_requests) -> None:
    # If the unfaceted retry ALSO 400s, the fallback must not mask it: with no
    # page ever succeeding, list_postings re-raises so run_source records a
    # whole-source failure. The retry happens exactly once (2 POSTs).
    bad1 = _FakeResponse({}, status_ok=False, status_code=400)
    bad2 = _FakeResponse({}, status_ok=False, status_code=400)
    fake_requests.post_responses = [bad1, bad2]

    with pytest.raises(real_requests.HTTPError):
        workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    assert len(post_calls) == 2
    assert post_calls[0]["json"]["appliedFacets"] != {}
    assert post_calls[1]["json"]["appliedFacets"] == {}


def test_list_postings_keeps_facet_across_pages_when_accepted(fake_requests) -> None:
    # A tenant that accepts the facet must never trigger the unfaceted retry:
    # every page keeps the facet and the POST count equals the page count.
    page1 = _jobs_page(25, [_job_posting(external_path=f"/job/{i}") for i in range(20)])
    page2 = _jobs_page(25, [_job_posting(external_path=f"/job/{i}") for i in range(20, 25)])
    fake_requests.post_responses = [page1, page2]

    postings = workday.list_postings(SLUG)

    post_calls = [c for c in fake_requests.calls if c["method"] == "post"]
    assert len(post_calls) == 2
    for c in post_calls:
        assert workday.USA_COUNTRY_FACET_KEY in c["json"]["appliedFacets"]
    assert len(postings) == 25


# --- 11-12: company display-name resolution ---------------------------------


def test_company_comes_from_company_kwarg(fake_requests, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]

    postings = workday.list_postings(SLUG, company="Some Display Name")

    assert len(postings) == 1
    assert postings[0].company == "Some Display Name"


def test_company_defaults_to_tenant_when_absent(fake_requests, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]

    postings = workday.list_postings(SLUG)

    assert len(postings) == 1
    assert postings[0].company == TENANT


# --- 13: fetch_description issues exactly one detail GET --------------------


def test_fetch_description_issues_one_get_and_returns_description(fake_requests) -> None:
    stub = Posting(
        source="workday",
        company=TENANT,
        external_id="/job/Remote/Engineer_R-1",
        title="Software Engineer",
        location="Seattle, WA",
        description="",
        url=f"https://{TENANT}.{HOST}.myworkdayjobs.com/{SITE}/job/Remote/Engineer_R-1",
        posted_at="2026-06-01",
    )
    fake_requests.get_responses = [_detail_payload("Full job description.")]

    description = workday.fetch_description(stub)

    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}"
        "/job/Remote/Engineer_R-1"
    )
    assert "Full job description." in description


def test_fetch_description_uses_listing_stub_directly(fake_requests) -> None:
    # Build the stub via list_postings itself (no detail GET consumed yet),
    # then fetch its description as a separate call.
    fake_requests.post_responses = [_jobs_page(1, [_job_posting()])]
    [stub] = workday.list_postings(SLUG)

    fake_requests.get_responses = [_detail_payload("Detail body text.")]
    description = workday.fetch_description(stub)

    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}"
        "/job/Remote/Engineer_R-1"
    )
    assert "Detail body text." in description


# --- 14: politeness sleep happens during fetch_description ------------------


def test_fetch_description_politeness_sleep_called(fake_requests, fake_sleep) -> None:
    stub = Posting(
        source="workday",
        company=TENANT,
        external_id="/job/Remote/Engineer_R-1",
        title="Software Engineer",
        location="Seattle, WA",
        description="",
        url=f"https://{TENANT}.{HOST}.myworkdayjobs.com/{SITE}/job/Remote/Engineer_R-1",
        posted_at="2026-06-01",
    )
    fake_requests.get_responses = [_detail_payload()]

    workday.fetch_description(stub)

    assert len(fake_sleep) >= 1


# --- 15: url becomes the public careers page; fetch_description ------------
# reconstructs the CXS endpoint from it -------------------------------------


def test_list_postings_returns_public_url_without_cxs_segment(fake_requests) -> None:
    # Digest links must resolve to the human-facing careers page, not the
    # raw CXS API JSON endpoint -- so `url` must drop the `/wday/cxs/{tenant}`
    # segment entirely.
    fake_requests.post_responses = [
        _jobs_page(1, [_job_posting(external_path="/job/USA-CA/Software-Engineer_R-123")])
    ]

    [posting] = workday.list_postings(SLUG)

    expected_url = (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/{SITE}/job/USA-CA/Software-Engineer_R-123"
    )
    assert posting.url == expected_url
    assert "/wday/cxs/" not in posting.url


def test_fetch_description_gets_reconstructed_cxs_endpoint_from_public_url(fake_requests) -> None:
    # Given a stub whose `url` is already the NEW public form (no CXS
    # segment), fetch_description must GET the reconstructed CXS detail
    # endpoint: {scheme}://{netloc}/wday/cxs/{tenant}{path}, where tenant is
    # the first hostname label.
    stub = Posting(
        source="workday",
        company=TENANT,
        external_id="/job/USA-CA/Software-Engineer_R-123",
        title="Software Engineer",
        location="Seattle, WA",
        description="",
        url=f"https://{TENANT}.{HOST}.myworkdayjobs.com/{SITE}/job/USA-CA/Software-Engineer_R-123",
        posted_at="2026-06-01",
    )
    fake_requests.get_responses = [_detail_payload("Full job description.")]

    description = workday.fetch_description(stub)

    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}"
        "/job/USA-CA/Software-Engineer_R-123"
    )
    assert "Full job description." in description


def test_fetch_description_round_trips_to_cxs_endpoint_for_listing_stub(fake_requests) -> None:
    # A stub produced by list_postings must carry the public-form url (no
    # /wday/cxs/ segment) -- otherwise the "round trip" below is trivial,
    # since a stub that already holds the CXS url would pass straight
    # through fetch_description unchanged. With the public-form url in
    # place, fetch_description must still round-trip to the EXACT old CXS
    # URL, so no detail-fetch behavior regresses when the public url
    # replaces it.
    fake_requests.post_responses = [
        _jobs_page(1, [_job_posting(external_path="/job/USA-CA/Software-Engineer_R-123")])
    ]
    [stub] = workday.list_postings(SLUG)
    assert "/wday/cxs/" not in stub.url

    fake_requests.get_responses = [_detail_payload("Detail body text.")]
    workday.fetch_description(stub)

    get_calls = [c for c in fake_requests.calls if c["method"] == "get"]
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == (
        f"https://{TENANT}.{HOST}.myworkdayjobs.com/wday/cxs/{TENANT}/{SITE}"
        "/job/USA-CA/Software-Engineer_R-123"
    )
