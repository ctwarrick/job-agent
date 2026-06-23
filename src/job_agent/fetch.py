"""Fetch orchestrator.

Reads registry.toml, dispatches each resolved source to the right ATS
adapter, normalizes, and upserts into SQLite. Adding a new ATS means
writing an adapter with a fetch(slug, *, company=...) -> list[Posting]
signature and registering it in ADAPTERS.
"""

from __future__ import annotations

import sys

from . import store
from .adapters import greenhouse, icims, lever, workday
from .registry import load_registry

ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "workday": workday.fetch,
    "icims": icims.fetch,
}


def main() -> list[dict]:
    """Fetch every registered source, upserting what succeeds.

    A per-source failure (an adapter raising, or a registry vendor with no
    adapter) is non-fatal (FR-005): the source is logged to stderr for
    after-the-fact diagnosis and collected as a structured record, and the run
    continues to the next source. The records are returned so the orchestrator
    can surface the degradation in the digest and on the run row -- the raw
    error text stays in the logs and out of the returned record's caller-facing
    uses (FR-007).

    Returns:
        A list of {source, company_slug, error} dicts, one per failed source;
        empty when every source fetched cleanly.
    """
    store.init()
    total_new = 0
    failures: list[dict] = []
    for source in load_registry():
        fetch = ADAPTERS.get(source.vendor)
        if fetch is None:
            error = f"no adapter for vendor {source.vendor!r}"
            print(f"  ! {error} (slug {source.slug})", file=sys.stderr)
            failures.append({"source": source.vendor, "company_slug": source.slug, "error": error})
            continue
        try:
            postings = fetch(source.slug, company=source.company)
            added = store.upsert_postings(postings)
            total_new += added
            print(f"  {source.vendor:12} {source.slug:20} {len(postings):4} fetched, {added:4} new")
        except Exception as e:  # one bad board shouldn't kill the run
            print(f"  ! {source.vendor}/{source.slug} failed: {e}", file=sys.stderr)
            failures.append({"source": source.vendor, "company_slug": source.slug, "error": str(e)})
    print(f"\nDone. {total_new} new postings added.")
    return failures


def _cli() -> None:
    """Console-script entry point (jobagent-fetch).

    Runs the stage and discards main()'s return value (the per-source failure
    records, which exist for in-process orchestration by main.py) so a
    successful run exits 0: the hatchling wrapper does ``sys.exit(main())`` and
    ``sys.exit()`` of a non-None, non-int object exits 1.
    """
    main()


if __name__ == "__main__":
    _cli()
