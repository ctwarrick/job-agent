"""iCIMS adapter.

Modern iCIMS career sites are fronted by Jibe (an iCIMS-owned career-site
platform) which exposes a public JSON jobs API -- no auth -- at
    https://{host}/api/jobs?page={n}
`page` is 1-indexed and the response carries a page of jobs with descriptions
inline, so parsing is the stdlib `json` only (no HTML scrape, no per-job
second round-trip). The shape was confirmed live by the Phase 0 recon spike
(specs/003-icims-adapter/research.md): top-level `jobs` (list, ~10/page),
`totalCount`, and each list item wrapped as `{"data": {...job...}}`.

A single compound slug `tenant[:host]` keeps `load_registry` and the
`fetch(slug) -> list[Posting]` adapter contract unchanged; this module splits
the slug internally. `host` defaults to `{tenant}.icims.com` when omitted, and
is given explicitly for the common custom-domain case (e.g.
"sig:careers.sig.com").

US scoping is deterministic on each job's `country_code`: keep `US`, drop a
code positively identified as non-US, and retain an empty/missing code
(FR-004 keep-if-any -- over-include, let LLM scoring resolve the bleed).

Company display names resolve from a git-ignored `companies.toml`
(`[display_names]` tenant -> name) exactly as the Workday adapter does;
a missing file or mapping falls back open to the tenant slug, since company
feeds the dedupe fingerprint (schema.py) and must never be empty.

`requests` and `time` are imported at module level so tests can monkeypatch
`icims.requests` / `icims.time.sleep`.
"""

from __future__ import annotations

import os
import time
import tomllib

import requests

from .. import store
from ..schema import Posting, normalize

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search)"}
PAGE_SIZE = 10  # the Jibe /api/jobs API returns 10 jobs per page


def _split_slug(slug: str) -> tuple[str, str]:
    """Split an iCIMS slug into (tenant, host).

    Args:
        slug: Either a bare `tenant` or a colon-delimited `tenant:host`
            (e.g. "sig:careers.sig.com").

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


def _resolve_company(tenant: str) -> str:
    """Resolve a tenant slug to a display name via `companies.toml`.

    Args:
        tenant: The iCIMS tenant slug (e.g. "sig").

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


def fetch(slug: str, *, timeout: int = 20) -> list[Posting]:
    """Fetch all open US postings for one iCIMS (Jibe) tenant.

    Args:
        slug: Compound `tenant[:host]` slug (e.g. "sig:careers.sig.com").
        timeout: Request timeout in seconds (default 20).

    Returns:
        List of normalized Posting objects (US-only).

    Raises:
        ValueError: If `slug` is malformed.
        requests.HTTPError: On API request failure (contained by the caller
            in fetch.py so one bad tenant does not abort the run).
    """
    tenant, host = _split_slug(slug)
    company = _resolve_company(tenant)
    base = f"https://{host}"

    cap_raw = os.environ.get("JOBAGENT_MAX_POSTINGS_PER_EMPLOYER")
    cap = int(cap_raw) if cap_raw else None

    postings: list[Posting] = []
    page = 1
    while True:
        resp = requests.get(
            f"{base}/api/jobs", params={"page": page}, headers=HEADERS, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("jobs", [])
        if not raw:
            break

        for item in raw:
            job = item.get("data") or {}
            if not _is_us(job.get("country_code", "")):
                continue
            postings.append(
                normalize(
                    source="icims",
                    company=company,
                    external_id=job.get("req_id", ""),
                    title=job.get("title", ""),
                    location=job.get("full_location") or job.get("location_name", ""),
                    description=job.get("description", ""),
                    url=job.get("apply_url", ""),
                    posted_at=job.get("posted_date"),
                )
            )
            if cap is not None and len(postings) >= cap:
                return postings[:cap]  # cap reached -> stop, do not page on

        if page * PAGE_SIZE >= data.get("totalCount", 0):
            break
        page += 1
        time.sleep(0.5)  # be polite

    return postings
