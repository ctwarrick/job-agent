"""Red-phase tests for the two-phase Talemetry / TTC-Portals adapter.

`talemetry.fetch` is being split into `list_postings` (paginated card listing
only, zero detail GETs, no per-employer cap) and `fetch_description` (one
detail GET per posting, called lazily downstream by `resilient.run_source`).
Stub-based, no network: `talemetry.requests` and `talemetry.time.sleep` are
monkeypatched per test. Mirrors the stub pattern in `tests/test_workday.py`
and `tests/test_icims.py`.

Fake-response interface (mirrors the real `requests.Response` surface the
adapter is expected to use):
    resp.text -> str (HTML body, in place of workday/icims's .json())
    resp.raise_for_status() -> None (or raises requests.HTTPError)

The fake `requests` stub exposes `.get(url, params=..., timeout=...)`,
recording each call's args on a shared `calls` list. Routing is by
presence of `params`: a GET with `params` is a paginated listing page; a
GET without `params` is a detail-page fetch (per the placeholder contract
in the dispatch prompt -- listing is `GET {host}/jobs/` with
`params={"page": n}`, detail is `GET {host}/jobs/{id}-{slug}/` with no
params).

Only the fictional placeholder host `careers.example.com` appears here --
no real employer name (FR-012).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests as real_requests

from job_agent.adapters import talemetry
from job_agent.schema import Posting

HOST = "careers.example.com"
LISTING_URL = f"https://{HOST}/jobs/"


class _FakeResponse:
    """Stand-in for requests.Response with a canned HTML body and status."""

    def __init__(self, text: str, status_ok: bool = True, text_raises: bool = False) -> None:
        self._text = text
        self._status_ok = status_ok
        self._text_raises = text_raises

    @property
    def text(self) -> str:
        if self._text_raises:
            raise ValueError("simulated non-parseable body")
        return self._text

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise real_requests.HTTPError("simulated HTTP error")


class _FakeRequests:
    """Stand-in for the `requests` module; records every GET made.

    Routing mirrors the placeholder contract: a GET carrying `params` is a
    listing page (served from `listing_responses`); a GET with no `params`
    is a detail-page fetch (served from `detail_responses`).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # Queued items may be a raw HTML str (convenience) or a
        # _FakeResponse (when a test needs to control status); both are
        # normalized to a _FakeResponse, since real requests.get always
        # returns one.
        self.listing_responses: list = []
        self.detail_responses: list = []

    @staticmethod
    def _as_response(item: object) -> _FakeResponse:
        return item if isinstance(item, _FakeResponse) else _FakeResponse(item)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if params is not None:
            return self._as_response(self.listing_responses.pop(0))
        return self._as_response(self.detail_responses.pop(0))


def _job_card(
    href: str = "/jobs/12345-software-engineer/",
    title: str = "Software Engineer",
    location: str = "Seattle, WA",
    posted_date: str = "2026-06-01",
    posted_text: str = "June 1, 2026",
) -> str:
    return (
        f'<a class="job-card" href="{href}">'
        f'<span class="job-title">{title}</span>'
        f'<span class="job-location">{location}</span>'
        f'<time class="job-date" datetime="{posted_date}">{posted_text}</time>'
        "</a>"
    )


def _listing_page(cards: list[str]) -> str:
    return f'<html><body><div class="job-list">{"".join(cards)}</div></body></html>'


def _detail_page(description: str = "<p>Full job description.</p>") -> str:
    return f'<html><body><div class="job-description">{description}</div></body></html>'


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    # Isolate the adapter from any real JOBAGENT_DATA_DIR so the default is
    # a clean fallback-to-host (no `company` kwarg supplied), mirroring
    # test_workday/test_icims.
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_requests(monkeypatch) -> _FakeRequests:
    fake = _FakeRequests()
    monkeypatch.setattr(talemetry, "requests", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(talemetry.time, "sleep", lambda *a, **k: calls.append(a))
    return calls


# --- _parse_job_id (unchanged helper) ---------------------------------------


def test_parse_job_id_parses_numeric_id() -> None:
    assert talemetry._parse_job_id("/jobs/12345-software-engineer/") == "12345"


def test_parse_job_id_returns_none_when_no_numeric_id() -> None:
    assert talemetry._parse_job_id("/jobs/no-numeric-id/") is None


# --- _is_us (unchanged helper) ----------------------------------------------


def test_is_us_keep_if_any() -> None:
    assert talemetry._is_us("Seattle, WA") is True
    assert talemetry._is_us("") is True
    assert talemetry._is_us("Unspecified") is True
    assert talemetry._is_us("Remote") is True
    assert talemetry._is_us("London, UK") is False


# --- list_postings: listing URL shape, zero detail GETs ---------------------


def test_list_postings_builds_correct_listing_url(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([])]

    talemetry.list_postings(HOST)

    listing_calls = [c for c in fake_requests.calls if c["params"] is not None]
    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(listing_calls) == 1
    assert listing_calls[0]["url"] == LISTING_URL
    assert listing_calls[0]["params"]["page"] == 1
    assert len(detail_calls) == 0


def test_list_postings_paginates_cards_with_zero_detail_gets(fake_requests) -> None:
    cards_page1 = [_job_card(href=f"/jobs/{i}-role-{i}/", title=f"Role {i}") for i in range(1, 4)]
    fake_requests.listing_responses = [_listing_page(cards_page1), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    listing_calls = [c for c in fake_requests.calls if c["params"] is not None]
    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(postings) == 3
    assert len(listing_calls) == 2  # page 1 (3 cards), page 2 (empty -> stop)
    assert len(detail_calls) == 0  # listing never fetches detail pages


def test_list_postings_stops_paging_on_empty_page(fake_requests) -> None:
    page1 = [_job_card(href="/jobs/1-role/", title="Role 1")]
    fake_requests.listing_responses = [_listing_page(page1), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    listing_calls = [c for c in fake_requests.calls if c["params"] is not None]
    assert len(postings) == 1
    assert len(listing_calls) == 2
    assert [c["params"]["page"] for c in listing_calls] == [1, 2]


# --- list_postings: stub field mapping, empty description -------------------


def test_list_postings_returns_stubs_with_mapped_fields_and_empty_description(
    fake_requests,
) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]

    postings = talemetry.list_postings(HOST, company="Example Co")

    assert len(postings) == 1
    posting = postings[0]
    assert isinstance(posting, Posting)
    assert posting.source == "talemetry"
    assert posting.company == "Example Co"
    assert posting.external_id == "12345"
    assert posting.title == "Software Engineer"
    assert posting.location == "Seattle, WA"
    assert posting.url == f"https://{HOST}/jobs/12345-software-engineer/"
    assert posting.posted_at == "2026-06-01"
    assert posting.description == ""  # listing makes zero detail GETs

    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(detail_calls) == 0


# --- list_postings: keep-if-any US filter -----------------------------------


def test_list_postings_us_filter_drops_nonus_retains_us_and_ambiguous(fake_requests) -> None:
    cards = [
        _job_card(href="/jobs/1-us-role/", title="US Role", location="Seattle, WA"),
        _job_card(href="/jobs/2-uk-role/", title="UK Role", location="London, UK"),
        _job_card(href="/jobs/3-ambiguous-role/", title="Ambiguous Role", location=""),
    ]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    titles = {p.title for p in postings}
    assert "US Role" in titles
    assert "Ambiguous Role" in titles  # retained, not dropped
    assert "UK Role" not in titles  # positively non-US -> dropped
    assert len(postings) == 2


# --- list_postings: company display-name resolution -------------------------


def test_list_postings_company_comes_from_company_kwarg(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]

    postings = talemetry.list_postings(HOST, company="Example Co")

    assert len(postings) == 1
    assert postings[0].company == "Example Co"


def test_list_postings_company_defaults_to_host_when_absent(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    assert len(postings) == 1
    assert postings[0].company == HOST


# --- list_postings: per-employer cap REMOVED --------------------------------


def test_list_postings_ignores_legacy_per_employer_cap(fake_requests, monkeypatch) -> None:
    # The legacy single-call adapter halted once it hit
    # JOBAGENT_MAX_POSTINGS_PER_EMPLOYER. The two-phase list_postings no
    # longer reads that env var, so all N=5 cards survive even though the
    # cap (2) is set and N > cap. A single page advertises all 5; no second
    # listing page or any detail GET should occur.
    monkeypatch.setenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", "2")
    cards = [_job_card(href=f"/jobs/{i}-role-{i}/", title=f"Role {i}") for i in range(1, 6)]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    assert len(postings) == 5  # cap (2) ignored entirely
    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(detail_calls) == 0


def test_list_postings_cap_unset_returns_all(fake_requests, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", raising=False)

    cards = [_job_card(href=f"/jobs/{i}-role-{i}/", title=f"Role {i}") for i in range(1, 4)]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    assert len(postings) == 3


# --- list_postings: per-page resilience vs whole-source failure -------------


def test_list_postings_raises_when_first_page_fails(fake_requests) -> None:
    # Nothing has succeeded yet, so a failure on page 1 is a whole-source
    # failure: list_postings raises and resilient.run_source records it via
    # SourceResult.error, rather than silently returning an empty list.
    fake_requests.listing_responses = [_FakeResponse("", status_ok=False)]

    with pytest.raises(real_requests.HTTPError):
        talemetry.list_postings(HOST)


def test_list_postings_retains_earlier_pages_when_later_page_fails(fake_requests) -> None:
    # Page 1 succeeds with 1 card; page 2 fails. The already-fetched card is
    # not discarded -- the run is not aborted outright, matching the
    # "partial source" tolerance in resilient.run_source.
    page1 = [_job_card(href="/jobs/1-role/", title="Role 1")]
    fake_requests.listing_responses = [_listing_page(page1), _FakeResponse("", status_ok=False)]

    postings = talemetry.list_postings(HOST)

    assert len(postings) == 1
    assert postings[0].title == "Role 1"


def test_list_postings_retains_earlier_pages_when_later_page_returns_non_json(
    fake_requests,
) -> None:
    # Page 1 succeeds with 1 card; page 2's status is OK but its body fails
    # to parse (resp.text raises ValueError), a stand-in for a read
    # timeout/non-JSON body per FR-005. The already-fetched card is not
    # discarded, and listing never issues any detail GETs.
    page1 = [_job_card(href="/jobs/1-role/", title="Role 1")]
    bad_page = _FakeResponse("", status_ok=True, text_raises=True)
    fake_requests.listing_responses = [_listing_page(page1), bad_page]

    postings = talemetry.list_postings(HOST)

    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(postings) == 1
    assert postings[0].title == "Role 1"
    assert len(detail_calls) == 0


# --- list_postings: zero-postings / unparseable-id edge cases ---------------


def test_list_postings_zero_postings_returns_empty_list_with_distinct_warning(
    fake_requests, capsys
) -> None:
    # An empty first page is a genuinely empty board (FR-014/SC-007): return
    # [] without raising, and emit a distinct stderr warning rather than
    # silently looking identical to a successful empty run.
    fake_requests.listing_responses = [_listing_page([])]

    postings = talemetry.list_postings(HOST)

    assert postings == []
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_list_postings_card_with_unparseable_job_id_is_skipped_with_warning(
    fake_requests, capsys
) -> None:
    cards = [
        _job_card(href="/jobs/no-numeric-id/", title="Bad Card"),
        _job_card(href="/jobs/99-good-card/", title="Good Card"),
    ]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]

    postings = talemetry.list_postings(HOST)

    titles = {p.title for p in postings}
    assert "Good Card" in titles
    assert "Bad Card" not in titles
    assert len(postings) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


# --- fetch_description: exactly one detail GET, parsed text returned --------


def test_fetch_description_issues_one_get_and_returns_description(fake_requests) -> None:
    stub = Posting(
        source="talemetry",
        company=HOST,
        external_id="12345",
        title="Software Engineer",
        location="Seattle, WA",
        description="",
        url=f"https://{HOST}/jobs/12345-software-engineer/",
        posted_at="2026-06-01",
    )
    fake_requests.detail_responses = [_detail_page("<p>Full job description.</p>")]

    description = talemetry.fetch_description(stub)

    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(detail_calls) == 1
    assert detail_calls[0]["url"] == stub.url
    assert "Full job description." in description


def test_fetch_description_uses_listing_stub_directly(fake_requests) -> None:
    # Build the stub via list_postings itself (no detail GET consumed yet),
    # then fetch its description as a separate call.
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]
    [stub] = talemetry.list_postings(HOST)

    fake_requests.detail_responses = [_detail_page("Detail body text.")]
    description = talemetry.fetch_description(stub)

    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(detail_calls) == 1
    assert detail_calls[0]["url"] == stub.url
    assert "Detail body text." in description


def test_fetch_description_politeness_sleep_called(fake_requests, fake_sleep) -> None:
    stub = Posting(
        source="talemetry",
        company=HOST,
        external_id="12345",
        title="Software Engineer",
        location="Seattle, WA",
        description="",
        url=f"https://{HOST}/jobs/12345-software-engineer/",
        posted_at="2026-06-01",
    )
    fake_requests.detail_responses = [_detail_page()]

    talemetry.fetch_description(stub)

    assert len(fake_sleep) >= 1
