"""Stub-based, no-network tests for the validated TOML source registry.

Mirrors tests/test_filter.py: real per-test TOML fixtures written to tmp
files (no network, no real registry.toml), exercised through
`registry.load_registry()`. Only public example slugs are used (e.g.
`initech`) -- never real company names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from job_agent import registry

# --- 1. Loads each vendor, reconstructs slug, resolves company --------------


ALL_VENDORS_TOML = """
[[source]]
vendor = "greenhouse"
slug   = "initech"

[[source]]
vendor = "lever"
slug   = "cyberdyne"
name   = "Cyberdyne"

[[source]]
vendor = "workday"
name   = "Acme Corp"
tenant = "acme"
site   = "AcmeCareers"
host   = "wd5"

[[source]]
vendor = "icims"
tenant = "hooli"
host   = "careers.hooli.com"
"""


def test_load_registry_resolves_each_vendor_slug_and_company(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(ALL_VENDORS_TOML)

    sources = registry.load_registry(str(toml_path))

    assert len(sources) == 4

    gh = sources[0]
    assert gh.vendor == "greenhouse"
    assert gh.slug == "initech"
    assert gh.company == "initech"  # no `name` -> vendor default (slug)

    lever_src = sources[1]
    assert lever_src.vendor == "lever"
    assert lever_src.slug == "cyberdyne"
    assert lever_src.company == "Cyberdyne"  # `name` present -> authoritative

    workday_src = sources[2]
    assert workday_src.vendor == "workday"
    assert workday_src.slug == "acme:AcmeCareers:wd5"
    assert workday_src.company == "Acme Corp"  # `name` present -> authoritative

    icims_src = sources[3]
    assert icims_src.vendor == "icims"
    assert icims_src.slug == "hooli:careers.hooli.com"
    assert icims_src.company == "hooli"  # no `name` -> vendor default (tenant)


# --- 2. enabled = false is omitted -------------------------------------------


DISABLED_SOURCE_TOML = """
[[source]]
vendor = "greenhouse"
slug   = "initech"

[[source]]
vendor = "lever"
slug   = "cyberdyne"
enabled = false
"""


def test_disabled_source_is_omitted(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(DISABLED_SOURCE_TOML)

    sources = registry.load_registry(str(toml_path))

    assert len(sources) == 1
    assert sources[0].vendor == "greenhouse"
    assert sources[0].slug == "initech"


# --- 3. raises on unknown vendor ---------------------------------------------


UNKNOWN_VENDOR_TOML = """
[[source]]
vendor = "bamboohr"
slug   = "initech"
"""


def test_unknown_vendor_raises(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(UNKNOWN_VENDOR_TOML)

    with pytest.raises(ValueError, match="bamboohr"):
        registry.load_registry(str(toml_path))


# --- 4. raises on missing required field (workday missing host) -------------


MISSING_HOST_TOML = """
[[source]]
vendor = "workday"
tenant = "acme"
site   = "AcmeCareers"
"""


def test_workday_missing_host_raises(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(MISSING_HOST_TOML)

    with pytest.raises(ValueError, match="acme"):
        registry.load_registry(str(toml_path))


# --- 5. raises on duplicate (vendor, slug) -----------------------------------


DUPLICATE_SOURCE_TOML = """
[[source]]
vendor = "greenhouse"
slug   = "initech"

[[source]]
vendor = "greenhouse"
slug   = "initech"
"""


def test_duplicate_vendor_slug_raises(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(DUPLICATE_SOURCE_TOML)

    with pytest.raises(ValueError, match="initech"):
        registry.load_registry(str(toml_path))


# --- 6. raises on unrecognized key (typo guard) ------------------------------


TYPO_KEY_TOML = """
[[source]]
vendor = "greenhouse"
slgu   = "initech"
"""


def test_unrecognized_key_raises(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(TYPO_KEY_TOML)

    with pytest.raises(ValueError, match="slgu"):
        registry.load_registry(str(toml_path))


# --- 7. honors JOBAGENT_DATA_DIR ----------------------------------------------


def test_load_registry_honors_data_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "registry.toml").write_text(ALL_VENDORS_TOML)
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))

    sources = registry.load_registry()

    assert len(sources) == 4
    assert sources[0].vendor == "greenhouse"
    assert sources[0].slug == "initech"


# --- 8. inline # comments are ignored ----------------------------------------


COMMENTED_TOML = """
[[source]]
vendor = "icims"
tenant = "hooli"
host   = "careers.hooli.com"   # custom domain, omit for {tenant}.icims.com default
# enabled = false            # disable without deleting
"""


def test_inline_comments_are_ignored(tmp_path: Path) -> None:
    toml_path = tmp_path / "registry.toml"
    toml_path.write_text(COMMENTED_TOML)

    sources = registry.load_registry(str(toml_path))

    assert len(sources) == 1
    assert sources[0].vendor == "icims"
    assert sources[0].slug == "hooli:careers.hooli.com"
    assert sources[0].company == "hooli"
