"""Red-phase tests for the Talemetry / TTC-Portals adapter (`talemetry.fetch`).

Stub-based, no network: `talemetry.requests` and `talemetry.time.sleep` are
monkeypatched per test. See specs/005-talemetry-adapter/plan.md and
contracts/talemetry-adapter.md for the approved design this pins down.

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

    def __init__(self, text: str, status_ok: bool = True) -> None:
        self.text = text
        self._status_ok = status_ok

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


# === T005 [US1] ============================================================

# --- _parse_job_id ----------------------------------------------------------


def test_parse_job_id_parses_numeric_id() -> None:
    assert talemetry._parse_job_id("/jobs/12345-software-engineer/") == "12345"


def test_parse_job_id_returns_none_when_no_numeric_id() -> None:
    assert talemetry._parse_job_id("/jobs/no-numeric-id/") is None


# --- listing URL shape -------------------------------------------------------


def test_fetch_builds_correct_listing_url(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([])]

    talemetry.fetch(HOST)

    listing_calls = [c for c in fake_requests.calls if c["params"] is not None]
    assert len(listing_calls) == 1
    assert listing_calls[0]["url"] == LISTING_URL
    assert listing_calls[0]["params"]["page"] == 1


# --- field mapping through normalize(), incl. detail-page description -------


def test_listing_card_maps_through_normalize_with_detail_description(fake_requests) -> None:
    fake_requests.listing_responses = [
        _listing_page([_job_card()]),
        _listing_page([]),  # paging stops on the next, empty page
    ]
    fake_requests.detail_responses = [_detail_page("<p>Full job description.</p>")]

    postings = talemetry.fetch(HOST, company="Example Co")

    assert len(postings) == 1
    posting = postings[0]
    assert isinstance(posting, Posting)
    assert posting.source == "talemetry"
    assert posting.company == "Example Co"
    assert posting.external_id  # present, non-empty
    assert posting.external_id == "12345"
    assert posting.title == "Software Engineer"
    assert posting.location == "Seattle, WA"
    assert posting.url.startswith("https://")
    assert posting.posted_at == "2026-06-01"

    # FR-013: the listing never carries the full description -- a detail GET
    # must have occurred, and its content must land on the posting.
    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(detail_calls) == 1
    assert "Full job description." in posting.description


# --- keep-if-any US filter ---------------------------------------------------


def test_is_us_keep_if_any() -> None:
    assert talemetry._is_us("Seattle, WA") is True
    assert talemetry._is_us("") is True
    assert talemetry._is_us("Unspecified") is True
    assert talemetry._is_us("Remote") is True
    assert talemetry._is_us("London, UK") is False


def test_us_filter_drops_nonus_retains_us_and_ambiguous(fake_requests) -> None:
    cards = [
        _job_card(href="/jobs/1-us-role/", title="US Role", location="Seattle, WA"),
        _job_card(href="/jobs/2-uk-role/", title="UK Role", location="London, UK"),
        _job_card(href="/jobs/3-ambiguous-role/", title="Ambiguous Role", location=""),
    ]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]
    fake_requests.detail_responses = [_detail_page(), _detail_page(), _detail_page()]

    postings = talemetry.fetch(HOST)

    titles = {p.title for p in postings}
    assert "US Role" in titles
    assert "Ambiguous Role" in titles  # retained, not dropped
    assert "UK Role" not in titles  # positively non-US -> dropped
    assert len(postings) == 2


# --- company display-name resolution -----------------------------------------


def test_company_comes_from_company_kwarg(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]
    fake_requests.detail_responses = [_detail_page()]

    postings = talemetry.fetch(HOST, company="Example Co")

    assert len(postings) == 1
    assert postings[0].company == "Example Co"


def test_company_defaults_to_host_when_absent(fake_requests) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]
    fake_requests.detail_responses = [_detail_page()]

    postings = talemetry.fetch(HOST)

    assert len(postings) == 1
    assert postings[0].company == HOST


# --- politeness ---------------------------------------------------------------


def test_politeness_sleep_called_between_requests(fake_requests, fake_sleep) -> None:
    fake_requests.listing_responses = [_listing_page([_job_card()]), _listing_page([])]
    fake_requests.detail_responses = [_detail_page()]

    talemetry.fetch(HOST)

    assert len(fake_sleep) >= 1


# === T011 [US2] =============================================================

# A cap below the board size must bound BOTH the postings returned and the
# round-trips made (no paging/detail past the cap), mirroring
# test_workday.test_per_employer_cap_limits_results.


def test_per_employer_cap_limits_results_and_halts_paging(fake_requests, monkeypatch) -> None:
    # Cap = 2 against a single listing page advertising 5 cards. Reaching the
    # cap happens within the first page, so only 1 listing GET and exactly 2
    # detail GETs should occur -- not 5 detail GETs and not a second listing
    # page GET.
    monkeypatch.setenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", "2")

    cards = [_job_card(href=f"/jobs/{i}-role-{i}/", title=f"Role {i}") for i in range(1, 6)]
    fake_requests.listing_responses = [_listing_page(cards)]
    fake_requests.detail_responses = [_detail_page(), _detail_page()]

    postings = talemetry.fetch(HOST)

    assert len(postings) == 2
    listing_calls = [c for c in fake_requests.calls if c["params"] is not None]
    detail_calls = [c for c in fake_requests.calls if c["params"] is None]
    assert len(listing_calls) == 1
    assert len(detail_calls) == 2


def test_cap_unset_returns_all(fake_requests, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER", raising=False)

    cards = [_job_card(href=f"/jobs/{i}-role-{i}/", title=f"Role {i}") for i in range(1, 4)]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]
    fake_requests.detail_responses = [_detail_page() for _ in range(3)]

    postings = talemetry.fetch(HOST)

    assert len(postings) == 3


# === T013 [US3] ==============================================================


def test_fetch_raises_on_http_error_so_run_records_failure(fake_requests) -> None:
    fake_requests.listing_responses = [_FakeResponse("", status_ok=False)]

    with pytest.raises(real_requests.HTTPError):
        talemetry.fetch(HOST)


def test_zero_postings_returns_empty_list_with_distinct_warning(fake_requests, capsys) -> None:
    # An empty first page is a genuinely empty board (FR-014/SC-007): return
    # [] without raising, and emit a distinct stderr warning rather than
    # silently looking identical to a successful empty run.
    fake_requests.listing_responses = [_listing_page([])]

    postings = talemetry.fetch(HOST)

    assert postings == []
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_card_with_unparseable_job_id_is_skipped_with_warning(fake_requests, capsys) -> None:
    cards = [
        _job_card(href="/jobs/no-numeric-id/", title="Bad Card"),
        _job_card(href="/jobs/99-good-card/", title="Good Card"),
    ]
    fake_requests.listing_responses = [_listing_page(cards), _listing_page([])]
    fake_requests.detail_responses = [_detail_page()]

    postings = talemetry.fetch(HOST)

    titles = {p.title for p in postings}
    assert "Good Card" in titles
    assert "Bad Card" not in titles
    assert len(postings) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
