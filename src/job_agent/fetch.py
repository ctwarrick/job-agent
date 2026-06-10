"""Fetch orchestrator.

Reads registry.txt, dispatches each company to the right ATS adapter,
normalizes, and upserts into SQLite. Adding a new ATS = write an adapter
with a fetch(slug) -> List[Posting] signature and register it in ADAPTERS.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import store
from .adapters import greenhouse, lever

ADAPTERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
}


def load_registry(path: str = "registry.txt") -> list[tuple[str, str]]:
    entries = []
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


def main() -> None:
    store.init()
    total_new = 0
    for vendor, slug in load_registry():
        fetch = ADAPTERS.get(vendor)
        if fetch is None:
            print(f"  ! no adapter for vendor {vendor!r} (slug {slug})", file=sys.stderr)
            continue
        try:
            postings = fetch(slug)
            added = store.upsert_postings(postings)
            total_new += added
            print(f"  {vendor:12} {slug:20} {len(postings):4} fetched, {added:4} new")
        except Exception as e:  # one bad board shouldn't kill the run
            print(f"  ! {vendor}/{slug} failed: {e}", file=sys.stderr)
    print(f"\nDone. {total_new} new postings added.")


if __name__ == "__main__":
    main()
