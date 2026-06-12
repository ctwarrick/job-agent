"""Greenhouse adapter.

Public board API, no auth required:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

The ?content=true flag returns the full job description (HTML-escaped) inline,
which saves a second round-trip per job.
"""

from __future__ import annotations

import html
import time

import requests

from ..schema import Posting, normalize

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}


def fetch(slug: str, *, timeout: int = 20) -> list[Posting]:
    """Fetch all open postings for one Greenhouse board.

    Calls the public Greenhouse Boards API with ?content=true to get
    full descriptions inline.

    Args:
        slug: Greenhouse board slug (e.g. 'stripe').
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting objects.

    Raises:
        requests.HTTPError: On API request failure.
    """
    url = BASE.format(slug=slug)
    resp = requests.get(url, params={"content": "true"}, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    postings: list[Posting] = []
    for job in data.get("jobs", []):
        loc = (job.get("location") or {}).get("name", "")
        # content is HTML-escaped HTML; unescape once,
        # schema._clean strips tags
        desc = html.unescape(job.get("content", ""))
        postings.append(
            normalize(
                source="greenhouse",
                company=slug,
                external_id=job.get("id"),
                title=job.get("title", ""),
                location=loc,
                description=desc,
                url=job.get("absolute_url", ""),
                posted_at=job.get("updated_at"),
            )
        )
    time.sleep(0.5)  # be polite
    return postings
