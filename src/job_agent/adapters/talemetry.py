"""Talemetry / TTC-Portals adapter.

A single-target, server-rendered HTML scraper for one careers site fronted by
the Talemetry / TTC-Portals recruitment-marketing platform -- deliberately NOT
a generic multi-tenant Talemetry adapter (FR-010). Listing and detail pages
follow a predictable `/jobs/{id}-{slug}/` shape; the numeric job ID parsed from
that URL is the posting's stable `external_id` (FR-004) and the basis for the
canonical URL.

Registry shape: `vendor = "talemetry"` plus a `host` field. The reconstructed
slug passed here is the host unchanged (no compound encoding), so the
`fetch(slug, *, company=...) -> list[Posting]` contract is unchanged.

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

import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from ..schema import Posting, normalize

__all__ = ["fetch"]

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


def fetch(slug: str, *, company: str | None = None, timeout: int = 20) -> list[Posting]:
    """Fetch all open US postings for one Talemetry-hosted careers host.

    Args:
        slug: The careers `host` (e.g. "careers.example.com"), returned
            unchanged by registry slug reconstruction.
        company: Display company name; defaults to `host` when absent.
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized, US-scoped Posting objects (possibly empty).

    Raises:
        requests.HTTPError: On request failure (contained by the caller in
            fetch.py so one bad source does not abort the run).
    """
    company = company or slug
    base = f"https://{slug}"

    cap_raw = os.environ.get("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER")
    cap = int(cap_raw) if cap_raw else None

    postings: list[Posting] = []
    page = 1
    while True:
        resp = requests.get(
            f"{base}/jobs/", params={"page": page}, headers=HEADERS, timeout=timeout
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
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

            url = f"{base}{href}"
            time.sleep(0.5)  # be polite
            detail_resp = requests.get(url, headers=HEADERS, timeout=timeout)
            detail_resp.raise_for_status()
            detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
            description_el = detail_soup.select_one(_JOB_DESCRIPTION_SELECTOR)
            description = description_el.decode_contents() if description_el else ""

            postings.append(
                normalize(
                    source="talemetry",
                    company=company,
                    external_id=job_id,
                    title=title,
                    location=location,
                    description=description,
                    url=url,
                    posted_at=posted_at,
                )
            )
            if cap is not None and len(postings) >= cap:
                return postings[:cap]  # cap reached -> stop, do not page on

        time.sleep(0.5)  # be polite, between listing pages
        page += 1

    if not postings:
        print(f"talemetry: zero postings parsed for {slug}", file=sys.stderr)

    return postings
