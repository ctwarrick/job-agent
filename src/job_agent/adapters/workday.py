"""Workday adapter.

Workday CXS (Career Site eXperience Service) is reached at
    https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

A single compound slug `tenant:site:host` (e.g. "globex:Globex:wd5")
keeps the adapter scoped per tenant/site; this module splits the slug
internally.

Two-phase contract (see `job_agent.resilient`, which calls these lazily so
filtering happens before any detail fetch is paid for):
  1. `list_postings(slug, *, company=...) -> list[Posting]` walks
     `POST .../wday/cxs/{tenant}/{site}/jobs` (paginated via limit/offset; the
     `total` is read once from the first page, since Workday echoes 0 on later
     pages, and a short/empty page also ends the walk), returning title/
     externalPath/locationsText/postedOn stubs scoped to a US country facet
     (dropped and retried facet-free when a tenant 400s it), with an empty
     `description` and zero detail GETs.
  2. `fetch_description(posting, ...) -> str` issues one
     `GET .../wday/cxs/{tenant}/{site}{externalPath}` for a single stub
     and returns its full job description.

The display company name is the caller-supplied `company` kwarg (resolved
upstream from `registry.toml`); it falls back to the tenant slug itself
when absent, since company feeds the dedupe fingerprint (schema.py) and
must never be empty.

`requests` and `time` are imported at module level so tests can monkeypatch
`workday.requests` / `workday.time.sleep`.
"""

from __future__ import annotations

import sys
import time

import requests
from requests import RequestException

from ..schema import Posting, normalize

# Scope the jobs POST body's appliedFacets to US postings. Both the facet
# key and the USA country WID are placeholders -- verify them live against
# the globex tenant before relying on faceted results.
USA_COUNTRY_FACET_KEY = "locationCountry"
USA_COUNTRY_WID = "bc33aa3152ec42d4995f4791a106ed09"

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}
PAGE_LIMIT = 20


def _is_bad_request(exc: Exception) -> bool:
    """Report whether `exc` is an HTTP 400 Bad Request.

    Used to detect the one recoverable listing failure: a tenant that does not
    expose the US country facet rejects any request applying it with a 400 (an
    unparseable body raises ValueError, with no response, and is not a 400).

    Args:
        exc: The exception raised while POSTing a jobs page.

    Returns:
        True if `exc` carries a response whose status code is 400.
    """
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) == 400


def _split_slug(slug: str) -> tuple[str, str, str]:
    """Split a compound Workday slug into (tenant, site, host).

    Args:
        slug: Colon-delimited `tenant:site:host` (e.g.
            "globex:Globex:wd5").

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


def list_postings(slug: str, *, company: str | None = None, timeout: int = 20) -> list[Posting]:
    """List all open US posting stubs for one Workday tenant/site.

    Issues zero detail GETs; `description` is left empty for the caller to
    fill in lazily via `fetch_description`.

    Args:
        slug: Compound `tenant:site:host` slug (e.g.
            "globex:Globex:wd5").
        company: Display company name; defaults to the tenant slug when
            absent.
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting stubs with empty `description`.

    Raises:
        ValueError: If `slug` is malformed.
        requests.HTTPError: On API request failure.
    """
    tenant, site, host = _split_slug(slug)
    company = company or tenant
    base = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"

    raw_jobs: list[dict] = []
    offset = 0
    # Workday reports a meaningful `total` only on the FIRST page; later pages
    # echo 0 while still serving full pages, so we capture it once and stop on
    # that count (or a short/empty page), never on a later page's bogus 0.
    total: int | None = None
    # Scope the listing to US postings server-side. Some tenants don't expose
    # this facet and 400 any request applying it; on that 400 we drop the facet
    # once and retry facet-free (the downstream location filter still scopes to
    # the US, so this only costs extra list pages, not detail fetches).
    applied_facets: dict[str, list[str]] = {USA_COUNTRY_FACET_KEY: [USA_COUNTRY_WID]}
    while True:
        body = {"offset": offset, "limit": PAGE_LIMIT, "appliedFacets": applied_facets}
        try:
            resp = requests.post(f"{base}/jobs", json=body, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except (RequestException, ValueError) as exc:
            if applied_facets and not raw_jobs and _is_bad_request(exc):
                applied_facets = {}  # tenant rejects the facet; retry this page
                continue
            if not raw_jobs:
                raise  # nothing has succeeded yet -> whole-source failure
            print(
                f"  ! workday/{slug} offset {offset} failed; "
                f"keeping {len(raw_jobs)} prior postings",
                file=sys.stderr,
            )
            break

        page = data.get("jobPostings", [])
        if not page:
            break
        raw_jobs.extend(page)
        if total is None:
            total = data.get("total", 0)
        offset += PAGE_LIMIT
        if offset >= total or len(page) < PAGE_LIMIT:
            break

    postings: list[Posting] = []
    for job in raw_jobs:
        external_path = job.get("externalPath", "")
        postings.append(
            normalize(
                source="workday",
                company=company,
                external_id=external_path,
                title=job.get("title", ""),
                location=job.get("locationsText", ""),
                description="",
                url=f"{base}{external_path}",
                posted_at=job.get("postedOn"),
            )
        )

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
        requests.HTTPError: On API request failure.
    """
    resp = requests.get(posting.url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    detail = resp.json()
    time.sleep(0.5)  # be polite
    return detail.get("jobPostingInfo", {}).get("jobDescription", "")
