"""iCIMS adapter.

Modern iCIMS career sites are fronted by Jibe (an iCIMS-owned career-site
platform) which exposes a public JSON jobs API -- no auth -- at
    https://{host}/api/jobs?page={n}
`page` is 1-indexed and the response carries a page of jobs with descriptions
inline, so parsing is the stdlib `json` only (no HTML scrape, no per-job
second round-trip). The shape was confirmed live by the Phase 0 recon spike
(specs/003-icims-adapter/research.md): top-level `jobs` (list, ~10/page),
`totalCount`, and each list item wrapped as `{"data": {...job...}}`.

A single compound slug `tenant[:host]` keeps the two-phase adapter contract
(see `job_agent.resilient`) scoped per tenant; this module splits the slug
internally. `host` defaults to `{tenant}.icims.com` when omitted, and is
given explicitly for the common custom-domain case (e.g.
"hooli:careers.hooli.com").

Unlike Workday, iCIMS returns the full job description inline in the listing
JSON, so this adapter is "already described":
  1. `list_postings(slug, *, company=...) -> list[Posting]` walks
     `GET .../api/jobs?page={n}`, filtering to US postings and excluding any
     posting whose inline description is empty/blank, returning fully
     -populated `Posting`s with zero detail round-trips. A failure on the
     first page raises (whole-source failure); a failure after at least one
     page has already succeeded is logged and stops pagination, retaining
     the pages already fetched (FR-005/006).
  2. `fetch_description(posting, ...) -> str` is a pure pass-through to the
     `description` already inline on the stub -- no network call -- present
     only so the shared two-phase contract is uniform across vendors;
     `resilient.run_source` short-circuits on it.

US scoping is deterministic on each job's `country_code`: keep `US`, drop a
code positively identified as non-US, and retain an empty/missing code
(FR-004 keep-if-any -- over-include, let LLM scoring resolve the bleed).

The display company name is the caller-supplied `company` kwarg (resolved
upstream from `registry.toml`); it falls back to the tenant slug when
absent, since company feeds the dedupe fingerprint (schema.py) and must
never be empty.

`requests` and `time` are imported at module level so tests can monkeypatch
`icims.requests` / `icims.time.sleep`.
"""

from __future__ import annotations

import sys
import time

import requests
from requests import RequestException

from ..schema import Posting, normalize

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}
PAGE_SIZE = 10  # the Jibe /api/jobs API returns 10 jobs per page


def _split_slug(slug: str) -> tuple[str, str]:
    """Split an iCIMS slug into (tenant, host).

    Args:
        slug: Either a bare `tenant` or a colon-delimited `tenant:host`
            (e.g. "hooli:careers.hooli.com").

    Returns:
        A (tenant, host) tuple. When `slug` carries no host, `host` defaults
        to `{tenant}.icims.com`.

    Raises:
        ValueError: If `slug` has more than two colon-delimited parts.
    """
    parts = slug.split(":")
    if len(parts) == 1:
        return parts[0], f"{parts[0]}.icims.com"
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"malformed icims slug {slug!r}; expected tenant or tenant:host")


def _is_us(country_code: str) -> bool:
    """Deterministic US gate keyed on the job's ISO-2 country code.

    Keep US and keep empty/ambiguous (retain rather than falsely reject,
    FR-004); drop only a code positively identified as non-US.

    Args:
        country_code: The job's `country_code` field (may be empty).

    Returns:
        True to keep the posting, False to drop it before scoring.
    """
    if not country_code:
        return True
    return country_code.strip().upper() == "US"


def list_postings(slug: str, *, company: str | None = None, timeout: int = 20) -> list[Posting]:
    """List all open US postings for one iCIMS (Jibe) tenant.

    Descriptions arrive inline in the listing response, so each returned
    `Posting` is fully populated -- no separate detail fetch is needed.

    Args:
        slug: Compound `tenant[:host]` slug (e.g. "hooli:careers.hooli.com").
        company: Display company name; defaults to the tenant slug when
            absent.
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting objects (US-only, non-empty description).
        A page failure after at least one page has already succeeded stops
        pagination but retains the postings already collected (FR-005/006).

    Raises:
        ValueError: If `slug` is malformed.
        requests.HTTPError: On a first-page request failure (contained by
            the caller in fetch.py so one bad tenant does not abort the
            run).
    """
    tenant, host = _split_slug(slug)
    company = company or tenant
    base = f"https://{host}"

    postings: list[Posting] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{base}/api/jobs", params={"page": page}, headers=HEADERS, timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except (RequestException, ValueError):
            if page == 1:
                raise  # nothing has succeeded yet -> whole-source failure
            print(
                f"  ! icims/{slug} page {page} failed; keeping {len(postings)} prior postings",
                file=sys.stderr,
            )
            break

        raw = data.get("jobs", [])
        if not raw:
            break

        for item in raw:
            job = item.get("data") or {}
            if not _is_us(job.get("country_code", "")):
                continue
            posting = normalize(
                source="icims",
                company=company,
                external_id=job.get("req_id", ""),
                title=job.get("title", ""),
                location=job.get("full_location") or job.get("location_name", ""),
                description=job.get("description", ""),
                url=job.get("apply_url", ""),
                posted_at=job.get("posted_date"),
            )
            if not posting.description:
                continue  # scorer needs description text -> exclude, don't score empty
            postings.append(posting)

        if page * PAGE_SIZE >= data.get("totalCount", 0):
            break
        page += 1
        time.sleep(0.5)  # be polite

    return postings


def fetch_description(posting: Posting, *, timeout: int = 20) -> str:
    """Return the job description already inline on a posting stub.

    iCIMS listing pages carry the full description, so this is a pure
    pass-through with zero network calls; it exists only to satisfy the
    two-phase contract shared with vendors that need a separate detail
    fetch (`job_agent.resilient` short-circuits on the inline value).

    Args:
        posting: A stub returned by `list_postings`; its `description` is
            already complete.
        timeout: Unused; present for contract parity with other adapters.

    Returns:
        The posting's existing `description`.
    """
    return posting.description
