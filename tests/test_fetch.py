from pathlib import Path

import pytest

from job_agent import fetch, registry, resilient
from job_agent.filter import Criteria
from job_agent.resilient import SourceResult
from job_agent.schema import Posting, normalize

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
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

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
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

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
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

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
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

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
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

    calls: list = []

    def fake_run_source(adapter, source, *, criteria, stage_deadline=None, **kw):
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


# --- stage budget: submission stop, deferral, ordering, no starvation -------
# (007 US2/T009, contracts/fetch-stage.md, data-model.md R5)


def _greenhouse_posting(slug: str) -> Posting:
    return normalize(
        source="greenhouse",
        company=slug,
        external_id="1",
        title="Engineer",
        location="Remote",
        description="desc",
        url=f"https://example.com/{slug}",
        posted_at=None,
    )


def test_fetch_dispatches_in_sources_by_recency_order(monkeypatch) -> None:
    """FR-006: dispatch follows store.sources_by_recency's returned order,
    not registry.toml's file order -- proves fetch builds (vendor, company)
    keys from the registry and consumes the reordered result, rather than
    just iterating load_registry() directly."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    registry_order = [
        registry.Source(vendor="greenhouse", slug="a", company="a"),
        registry.Source(vendor="greenhouse", slug="b", company="b"),
        registry.Source(vendor="greenhouse", slug="c", company="c"),
    ]
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: registry_order)

    recorded_keys: list = []

    def fake_recency(keys, path=None):
        recorded_keys.append(list(keys))
        return list(reversed(keys))  # deliberately reorder

    monkeypatch.setattr(fetch.store, "sources_by_recency", fake_recency)

    dispatched: list[str] = []

    def fetch_fn(slug, *, company=None):
        dispatched.append(slug)
        return []

    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": fetch_fn})
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

    fetch.main()

    assert recorded_keys == [[("greenhouse", "a"), ("greenhouse", "b"), ("greenhouse", "c")]]
    assert dispatched == ["c", "b", "a"]


def test_fetch_dispatches_all_boards_sharing_a_recency_key(monkeypatch) -> None:
    """Regression (review defect): registry uniqueness is (vendor, SLUG), not
    (vendor, company) -- company is a display value that defaults to the
    vendor's tenant/slug default or an explicit `name` override, so two
    distinct boards can legitimately share (vendor, company) while having
    different slugs (e.g. two Workday sites under one tenant, both named
    "acme"). Collapsing dispatch through a `{(vendor, company): source}` dict
    silently drops every board but the last one sharing a key -- data loss.
    Both boards here share vendor="greenhouse", company="Acme" but have
    distinct slugs, so both must still be dispatched."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    sources = [
        registry.Source(vendor="greenhouse", slug="acme-eng", company="Acme"),
        registry.Source(vendor="greenhouse", slug="acme-sales", company="Acme"),
    ]
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: sources)
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)
    monkeypatch.setattr(fetch.store, "mark_converged", lambda *a, **k: None)

    dispatched: list[str] = []

    def recording_adapter(slug, *, company=None):
        dispatched.append(slug)
        return []

    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": recording_adapter})

    fetch.main()

    assert "acme-eng" in dispatched, "first board sharing (vendor, company) was dropped"
    assert "acme-sales" in dispatched, "second board sharing (vendor, company) was dropped"


def test_fetch_stops_submitting_new_boards_past_stage_budget(tmp_path: Path, monkeypatch) -> None:
    """FR-003/SC-003: over many stubbed boards, once clock() > stage_deadline
    fetch stops SUBMITTING new boards. Everything already fetched is
    retained, and every never-dispatched board comes back in
    partial_sources as {source, company_slug, reason='budget_deferred'} --
    the single representation, no third return value. A deferred board's
    last_converged_at is NOT advanced (no starvation), while the dispatched
    board's IS (extends 006's mark_converged to single-request vendors)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)

    boards = [
        registry.Source(vendor="greenhouse", slug=f"co{i}", company=f"co{i}") for i in range(5)
    ]
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: boards)
    # Ordering is proven separately; here it is identity so submission order
    # equals registry order.
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)

    dispatched: list[str] = []

    def fetch_fn(slug, *, company=None):
        dispatched.append(slug)
        return [_greenhouse_posting(slug)]

    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": fetch_fn})
    monkeypatch.setenv("JOBAGENT_FETCH_BUDGET_SECONDS", "10")

    # State-based, not call-count-based, so it is robust to however many
    # times fetch checks the clock per board: budget is exceeded only AFTER
    # the first board has actually run (its adapter appended to `dispatched`).
    def fake_clock() -> float:
        return 100.0 if dispatched else 0.0

    failed, partial = fetch.main(clock=fake_clock)

    assert dispatched == ["co0"], "only the first board should have been submitted"
    assert failed == []
    deferred = [p for p in partial if p.get("reason") == "budget_deferred"]
    assert {d["company_slug"] for d in deferred} == {"co1", "co2", "co3", "co4"}
    assert all(d["source"] == "greenhouse" for d in deferred)
    assert set(deferred[0]) == {"source", "company_slug", "reason"}  # single representation

    # Everything already fetched is retained (the upsert happened).
    assert fetch.store.existing_external_ids("greenhouse", "co0") == {"1"}
    for slug in ("co1", "co2", "co3", "co4"):
        assert fetch.store.existing_external_ids("greenhouse", slug) == set()

    # No starvation: the dispatched board converges, the deferred ones don't.
    assert fetch.store.get_last_converged("greenhouse", "co0") is not None
    for slug in ("co1", "co2", "co3", "co4"):
        assert fetch.store.get_last_converged("greenhouse", slug) is None


def test_fetch_marks_converged_after_successful_single_request_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    """data-model.md R4: greenhouse/lever now advance last_converged_at on a
    successful whole-board fetch (previously only resilient vendors did),
    making the recency ordering total across the whole registry."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)
    sources = [
        registry.Source(vendor="greenhouse", slug="acme", company="acme"),
        registry.Source(vendor="lever", slug="globex", company="globex"),
    ]
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: sources)
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)

    def make_fetch(vendor: str):
        def fetch_fn(slug, *, company=None):
            return [
                normalize(
                    source=vendor,
                    company=company or slug,
                    external_id="1",
                    title="Engineer",
                    location="Remote",
                    description="desc",
                    url=f"https://example.com/{slug}",
                    posted_at=None,
                )
            ]

        return fetch_fn

    monkeypatch.setattr(
        fetch, "ADAPTERS", {"greenhouse": make_fetch("greenhouse"), "lever": make_fetch("lever")}
    )

    fetch.main()

    assert fetch.store.get_last_converged("greenhouse", "acme") is not None
    assert fetch.store.get_last_converged("lever", "globex") is not None


def test_fetch_passes_stage_deadline_to_resilient_run_source(monkeypatch) -> None:
    """FR-009/R5: fetch computes the absolute stage_deadline once (clock() +
    fetch_budget_seconds()) and forwards it to resilient.run_source for a
    dispatched resilient board -- the seam run_source uses to clamp an
    in-flight board's effective deadline to min(per-source, stage-remaining)."""
    src = registry.Source(vendor="workday", slug="globex:Globex:wd5", company="Globex")
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: [src])
    monkeypatch.setattr(fetch.store, "sources_by_recency", lambda keys, path=None: keys)
    monkeypatch.setattr(fetch, "RESILIENT_VENDORS", {"workday"})
    monkeypatch.setattr(fetch, "ADAPTERS", {"workday": object()})
    monkeypatch.setattr(fetch, "load_criteria", lambda *a, **k: object())

    captured: dict = {}

    def fake_run_source(adapter, source, *, criteria, stage_deadline=None, **kw):
        captured["stage_deadline"] = stage_deadline
        return SourceResult("workday", "globex:Globex:wd5", new=1)

    monkeypatch.setattr(fetch.resilient, "run_source", fake_run_source)
    monkeypatch.setenv("JOBAGENT_FETCH_BUDGET_SECONDS", "10")

    fetch.main(clock=lambda: 0.0)

    assert captured.get("stage_deadline") == 10.0


def test_resilient_run_source_clamps_effective_deadline_to_stage_remaining(
    monkeypatch,
) -> None:
    """R5/T009: resilient.run_source's new `stage_deadline` kwarg (default
    None, so tests/test_resilient.py stays green unchanged -- T017) makes the
    effective deadline min(clock()+fetch_deadline_seconds(), stage_deadline).
    Mirrors the tick pattern of test_resilient.py's own deadline test: a
    generous per-source deadline (1000s, never binds) is clamped down to the
    tighter stage_deadline=10, so the same 2-described/18-remaining
    truncation fires as if the per-source deadline were 10."""
    monkeypatch.setenv("JOBAGENT_FETCH_DEADLINE_SECONDS", "1000")
    criteria = Criteria(("sales",), (), 3650, True, ("WA",), ())
    postings = [
        Posting("workday", "Globex", str(i), "Engineer", "Remote", "", f"https://x/{i}", None)
        for i in range(20)
    ]

    class FakeAdapter:
        def list_postings(self, slug, *, company=None):
            return postings

        def fetch_description(self, posting):
            return f"desc {posting.external_id}"

    class FakeSource:
        vendor = "workday"
        slug = "globex"
        company = "Globex"

    class FakeStore:
        def existing_external_ids(self, source, company):
            return set()

        def upsert_postings(self, postings):
            return len(postings)

        def get_last_converged(self, source, company):
            return None

        def mark_converged(self, source, company, when):
            pass

        def seed_source(self, source, company, when):
            pass

    # start=0, per-source deadline = 0+1000 = 1000 (never binds). stage_deadline
    # =10 is the tighter constraint: top-of-loop ticks 4, 8 (<=10, two survivors
    # described), then 12 (>10) trips the clamp before the third -> 2 described,
    # 18 remaining (20 - 2 = 18), exactly test_resilient.py's own arithmetic for
    # a deadline of 10.
    ticks = iter([0, 4, 8, 12, 16, 20, 24, 28])

    def fake_clock() -> float:
        return next(ticks)

    result = resilient.run_source(
        FakeAdapter(),
        FakeSource(),
        criteria=criteria,
        store_=FakeStore(),
        clock=fake_clock,
        stage_deadline=10.0,
    )

    assert result.truncated is True
    assert result.remaining == 18


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
