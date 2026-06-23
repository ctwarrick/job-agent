"""Lever adapter.

Public postings API, no auth required:
    https://api.lever.co/v0/postings/{slug}?mode=json

Returns a flat JSON array. Description comes back as both `description`
(HTML) and `descriptionPlain`; we prefer the plain text when present.
"""

from __future__ import annotations

import time

import requests

from ..schema import Posting, normalize

BASE = "https://api.lever.co/v0/postings/{slug}"
HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}


def fetch(slug: str, *, company: str | None = None, timeout: int = 20) -> list[Posting]:
    """Fetch all open postings for one Lever account.

    Calls the public Lever postings API. Prefers descriptionPlain over
    the HTML description field.

    Args:
        slug: Lever account slug (e.g. 'initech').
        company: Display company name; defaults to `slug` when absent.
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting objects.

    Raises:
        requests.HTTPError: On API request failure.
    """
    company = company or slug
    url = BASE.format(slug=slug)
    resp = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    jobs = resp.json()  # Lever returns a top-level list

    postings: list[Posting] = []
    for job in jobs:
        cats = job.get("categories") or {}
        loc = cats.get("location", "")
        desc = job.get("descriptionPlain") or job.get("description", "")
        # Lever timestamps are epoch millis
        ts = job.get("createdAt")
        posted_iso = None
        if ts:
            from datetime import datetime, timezone

            posted_iso = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()

        postings.append(
            normalize(
                source="lever",
                company=company,
                external_id=job.get("id"),
                title=job.get("text", ""),
                location=loc,
                description=desc,
                url=job.get("hostedUrl", ""),
                posted_at=posted_iso,
            )
        )
    time.sleep(0.5)
    return postings
