"""Workday adapter.

Workday CXS (Career Site eXperience Service) is reached at
    https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

A single compound slug `tenant:site:host` (e.g. "chrobinson:CHRobinson:wd5")
keeps `load_registry` and the `fetch(slug) -> list[Posting]` adapter contract
unchanged; this module splits the slug internally.

Two round-trips per posting:
  1. `POST .../wday/cxs/{tenant}/{site}/jobs` (paginated via limit/offset on
     a `total` the API reports) returns title/externalPath/locationsText/
     postedOn for a page of jobs, scoped to a US country facet.
  2. `GET .../wday/cxs/{tenant}/{site}{externalPath}` returns the full
     per-job description.

Company display names are resolved from a git-ignored `companies.toml`
(`[display_names]` tenant -> name), loaded via `store.data_path` the same
way `filter.py` loads `filter.toml`; missing file or missing mapping falls
back open to the tenant slug itself, since company feeds the dedupe
fingerprint (schema.py) and must never be empty.

`requests` and `time` are imported at module level so tests can monkeypatch
`workday.requests` / `workday.time.sleep`.
"""

from __future__ import annotations

import os
import time
import tomllib

import requests

from .. import store
from ..schema import Posting, normalize

# Scope the jobs POST body's appliedFacets to US postings. Both the facet
# key and the USA country WID are placeholders -- verify them live against
# the chrobinson tenant before relying on faceted results.
USA_COUNTRY_FACET_KEY = "locationCountry"
USA_COUNTRY_WID = "bc33aa3152ec42d4995f4791a106ed09"

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}
PAGE_LIMIT = 20


def _split_slug(slug: str) -> tuple[str, str, str]:
    """Split a compound Workday slug into (tenant, site, host).

    Args:
        slug: Colon-delimited `tenant:site:host` (e.g.
            "chrobinson:CHRobinson:wd5").

    Returns:
        A (tenant, site, host) tuple.

    Raises:
        ValueError: If `slug` does not have exactly three colon-delimited
            parts.
    """
    parts = slug.split(":")
    if len(parts) != 3:
        raise ValueError(f"malformed workday slug {slug!r}; expected tenant:site:host")
    tenant, site, host = parts
    return tenant, site, host


def _resolve_company(tenant: str) -> str:
    """Resolve a tenant slug to a display name via `companies.toml`.

    Args:
        tenant: The Workday tenant slug (e.g. "chrobinson").

    Returns:
        The mapped display name from `companies.toml`'s `[display_names]`
        table, or `tenant` itself if the file is missing or has no mapping
        for this tenant (fail-open).
    """
    path = store.data_path("companies.toml")
    if not os.path.exists(path):
        return tenant
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("display_names", {}).get(tenant, tenant)


def fetch(slug: str, *, timeout: int = 20) -> list[Posting]:
    """Fetch all open US postings for one Workday tenant/site.

    Args:
        slug: Compound `tenant:site:host` slug (e.g.
            "chrobinson:CHRobinson:wd5").
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting objects.

    Raises:
        ValueError: If `slug` is malformed.
        requests.HTTPError: On API request failure.
    """
    tenant, site, host = _split_slug(slug)
    company = _resolve_company(tenant)
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    cap_raw = os.environ.get("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER")
    cap = int(cap_raw) if cap_raw else None

    raw_jobs: list[dict] = []
    offset = 0
    while True:
        body = {
            "offset": offset,
            "limit": PAGE_LIMIT,
            "appliedFacets": {USA_COUNTRY_FACET_KEY: [USA_COUNTRY_WID]},
        }
        resp = requests.post(f"{base}/jobs", json=body, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("jobPostings", [])
        if not page:
            break
        raw_jobs.extend(page)
        if cap is not None and len(raw_jobs) >= cap:
            break
        offset += PAGE_LIMIT
        if offset >= data.get("total", 0):
            break

    if cap is not None:
        raw_jobs = raw_jobs[:cap]

    postings: list[Posting] = []
    for job in raw_jobs:
        external_path = job.get("externalPath", "")
        detail_resp = requests.get(f"{base}{external_path}", headers=HEADERS, timeout=timeout)
        detail_resp.raise_for_status()
        detail = detail_resp.json()
        description = detail.get("jobPostingInfo", {}).get("jobDescription", "")
        postings.append(
            normalize(
                source="workday",
                company=company,
                external_id=external_path,
                title=job.get("title", ""),
                location=job.get("locationsText", ""),
                description=description,
                url=f"{base}{external_path}",
                posted_at=job.get("postedOn"),
            )
        )
        time.sleep(0.5)  # be polite

    return postings
