"""Deterministic pre-LLM relevance gate (feature 002, User Story 1).

`score.main()` runs every scorable posting through `classify()` before
spending any LLM tokens on it. The gate is configured entirely by
`filter.toml` (a runtime tuning file, git-ignored; see
`filter.toml.example` for the schema) so the search can be retargeted
without a code change or redeploy.

Data contract:
  - `classify(posting, criteria)` is a pure function. `posting` is either a
    plain dict or a `sqlite3.Row` (which has no `.get()`), so every field is
    accessed by `posting["key"]`. The keys `title`, `location`, and
    `posted_at` are always present (location/posted_at may be None/"").
  - It returns `None` if the posting is plausible (should reach the LLM), or
    a machine-readable rejection reason string of the form `<gate>:<detail>`
    otherwise.
  - Only the function denylist (gate 1) can reject; the allowlist (gate 2)
    is advisory and never returns a reason; the age and location gates
    (3-4) fail OPEN when their input field is missing or unparseable.

See specs/002-scoring-spend-efficiency/contracts/filter-criteria.md for the
full gate-order and semantics contract.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import store


@dataclass(frozen=True)
class Criteria:
    """Deterministic filter criteria loaded from `filter.toml`.

    Attributes:
        denylist_title_keywords: Title substrings that hard-reject a
            posting (case-insensitive).
        allowlist_title_keywords: Target-function title substrings;
            advisory only, never rejects on its own.
        age_max_days: Reject postings older than this many days when
            `posted_at` is present and parseable.
        location_remote_ok: Whether a remote location keeps a posting.
        location_regions: State/region tokens (e.g. "WA") that keep a
            posting when they exactly match a comma-delimited token of
            its location.
        location_metros: Substrings (e.g. "Bay Area") that keep a posting
            when they appear anywhere in its location string.
    """

    denylist_title_keywords: tuple[str, ...]
    allowlist_title_keywords: tuple[str, ...]
    age_max_days: int
    location_remote_ok: bool
    location_regions: tuple[str, ...]
    location_metros: tuple[str, ...]


def _require_str_list(value: Any, field: str) -> list[str]:
    """Validate that `value` is a list of str, raising ValueError if not.

    Args:
        value: The value to validate.
        field: Dotted field name for the error message (e.g.
            "denylist.title_keywords").

    Returns:
        `value` unchanged, typed as list[str].

    Raises:
        ValueError: If `value` is not a list, or contains a non-str item.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{field} must be a list of strings")
    return value


def load_criteria(path: str | None = None) -> Criteria:
    """Load and validate filter criteria from `filter.toml`.

    Args:
        path: Optional path to the TOML file; defaults to
            `store.data_path("filter.toml")` (honors JOBAGENT_DATA_DIR).

    Returns:
        A validated, frozen `Criteria` instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is structurally invalid (wrong types,
            non-positive max_days, etc.) per
            specs/002-scoring-spend-efficiency/contracts/filter-criteria.md.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    resolved = path or store.data_path("filter.toml")
    with open(resolved, "rb") as f:
        data = tomllib.load(f)

    denylist = data.get("denylist", {})
    allowlist = data.get("allowlist", {})
    age = data.get("age", {})
    location = data.get("location", {})

    denylist_keywords = _require_str_list(
        denylist.get("title_keywords", []), "denylist.title_keywords"
    )
    allowlist_keywords = _require_str_list(
        allowlist.get("title_keywords", []), "allowlist.title_keywords"
    )

    max_days = age.get("max_days", 30)
    if isinstance(max_days, bool) or not isinstance(max_days, int) or max_days <= 0:
        raise ValueError("age.max_days must be a positive int")

    remote_ok = location.get("remote_ok", True)
    if not isinstance(remote_ok, bool):
        raise ValueError("location.remote_ok must be a bool")

    regions = _require_str_list(location.get("regions", []), "location.regions")
    metros = _require_str_list(location.get("metros", []), "location.metros")

    return Criteria(
        denylist_title_keywords=tuple(denylist_keywords),
        allowlist_title_keywords=tuple(allowlist_keywords),
        age_max_days=max_days,
        location_remote_ok=remote_ok,
        location_regions=tuple(regions),
        location_metros=tuple(metros),
    )


def classify(posting: Any, criteria: Criteria) -> str | None:
    """Classify a posting as plausible (None) or rejected (reason string).

    Runs the gates in order (function denylist, allowlist, posting-age,
    location); the first rejecting gate wins. See
    specs/002-scoring-spend-efficiency/contracts/filter-criteria.md for the
    full semantics, including the fail-open rules for age and location.

    Args:
        posting: A posting dict or `sqlite3.Row` with at least `title`,
            `location`, and `posted_at` keys (accessed by `[]`, not
            `.get()`, since `sqlite3.Row` has no `.get()`).
        criteria: The loaded filter criteria.

    Returns:
        `None` if the posting is plausible (should reach the LLM), or a
        machine-readable reason string of the form `<gate>:<detail>`.
    """
    title = posting["title"]
    title_lower = title.lower()

    # 1. Function denylist (hard reject; the only rejecting gate).
    for kw in criteria.denylist_title_keywords:
        if kw.lower() in title_lower:
            return f"function_denylist:{kw}"

    # 2. Allowlist is advisory only -- it never rejects, so there is
    # nothing to evaluate here.

    # 3. Posting-age: only when posted_at is present and parseable.
    posted_at = posting["posted_at"]
    if posted_at:
        try:
            posted = datetime.fromisoformat(posted_at)
        except ValueError:
            posted = None
        if posted is not None:
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - posted).days
            if days > criteria.age_max_days:
                return f"age:{days}d"

    # 4. Location: only when present and not the default "Unspecified".
    location = posting["location"]
    if location and location != "Unspecified":
        location_lower = location.lower()
        if criteria.location_remote_ok and "remote" in location_lower:
            return None
        tokens = [t.strip().lower() for t in location.split(",")]
        if any(region.lower() in tokens for region in criteria.location_regions):
            return None
        if any(metro.lower() in location_lower for metro in criteria.location_metros):
            return None
        return f"location:{location}"

    return None
