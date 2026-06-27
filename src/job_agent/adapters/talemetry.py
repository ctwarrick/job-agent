"""Talemetry / TTC-Portals adapter.

A single-target, server-rendered HTML scraper for one careers site fronted by
the Talemetry / TTC-Portals recruitment-marketing platform -- deliberately NOT
a generic multi-tenant Talemetry adapter (FR-010). Listing and detail pages
follow a predictable `/jobs/{id}-{slug}/` shape; the numeric job ID parsed from
that URL is the posting's stable `external_id` (FR-004) and the basis for the
canonical URL.

Registry shape: `vendor = "talemetry"` plus a `host` field. The reconstructed
slug passed here is the host unchanged (no compound encoding).

Two-phase contract (see `job_agent.resilient`, which calls these lazily so
filtering happens before any detail fetch is paid for):
  1. `list_postings(slug, *, company=...) -> list[Posting]` paginates
     `GET https://{host}/jobs/` (page-numbered), returning title/location/
     posted_at stubs scoped to a US keep-if-any filter, with an empty
     `description` and zero detail GETs.
  2. `fetch_description(posting, ...) -> str` issues one `GET` against the
     stub's detail URL and returns its full job description.

The display company name is the caller-supplied `company` kwarg (resolved
upstream from `registry.toml`); it falls back to the `host` when absent, since
company feeds the dedupe fingerprint (schema.py) and must never be empty.

Dedupe note: the shared `Posting.fingerprint` stays content-based
(title+company+location+description); the numeric ID is stored as `external_id`
but is NOT the fingerprint (see specs/005-talemetry-adapter/plan.md "dedupe
identity reconciliation").

`requests` and `time` are imported at module level so tests can monkeypatch
`talemetry.requests` / `talemetry.time.sleep`; the BeautifulSoup parse runs on
canned HTML offline.

Status: shipped DARK / inactive. The intended live target fronts its board
with a Cloudflare "managed challenge" (a JS/browser-fingerprint gate) that a
plain `requests` GET cannot pass -- every path returns the interstitial, not
job HTML. So the selectors and `_NON_US_MARKERS` below are UNCONFIRMED
placeholders, never verified against live markup, and no live source is wired
into `registry.toml`. The adapter is fully stub-tested and ready to light up
once the access problem is solved (a headless browser or an alternate
non-gated feed); see specs/005-talemetry-adapter/tasks.md T017.
"""

from __future__ import annotations

import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from requests import RequestException

from ..schema import Posting, normalize

__all__ = ["list_postings", "fetch_description"]

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}

# Listing/detail selectors -- UNCONFIRMED placeholders. The live target is
# Cloudflare-gated (see module docstring), so these were never verified against
# live markup; the adapter ships dark. T017 (skipped) tracks confirmation.
_JOB_CARD_SELECTOR = "a.job-card"
_JOB_TITLE_SELECTOR = ".job-title"
_JOB_LOCATION_SELECTOR = ".job-location"
_JOB_DATE_SELECTOR = "time.job-date"
_JOB_DESCRIPTION_SELECTOR = ".job-description"

_JOB_ID_RE = re.compile(r"/jobs/(\d+)-")

# Locations carrying one of these markers are positively non-US and dropped;
# everything else (including empty/ambiguous) is kept (FR-005 keep-if-any).
# UNCONFIRMED set -- live target Cloudflare-gated; ships dark (T017).
_NON_US_MARKERS = {"UK", "UNITED KINGDOM"}


def _parse_job_id(href: str) -> str | None:
    """Parse the numeric job ID out of a `/jobs/{id}-{slug}/` href.

    Args:
        href: The job card's `href` attribute.

    Returns:
        The numeric ID as a string, or None if `href` carries no
        recognizable numeric ID.
    """
    match = _JOB_ID_RE.search(href)
    return match.group(1) if match else None


def _is_us(location: str) -> bool:
    """Keep-if-any US gate: drop only a positively-non-US location.

    Args:
        location: The job card's location text (may be empty).

    Returns:
        False only when `location` contains a known non-US marker; True
        otherwise (including empty/"Unspecified"/"Remote").
    """
    if not location:
        return True
    upper = location.upper()
    return not any(marker in upper for marker in _NON_US_MARKERS)


def list_postings(slug: str, *, company: str | None = None, timeout: int = 20) -> list[Posting]:
    """List all open US posting stubs for one Talemetry-hosted careers host.

    Issues zero detail GETs; `description` is left empty for the caller to
    fill in lazily via `fetch_description`.

    Args:
        slug: The careers `host` (e.g. "careers.example.com"), returned
            unchanged by registry slug reconstruction.
        company: Display company name; defaults to `host` when absent.
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting stubs with empty `description`. A page
        failure after at least one page has already succeeded stops
        pagination but retains the postings already collected (FR-005/006).

    Raises:
        requests.HTTPError: On a first-page request failure (contained by
            the caller in fetch.py so one bad source does not abort the
            run).
    """
    company = company or slug
    base = f"https://{slug}"

    postings: list[Posting] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{base}/jobs/", params={"page": page}, headers=HEADERS, timeout=timeout
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except (RequestException, ValueError):
            if page == 1:
                raise  # nothing has succeeded yet -> whole-source failure
            print(
                f"  ! talemetry/{slug} page {page} failed; "
                f"keeping {len(postings)} prior postings",
                file=sys.stderr,
            )
            break

        cards = soup.select(_JOB_CARD_SELECTOR)
        if not cards:
            break

        for card in cards:
            href = card.get("href", "")
            job_id = _parse_job_id(href)
            if job_id is None:
                print(f"talemetry: skipping unparseable job href {href!r}", file=sys.stderr)
                continue

            title_el = card.select_one(_JOB_TITLE_SELECTOR)
            location_el = card.select_one(_JOB_LOCATION_SELECTOR)
            date_el = card.select_one(_JOB_DATE_SELECTOR)
            title = title_el.get_text(strip=True) if title_el else ""
            location = location_el.get_text(strip=True) if location_el else ""
            posted_at = date_el.get("datetime") if date_el else None

            if not _is_us(location):
                continue

            postings.append(
                normalize(
                    source="talemetry",
                    company=company,
                    external_id=job_id,
                    title=title,
                    location=location,
                    description="",
                    url=f"{base}{href}",
                    posted_at=posted_at,
                )
            )

        page += 1

    if not postings:
        print(f"talemetry: zero postings parsed for {slug}", file=sys.stderr)

    return postings


def fetch_description(posting: Posting, *, timeout: int = 20) -> str:
    """Fetch the full job description for one posting stub.

    Args:
        posting: A stub returned by `list_postings`; its `url` is the
            detail endpoint.
        timeout: Request timeout in seconds (default 20).

    Returns:
        The full job description text (empty string if absent).

    Raises:
        requests.HTTPError: On request failure.
    """
    resp = requests.get(posting.url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    description_el = soup.select_one(_JOB_DESCRIPTION_SELECTOR)
    description = description_el.decode_contents() if description_el else ""
    time.sleep(0.5)  # be polite
    return description
