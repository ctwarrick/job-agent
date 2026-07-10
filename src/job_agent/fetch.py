"""Fetch orchestrator.

Reads registry.toml, dispatches each resolved source to the right ATS adapter,
normalizes, and upserts into SQLite.

Two adapter shapes coexist:

  - Single-request adapters (Greenhouse, Lever) expose
    ``fetch(slug, *, company=...) -> list[Posting]`` and fetch a whole board,
    descriptions inline, in one call. They are dispatched directly here.
  - Two-phase adapters (the vendors in ``RESILIENT_VENDORS`` -- Workday, and
    later iCIMS/Talemetry) expose ``list_postings`` + ``fetch_description`` and
    are driven by ``resilient.run_source``, which lists cheaply, filters before
    spending on descriptions, bounds the work with a backstop, and makes
    forward progress across runs (see specs/006-resilient-fetch).

main() returns a ``(failed_sources, partial_sources)`` tuple: wholly-unreachable
sources (existing per-source containment) and sources that were only partially
fetched (per-item skips, backstop truncation, or persistent staleness), the
latter surfaced as the digest's degraded category (FR-014).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Callable

from . import resilient, store
from .adapters import greenhouse, icims, lever, talemetry, workday
from .filter import load_criteria
from .registry import load_registry

# Vendors whose adapters use the two-phase contract and are driven through
# resilient.run_source rather than a direct fetch().
RESILIENT_VENDORS = {"workday", "icims", "talemetry"}

# Vendor -> dispatch target. Out-of-scope vendors map to their fetch callable;
# resilient vendors map to the adapter module (passed to resilient.run_source).
# All supported vendors must appear here: registry.load_registry validates a
# source's vendor against these keys.
ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "workday": workday,
    "icims": icims,
    "talemetry": talemetry,
}


def fetch_concurrency() -> int:
    """Max number of sources fetched in parallel.

    Returns:
        `JOBAGENT_FETCH_CONCURRENCY` (default 8).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return resilient._positive_int_env("JOBAGENT_FETCH_CONCURRENCY", 8)


def fetch_budget_seconds() -> int:
    """Wall-clock budget, in seconds, for the whole fetch stage.

    Returns:
        `JOBAGENT_FETCH_BUDGET_SECONDS` (default 5400).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return resilient._positive_int_env("JOBAGENT_FETCH_BUDGET_SECONDS", 5400)


def score_digest_headroom_seconds() -> int:
    """Wall-clock headroom, in seconds, reserved for score + digest.

    Returns:
        `JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS` (default 1800).

    Raises:
        ValueError: If set but not a positive integer.
    """
    return resilient._positive_int_env("JOBAGENT_SCORE_DIGEST_HEADROOM_SECONDS", 1800)


def execution_window_seconds() -> int:
    """Total wall-clock window, in seconds, the platform allows the run.

    Required, with no code default: production Bicep always sets
    `JOBAGENT_EXECUTION_WINDOW_SECONDS` to the same value it passes to the
    platform as the replica timeout, so the app validates against the exact
    deadline the platform enforces (single source of truth). An unset value
    means either a misconfigured deployment or a dev run that must opt in
    explicitly -- both should fail loud rather than silently picking a
    window the platform doesn't actually honor.

    Returns:
        `JOBAGENT_EXECUTION_WINDOW_SECONDS`.

    Raises:
        SystemExit: If the variable is unset.
        ValueError: If set but not a positive integer.
    """
    if "JOBAGENT_EXECUTION_WINDOW_SECONDS" not in os.environ:
        sys.exit(
            "config: JOBAGENT_EXECUTION_WINDOW_SECONDS is required (no code default) -- "
            "set it explicitly, or deploy via infra/main.bicep which pins it to "
            "replicaTimeoutSeconds"
        )
    # Default is unreachable (env presence checked above); kept for a single
    # fail-loud parse path shared with the other getters.
    return resilient._positive_int_env("JOBAGENT_EXECUTION_WINDOW_SECONDS", 0)


def main(clock: Callable[[], float] = time.monotonic) -> tuple[list[dict], list[dict]]:
    """Fetch every registered source, upserting what succeeds.

    Boards are dispatched in `store.sources_by_recency` order (least-recently-
    fully-fetched first -- FR-006), computed once up front, so the oldest or
    never-fetched board is served first across the whole registry.

    A per-source failure (an adapter raising, a resilient source whose listing
    failed, or a registry vendor with no adapter) is non-fatal (FR-006): it is
    logged to stderr, collected as a structured record, and the run continues.
    A resilient source that was only partially fetched (skips, backstop
    truncation, or persistent staleness) is collected separately so the digest
    can flag it as degraded (FR-014). Raw error text stays in the logs, out of
    the returned records' caller-facing uses (Principle VI).

    A successful whole-board fetch (greenhouse/lever, always fully fetched in
    one call) also marks the source converged, extending 006's bookkeeping to
    single-request vendors so the recency ordering is total (data-model.md R4).

    A stage-wide wall-clock budget (`fetch_budget_seconds()`) bounds the whole
    loop (FR-003/R5): once `clock()` passes the once-computed `stage_deadline`,
    no further board is submitted. Boards never dispatched are reported as
    `partial_sources` entries with `reason="budget_deferred"` and do NOT get
    marked converged, so they sort first on the next run (no starvation --
    SC-003). A dispatched resilient board is given `stage_deadline` so its own
    per-source deadline is clamped to whichever is nearer (FR-009).

    The deterministic filter criteria are loaded lazily, only when a resilient
    source is actually dispatched, so a run with no two-phase sources needs no
    filter.toml; a missing/invalid criteria file then fails loud (Principle V).

    Args:
        clock: Monotonic clock used for the stage budget; overridable for
            tests (default `time.monotonic`).

    Returns:
        A ``(failed_sources, partial_sources)`` tuple. ``failed_sources`` is a
        list of {source, company_slug, error} dicts; ``partial_sources`` is a
        list of {source, company_slug, new, skipped, truncated, persistent}
        dicts, or {source, company_slug, reason} for a budget-deferred board.
        Both are empty when every source fetched cleanly.
    """
    store.init()
    total_new = 0
    failed: list[dict] = []
    partial: list[dict] = []
    criteria = None

    sources = load_registry()
    # Registry uniqueness is (vendor, slug), not (vendor, company): a company
    # can legitimately run two boards (e.g. two Workday sites under one
    # tenant) sharing (vendor, company). Dedup only the ordering *keys* --
    # never the Source objects themselves, or a shared-key board is silently
    # dropped. `dict.fromkeys` dedups while preserving first-seen registry
    # order, so `sources_by_recency` still receives keys in registry order
    # (ties stay registry-order-stable); `sorted` on `rank` is itself stable,
    # so two Sources sharing a key dispatch adjacently in registry order.
    unique_keys = list(dict.fromkeys((s.vendor, s.company) for s in sources))
    ordered_keys = store.sources_by_recency(unique_keys)
    rank = {key: i for i, key in enumerate(ordered_keys)}
    ordered_sources = sorted(sources, key=lambda s: rank[(s.vendor, s.company)])
    stage_deadline = clock() + fetch_budget_seconds()

    for i, source in enumerate(ordered_sources):
        if clock() > stage_deadline:
            for deferred in ordered_sources[i:]:
                partial.append(
                    {
                        "source": deferred.vendor,
                        "company_slug": deferred.slug,
                        "reason": "budget_deferred",
                    }
                )
            break

        adapter = ADAPTERS.get(source.vendor)
        if adapter is None:
            error = f"no adapter for vendor {source.vendor!r}"
            print(f"  ! {error} (slug {source.slug})", file=sys.stderr)
            failed.append({"source": source.vendor, "company_slug": source.slug, "error": error})
            continue
        if source.vendor in RESILIENT_VENDORS:
            if criteria is None:
                criteria = load_criteria()
            result = resilient.run_source(
                adapter, source, criteria=criteria, stage_deadline=stage_deadline
            )
            if result.error:
                print(f"  ! {source.vendor}/{source.slug} failed: {result.error}", file=sys.stderr)
                failed.append(
                    {
                        "source": result.source,
                        "company_slug": result.company_slug,
                        "error": result.error,
                    }
                )
                continue
            total_new += result.new
            if result.skipped or result.truncated or result.persistent:
                partial.append(
                    {
                        "source": result.source,
                        "company_slug": result.company_slug,
                        "new": result.new,
                        "skipped": result.skipped,
                        "truncated": result.truncated,
                        "persistent": result.persistent,
                    }
                )
            flags = []
            if result.truncated:
                flags.append("truncated")
            if result.skipped:
                flags.append(f"{result.skipped} skipped")
            if result.persistent:
                flags.append("persistent")
            note = f" ({', '.join(flags)})" if flags else ""
            print(f"  {source.vendor:12} {source.slug:20} {result.new:4} new{note}")
        else:
            try:
                postings = adapter(source.slug, company=source.company)
                added = store.upsert_postings(postings)
                total_new += added
                store.mark_converged(
                    source.vendor, source.company, datetime.now(timezone.utc).isoformat()
                )
                print(
                    f"  {source.vendor:12} {source.slug:20} "
                    f"{len(postings):4} fetched, {added:4} new"
                )
            except Exception as e:  # one bad board shouldn't kill the run
                print(f"  ! {source.vendor}/{source.slug} failed: {e}", file=sys.stderr)
                failed.append(
                    {"source": source.vendor, "company_slug": source.slug, "error": str(e)}
                )
    print(f"\nDone. {total_new} new postings added.")
    return failed, partial


def _cli() -> None:
    """Console-script entry point (jobagent-fetch).

    Runs the stage and discards main()'s return value (the (failed, partial)
    records, which exist for in-process orchestration by main.py) so a
    successful run exits 0: the hatchling wrapper does ``sys.exit(main())`` and
    ``sys.exit()`` of a non-None, non-int object exits 1.
    """
    main()


if __name__ == "__main__":
    _cli()
