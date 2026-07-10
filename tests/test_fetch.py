import pytest

from job_agent import fetch, registry
from job_agent.resilient import SourceResult

# --- per-source failure records (FR-005/006) --------------------------------
# fetch.main() now returns a (failed_sources, partial_sources) tuple: the first
# is wholly-unreachable sources (existing behavior), the second is sources that
# were only partially fetched (skips / backstop truncation / persistent
# staleness) for the digest's degraded category (FR-014).


def test_main_returns_failure_record_when_adapter_raises(monkeypatch) -> None:
    """A failing out-of-scope adapter does not kill the run: its failure is
    captured as a {source, company_slug, error} record while a healthy source
    in the same run still upserts. No partial records for a clean source."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="good", company="good"),
        ],
    )

    def bad(slug: str, *, company=None):
        raise RuntimeError("boom timeout")

    def good(slug: str, *, company=None):
        return ["p1", "p2"]

    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": bad, "lever": good})

    upserted: list = []
    monkeypatch.setattr(
        fetch.store,
        "upsert_postings",
        lambda postings, *a, **k: (upserted.extend(postings) or len(postings)),
    )

    failed, partial = fetch.main()

    assert upserted == ["p1", "p2"]
    assert partial == []
    assert len(failed) == 1
    rec = failed[0]
    assert rec["source"] == "greenhouse"
    assert rec["company_slug"] == "acme"
    assert "boom timeout" in rec["error"]


def test_main_returns_failure_record_for_unknown_vendor(monkeypatch) -> None:
    """A registry vendor with no adapter is per-source degradation, not a fatal
    config error: a failure record, the run continues, and (since the adapter
    is missing) no filter criteria are loaded."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [registry.Source(vendor="workday", slug="bigco", company="bigco")],
    )
    monkeypatch.setattr(fetch, "ADAPTERS", {})

    def boom_criteria(*a, **k):
        raise AssertionError("criteria must not load when the adapter is missing")

    monkeypatch.setattr(fetch, "load_criteria", boom_criteria)

    failed, partial = fetch.main()

    assert partial == []
    assert len(failed) == 1
    rec = failed[0]
    assert rec["source"] == "workday"
    assert rec["company_slug"] == "bigco"
    assert "adapter" in rec["error"].lower()


def test_main_returns_empty_lists_when_all_sources_succeed(monkeypatch) -> None:
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="plaid", company="plaid"),
        ],
    )
    monkeypatch.setattr(
        fetch,
        "ADAPTERS",
        {"greenhouse": lambda s, *, company=None: [], "lever": lambda s, *, company=None: []},
    )
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    assert fetch.main() == ([], [])


def test_main_passes_resolved_company_to_out_of_scope_adapter(monkeypatch) -> None:
    """An out-of-scope adapter is still dispatched as
    ADAPTERS[vendor](slug, company=source.company)."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="plaid", company="Acme Corp"),
        ],
    )

    received: list[tuple] = []

    def recording_adapter(slug, *, company=None):
        received.append((slug, company))
        return []

    monkeypatch.setattr(
        fetch, "ADAPTERS", {"greenhouse": recording_adapter, "lever": recording_adapter}
    )
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    fetch.main()

    assert ("acme", "acme") in received
    assert ("plaid", "Acme Corp") in received


# --- resilient (two-phase) routing (FR-007/013/014) -------------------------


def _one_resilient_source(monkeypatch, result: SourceResult) -> dict:
    """Wire fetch.main() to dispatch one workday source through a fake
    resilient.run_source returning `result`.

    Returns a context dict with the Source, the recorded run_source calls, and
    the sentinel adapter/criteria objects (registry.Source is frozen, so the
    bookkeeping cannot hang off it)."""
    src = registry.Source(vendor="workday", slug="globex:Globex:wd5", company="Globex")
    sentinel_adapter = object()
    sentinel_criteria = object()
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: [src])
    monkeypatch.setattr(fetch, "RESILIENT_VENDORS", {"workday"})
    monkeypatch.setattr(fetch, "ADAPTERS", {"workday": sentinel_adapter})
    monkeypatch.setattr(fetch, "load_criteria", lambda *a, **k: sentinel_criteria)

    calls: list = []

    def fake_run_source(adapter, source, *, criteria):
        calls.append((adapter, source, criteria))
        return result

    monkeypatch.setattr(fetch.resilient, "run_source", fake_run_source)
    return {
        "source": src,
        "calls": calls,
        "adapter": sentinel_adapter,
        "criteria": sentinel_criteria,
    }


def test_main_routes_resilient_vendor_through_run_source(monkeypatch) -> None:
    ctx = _one_resilient_source(monkeypatch, SourceResult("workday", "globex:Globex:wd5", new=7))

    failed, partial = fetch.main()

    assert failed == [] and partial == []
    assert len(ctx["calls"]) == 1
    adapter, source, criteria = ctx["calls"][0]
    assert adapter is ctx["adapter"]
    assert source is ctx["source"]
    assert criteria is ctx["criteria"]


def test_main_collects_partial_source_when_truncated_or_skipped(monkeypatch) -> None:
    _one_resilient_source(
        monkeypatch,
        SourceResult(
            "workday", "globex:Globex:wd5", new=3, skipped=2, truncated=True, remaining=10
        ),
    )

    failed, partial = fetch.main()

    assert failed == []
    assert len(partial) == 1
    p = partial[0]
    assert p["source"] == "workday"
    assert p["company_slug"] == "globex:Globex:wd5"
    assert p["new"] == 3
    assert p["skipped"] == 2
    assert p["truncated"] is True
    assert p["persistent"] is False


def test_main_persistent_source_is_partial(monkeypatch) -> None:
    _one_resilient_source(
        monkeypatch,
        SourceResult(
            "workday", "globex:Globex:wd5", new=0, truncated=True, remaining=50, persistent=True
        ),
    )

    failed, partial = fetch.main()

    assert failed == []
    assert len(partial) == 1
    assert partial[0]["persistent"] is True


def test_main_resilient_source_error_goes_to_failed(monkeypatch) -> None:
    _one_resilient_source(
        monkeypatch, SourceResult("workday", "globex:Globex:wd5", error="listing 500")
    )

    failed, partial = fetch.main()

    assert partial == []
    assert len(failed) == 1
    assert failed[0]["source"] == "workday"
    assert failed[0]["company_slug"] == "globex:Globex:wd5"
    assert "listing 500" in failed[0]["error"]


def test_main_clean_resilient_source_is_neither_failed_nor_partial(monkeypatch) -> None:
    _one_resilient_source(
        monkeypatch, SourceResult("workday", "globex:Globex:wd5", new=12, remaining=0)
    )

    assert fetch.main() == ([], [])


def test_cli_discards_return_value(monkeypatch) -> None:
    """jobagent-fetch (fetch:_cli) returns None so sys.exit(_cli()) exits 0 on
    success, even though main() now returns a (failed, partial) tuple."""
    monkeypatch.setattr(
        fetch, "main", lambda: ([{"source": "x", "company_slug": "y", "error": "z"}], [])
    )
    assert fetch._cli() is None


# --- config getter fail-loud (FR-004/FR-010) --------------------------------
# execution_window_seconds() has no code default: unset must fail loud, and a
# non-positive value must raise via the shared _positive_int_env parse path.


def test_execution_window_seconds_unset_fails_loud(monkeypatch) -> None:
    """FR-004: the window is required with no code default -- an unset var
    exits non-zero rather than silently assuming a window the platform never
    honors (Principle V)."""
    monkeypatch.delenv("JOBAGENT_EXECUTION_WINDOW_SECONDS", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        fetch.execution_window_seconds()
    assert "JOBAGENT_EXECUTION_WINDOW_SECONDS" in str(exc_info.value.code)


def test_execution_window_seconds_non_positive_raises(monkeypatch) -> None:
    """FR-010: a set-but-non-positive window is invalid config -- it raises via
    the same _positive_int_env validator the other getters use."""
    monkeypatch.setenv("JOBAGENT_EXECUTION_WINDOW_SECONDS", "0")
    with pytest.raises(ValueError):
        fetch.execution_window_seconds()
