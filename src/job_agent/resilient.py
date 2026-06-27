"""Resilient per-source fetch loop with cap/deadline/forward-progress backstops.

A single ATS source can return thousands of stubs; fetching a detail page per
stub is the expensive part. `run_source` runs the two-phase adapter contract
(`list_postings` then `fetch_description`) with three independent backstops
so one huge or slow board cannot starve the rest of the pipeline or run past
its budget:

  1. A per-source cap on detail fetches (`JOBAGENT_MAX_DETAIL_PER_SOURCE`).
  2. A wall-clock deadline per source (`JOBAGENT_FETCH_DEADLINE_SECONDS`).
  3. Forward progress: postings already in the store are never re-fetched,
     so a truncated run still converges over repeated invocations.

Filtering (via `job_agent.filter.classify`) happens before any detail fetch,
so rejected postings never cost a network round-trip. A source that never
fully catches up (truncated for longer than `JOBAGENT_STALENESS_BOUND_DAYS`)
is reported with `persistent=True` so callers can surface it as a standing
problem rather than routine backlog.

Data contract: `run_source(adapter, source, *, criteria, store_, clock, now)
-> SourceResult` where `adapter` exposes `list_postings(slug, *, company)`
and `fetch_description(posting)`, `source` exposes `.vendor`, `.slug`,
`.company`, and `store_` exposes the `job_agent.store` module surface
(`existing_external_ids`, `upsert_postings`, `get_last_converged`,
`mark_converged`, `seed_source`).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import store
from .filter import classify
from .schema import normalize


@dataclass
class SourceResult:
    """Outcome of one `run_source` call for a single registry source.

    Attributes:
        source: Adapter vendor name (e.g. "workday").
        company_slug: The registry slug for this source's company.
        new: Number of postings successfully described and upserted.
        skipped: Number of survivors whose detail fetch failed.
        truncated: True if the cap or deadline backstop cut the run short.
        remaining: Count of survivors not yet described in this run.
        persistent: True if remaining > 0 and the source has been stale
            (never converged) for longer than the staleness bound.
        error: Set, with no other work done, if `list_postings` raised.
    """

    source: str
    company_slug: str
    new: int = 0
    skipped: int = 0
    truncated: bool = False
    remaining: int = 0
    persistent: bool = False
    error: str | None = None


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive-int env var, falling back to `default` if unset.

    Args:
        name: Environment variable name.
        default: Value to use when the variable is unset.

    Returns:
        The parsed positive integer.

    Raises:
        ValueError: If the variable is set but not a positive integer.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def max_detail_per_source() -> int:
    """Per-source cap on detail fetches.

    Returns:
        `JOBAGENT_MAX_DETAIL_PER_SOURCE` (default 150).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return _positive_int_env("JOBAGENT_MAX_DETAIL_PER_SOURCE", 150)


def fetch_deadline_seconds() -> int:
    """Per-source wall-clock deadline, in seconds.

    Returns:
        `JOBAGENT_FETCH_DEADLINE_SECONDS` (default 300).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return _positive_int_env("JOBAGENT_FETCH_DEADLINE_SECONDS", 300)


def staleness_bound_days() -> int:
    """Days a source may remain unconverged before being flagged persistent.

    Returns:
        `JOBAGENT_STALENESS_BOUND_DAYS` (default 7).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return _positive_int_env("JOBAGENT_STALENESS_BOUND_DAYS", 7)


def _parse_converged_at(when: str) -> datetime:
    """Parse a stored convergence timestamp, assuming UTC if naive.

    Args:
        when: An ISO-8601 timestamp string from the store.

    Returns:
        A timezone-aware `datetime` in UTC.
    """
    parsed = datetime.fromisoformat(when)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_source(
    adapter: Any,
    source: Any,
    *,
    criteria: Any,
    store_: Any = store,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SourceResult:
    """Run the two-phase fetch (list, then filtered detail) for one source.

    Args:
        adapter: Object exposing `list_postings(slug, *, company)` and
            `fetch_description(posting)`.
        source: Object exposing `.vendor`, `.slug`, `.company`.
        criteria: Passed through to `filter.classify` to reject postings
            before any detail fetch.
        store_: The store module (or a stub) to read/write bookkeeping.
        clock: Monotonic clock used for the deadline backstop.
        now: Wall-clock timestamp provider for convergence bookkeeping.

    Returns:
        A `SourceResult` summarizing what happened.
    """
    try:
        stubs = adapter.list_postings(source.slug, company=source.company)
    except Exception as exc:  # noqa: BLE001 - any adapter failure is non-fatal
        print(
            f"resilient: list_postings failed for {source.vendor}/{source.slug}: {exc}",
            file=sys.stderr,
        )
        return SourceResult(source.vendor, source.slug, error=str(exc))

    if store_.get_last_converged(source.vendor, source.company) is None:
        store_.seed_source(source.vendor, source.company, now().isoformat())

    survivors = [
        s
        for s in stubs
        if classify({"title": s.title, "location": s.location, "posted_at": s.posted_at}, criteria)
        is None
    ]

    already = store_.existing_external_ids(source.vendor, source.company)
    todo = [s for s in survivors if s.external_id not in already]

    cap = max_detail_per_source()
    deadline = clock() + fetch_deadline_seconds()
    described = []
    skipped = 0
    truncated = False
    for s in todo:
        # Evaluate both backstops on FRESH wall-clock every iteration, before the
        # fetch, so the deadline fires whether or not the previous detail fetch
        # succeeded -- a board whose detail pages all time out must still be
        # bounded (FR-013/SC-008), not loop until the run is killed.
        if len(described) >= cap or clock() > deadline:
            truncated = True
            break
        try:
            desc = s.description or adapter.fetch_description(s)
        except Exception as exc:  # noqa: BLE001 - per-item failure is non-fatal
            skipped += 1
            print(
                f"resilient: detail fetch failed for {source.vendor}/{s.external_id}: {exc}",
                file=sys.stderr,
            )
            continue
        described.append(
            normalize(
                source=s.source,
                company=s.company,
                external_id=s.external_id,
                title=s.title,
                location=s.location,
                description=desc,
                url=s.url,
                posted_at=s.posted_at,
            )
        )

    store_.upsert_postings(described)

    remaining = len(todo) - len(described)
    if remaining == 0:
        store_.mark_converged(source.vendor, source.company, now().isoformat())
        persistent = False
    else:
        last = store_.get_last_converged(source.vendor, source.company)
        persistent = last is not None and (
            now() - _parse_converged_at(last) > timedelta(days=staleness_bound_days())
        )

    return SourceResult(
        source.vendor,
        source.slug,
        new=len(described),
        skipped=skipped,
        truncated=truncated,
        remaining=remaining,
        persistent=persistent,
    )
