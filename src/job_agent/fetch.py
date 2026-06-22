"""Fetch orchestrator.

Reads registry.txt, dispatches each company to the right ATS adapter,
normalizes, and upserts into SQLite. Adding a new ATS means writing an
adapter with a fetch(slug) -> list[Posting] signature and registering it
in ADAPTERS.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import store
from .adapters import greenhouse, lever, workday

ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "workday": workday.fetch,
}


def load_registry(path: str | None = None) -> list[tuple[str, str]]:
    """Parse registry.txt and return (vendor, slug) tuples.

    Skips blank lines and comments (# to end of line). Lowercases vendor
    names for comparison against ADAPTERS keys.

    Args:
        path: Optional path to registry.txt; defaults to
            data_path("registry.txt").

    Returns:
        List of (vendor, slug) tuples, one per valid registry line.
        Malformed lines are logged to stderr and skipped.
    """
    entries = []
    path = path or store.data_path("registry.txt")
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            print(f"  ! malformed registry line: {line!r}", file=sys.stderr)
            continue
        entries.append((parts[0].lower(), parts[1]))
    return entries


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
    for vendor, slug in load_registry():
        fetch = ADAPTERS.get(vendor)
        if fetch is None:
            error = f"no adapter for vendor {vendor!r}"
            print(f"  ! {error} (slug {slug})", file=sys.stderr)
            failures.append({"source": vendor, "company_slug": slug, "error": error})
            continue
        try:
            postings = fetch(slug)
            added = store.upsert_postings(postings)
            total_new += added
            print(f"  {vendor:12} {slug:20} {len(postings):4} fetched, {added:4} new")
        except Exception as e:  # one bad board shouldn't kill the run
            print(f"  ! {vendor}/{slug} failed: {e}", file=sys.stderr)
            failures.append({"source": vendor, "company_slug": slug, "error": str(e)})
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
