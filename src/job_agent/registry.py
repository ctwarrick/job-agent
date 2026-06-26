"""Validated loader for the `registry.toml` source list.

`fetch.main()` needs a list of ATS sources to poll. Rather than the old
line-oriented `registry.txt` (vendor + slug only, company resolved
separately via `companies.toml`), sources now live in `registry.toml` as
`[[source]]` tables that carry the vendor's own identifying fields plus an
optional display `name`. This module loads that file, validates every
source fail-loud (typo'd keys and missing required fields surface at load
time, not as a silent skip during a fetch run), reconstructs each source's
slug, and resolves its display company name.

Data contract: see
specs/004-registry-toml-config/contracts/registry-schema.md and
specs/004-registry-toml-config/data-model.md for the full field table,
validation rules, slug-reconstruction, and company-resolution semantics.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Any

from . import store

# Fields every source may carry regardless of vendor.
_COMMON_KEYS = {"vendor", "name", "enabled", "max_per_employer"}

# Required (non-empty) fields per vendor, beyond `vendor` itself.
_REQUIRED_FIELDS = {
    "greenhouse": ("slug",),
    "lever": ("slug",),
    "workday": ("tenant", "site", "host"),
    "icims": ("tenant",),
    "talemetry": ("host",),
}

# All fields a vendor is allowed to set, beyond `_COMMON_KEYS`.
_VENDOR_FIELDS = {
    "greenhouse": {"slug"},
    "lever": {"slug"},
    "workday": {"tenant", "site", "host"},
    "icims": {"tenant", "host"},
    "talemetry": {"host"},
}


@dataclass(frozen=True)
class Source:
    """One validated, resolved entry from `registry.toml`.

    Attributes:
        vendor: The ATS vendor key (a key of `fetch.ADAPTERS`).
        slug: The reconstructed board/tenant slug (vendor-specific
            format; see data-model.md "Slug reconstruction").
        company: The resolved display company name: the source's `name`
            if present, else the vendor default (`slug` for
            greenhouse/lever, `tenant` for workday/icims).
    """

    vendor: str
    slug: str
    company: str


def _describe(raw: dict[str, Any]) -> str:
    """Build a human-readable identifier for an offending source.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.

    Returns:
        A short string naming the source's vendor and, when available,
        its slug/tenant, for use in `ValueError` messages.
    """
    vendor = raw.get("vendor", "<unknown vendor>")
    ident = raw.get("slug") or raw.get("tenant")
    if ident:
        return f"vendor={vendor!r} ({ident!r})"
    return f"vendor={vendor!r}"


def _validate_keys(raw: dict[str, Any], vendor: str) -> None:
    """Raise if `raw` carries any key not recognized for its vendor.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.
        vendor: The source's vendor key.

    Raises:
        ValueError: If an unrecognized key (typo) is present.
    """
    allowed = _COMMON_KEYS | _VENDOR_FIELDS[vendor]
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unrecognized key(s) {sorted(unknown)} on source {_describe(raw)}")


def _validate_required(raw: dict[str, Any], vendor: str) -> None:
    """Raise if `raw` is missing a non-empty required field for its vendor.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.
        vendor: The source's vendor key.

    Raises:
        ValueError: If a required field is missing or empty.
    """
    for field in _REQUIRED_FIELDS[vendor]:
        value = raw.get(field)
        if not value:
            raise ValueError(f"Missing required field {field!r} on source {_describe(raw)}")


def _validate_common(raw: dict[str, Any]) -> None:
    """Raise if the optional common fields have the wrong type.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.

    Raises:
        ValueError: If `enabled` is present and not a bool, or
            `max_per_employer` is present and not an int.
    """
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError(f"'enabled' must be a bool on source {_describe(raw)}")
    max_per_employer = raw.get("max_per_employer")
    if max_per_employer is not None and (
        isinstance(max_per_employer, bool) or not isinstance(max_per_employer, int)
    ):
        raise ValueError(f"'max_per_employer' must be an int on source {_describe(raw)}")


def _reconstruct_slug(raw: dict[str, Any], vendor: str) -> str:
    """Reconstruct the canonical slug for a validated source.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.
        vendor: The source's vendor key.

    Returns:
        The reconstructed slug per data-model.md "Slug reconstruction".
    """
    if vendor in ("greenhouse", "lever"):
        return raw["slug"]
    if vendor == "workday":
        return f"{raw['tenant']}:{raw['site']}:{raw['host']}"
    if vendor == "talemetry":
        return raw["host"]
    # icims
    host = raw.get("host")
    return raw["tenant"] if not host else f"{raw['tenant']}:{host}"


def _resolve_company(raw: dict[str, Any], vendor: str) -> str:
    """Resolve the display company name for a validated source.

    Args:
        raw: The raw `[[source]]` table as parsed from TOML.
        vendor: The source's vendor key.

    Returns:
        `name` if present and non-empty, else the vendor default
        (`slug` for greenhouse/lever, `tenant` for workday/icims).
    """
    name = raw.get("name")
    if name:
        return name
    if vendor in ("greenhouse", "lever"):
        return raw["slug"]
    if vendor == "talemetry":
        return raw["host"]
    return raw["tenant"]


def load_registry(path: str | None = None) -> list[Source]:
    """Load and validate sources from `registry.toml`.

    Args:
        path: Optional path to the TOML file; defaults to
            `store.data_path("registry.toml")` (honors JOBAGENT_DATA_DIR).

    Returns:
        Validated, resolved `Source` records in file order, with any
        `enabled = false` sources omitted.

    Raises:
        ValueError: If a source has an unknown vendor, a missing
            required field, an unrecognized key, a malformed `enabled`/
            `max_per_employer`, or duplicates another source's
            reconstructed `(vendor, slug)`. The message names the
            offending source.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    # Imported lazily: `fetch` imports this module, so a module-level
    # import here would form a cycle. By call time both modules are loaded.
    from .fetch import ADAPTERS

    resolved = path or store.data_path("registry.toml")
    with open(resolved, "rb") as f:
        data = tomllib.load(f)

    sources: list[Source] = []
    seen: set[tuple[str, str]] = set()

    for raw in data.get("source", []):
        vendor = raw.get("vendor")
        if vendor not in ADAPTERS:
            raise ValueError(f"Unknown vendor {_describe(raw)}")

        _validate_keys(raw, vendor)
        _validate_required(raw, vendor)
        _validate_common(raw)

        if raw.get("enabled") is False:
            continue

        slug = _reconstruct_slug(raw, vendor)
        key = (vendor, slug)
        if key in seen:
            raise ValueError(f"Duplicate source vendor={vendor!r} slug={slug!r}")
        seen.add(key)

        company = _resolve_company(raw, vendor)
        sources.append(Source(vendor=vendor, slug=slug, company=company))

    return sources
