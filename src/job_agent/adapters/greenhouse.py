"""Greenhouse adapter.

Public board API, no auth required:
    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

The ?content=true flag returns the full job description (HTML-escaped) inline,
which saves a second round-trip per job.
"""
from __future__ import annotations

import html
import time
from typing import List

import requests

from ..schema import Posting, normalize

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}


def fetch(slug: str, *, timeout: int = 20) -> List[Posting]:
    """Return all open postings for one Greenhouse board."""
    url = BASE.format(slug=slug)
    resp = requests.get(
        url, params={"content": "true"}, headers=HEADERS, timeout=timeout
    )
    resp.raise_for_status()
    data = resp.json()

    postings: List[Posting] = []
    for job in data.get("jobs", []):
        loc = (job.get("location") or {}).get("name", "")
        # content is HTML-escaped HTML; unescape once, schema._clean strips tags
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
