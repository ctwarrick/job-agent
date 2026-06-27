"""Failing tests (TDD red) for the not-yet-built `job_agent.resilient` module.

Pins the resilient per-source fetch loop: filter-before-detail ordering, the
three backstops (cap / deadline / forward-progress), per-item skip-and-continue,
inline short-circuit when a posting already carries a description, and
staleness/convergence bookkeeping. See the interface contract in the dispatch
prompt; implementation lives in `src/job_agent/resilient.py` (does not exist
yet, so every test here fails at import time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from job_agent import resilient  # ModuleNotFoundError expected -- this is red.
from job_agent.filter import Criteria
from job_agent.schema import Posting

CRITERIA = Criteria(("sales",), (), 3650, True, ("WA",), ())


def make_posting(i: int, *, title: str = "Engineer", location: str = "Remote") -> Posting:
    """Build a stub Posting; `run_source` re-normalizes it via schema.normalize."""
    return Posting("workday", "Globex", str(i), title, location, "", f"https://x/{i}", None)


@dataclass
class FakeSource:
    vendor: str = "workday"
    slug: str = "globex"
    company: str = "Globex"


@dataclass
class FakeAdapter:
    """Stub adapter: presets a posting list (or an exception) plus per-posting
    descriptions (or an exception) for fetch_description."""

    postings: list[Posting] = field(default_factory=list)
    list_error: Exception | None = None
    description_by_id: dict[str, str] = field(default_factory=dict)
    description_error_ids: set[str] = field(default_factory=set)
    described_ids: list[str] = field(default_factory=list)
    description_calls: int = 0

    def list_postings(self, slug: str, *, company=None) -> list[Posting]:
        if self.list_error is not None:
            raise self.list_error
        return self.postings

    def fetch_description(self, posting: Posting) -> str:
        self.description_calls += 1
        self.described_ids.append(posting.external_id)
        if posting.external_id in self.description_error_ids:
            raise RuntimeError(f"detail fetch failed for {posting.external_id}")
        return self.description_by_id.get(posting.external_id, f"desc {posting.external_id}")


@dataclass
class FakeStore:
    """Stub store_ module: presets existing ids + last_converged; records calls."""

    existing_ids: set[str] = field(default_factory=set)
    last_converged: str | None = None
    upserted: list[Posting] = field(default_factory=list)
    seed_calls: list[tuple[str, str, object]] = field(default_factory=list)
    converge_calls: list[tuple[str, str, object]] = field(default_factory=list)

    def existing_external_ids(self, source: str, company: str) -> set[str]:
        return self.existing_ids

    def upsert_postings(self, postings: list[Posting]) -> int:
        self.upserted.extend(postings)
        return len(postings)

    def get_last_converged(self, source: str, company: str) -> str | None:
        return self.last_converged

    def mark_converged(self, source: str, company: str, when) -> None:
        self.converge_calls.append((source, company, when))

    def seed_source(self, source: str, company: str, when) -> None:
        self.seed_calls.append((source, company, when))


def survivors(n: int) -> list[Posting]:
    """n postings that all pass CRITERIA: Remote location, non-denied title."""
    return [make_posting(i) for i in range(n)]


# --- FR-006: list_postings failure is non-fatal and short-circuits ----------


def test_run_source_list_postings_failure_returns_error_and_skips_detail() -> None:
    adapter = FakeAdapter(list_error=RuntimeError("board down"))
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert result.error and "board down" in result.error
    assert result.new == 0
    assert store_.upserted == []
    assert adapter.description_calls == 0


# --- First sighting seeds convergence bookkeeping ----------------------------


def test_run_source_first_sighting_seeds_source() -> None:
    adapter = FakeAdapter(postings=survivors(2))
    store_ = FakeStore(last_converged=None)
    resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert len(store_.seed_calls) == 1
    assert store_.seed_calls[0][0] == "workday"
    assert store_.seed_calls[0][1] == "Globex"


# --- SC-004: filter-before-detail; only survivors get a detail fetch --------


def test_run_source_filters_before_fetching_detail() -> None:
    mixed = [
        make_posting(0, title="Sales Rep"),  # denylist reject
        make_posting(1, title="Engineer", location="Remote"),  # survivor
        make_posting(2, title="Sales Manager"),  # denylist reject
        make_posting(3, title="Engineer", location="Seattle, WA"),  # survivor
        make_posting(4, title="Engineer", location="Brazil"),  # location reject
    ]
    adapter = FakeAdapter(postings=mixed)
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert adapter.description_calls == 2
    assert set(adapter.described_ids) == {"1", "3"}
    assert {p.external_id for p in store_.upserted} == {"1", "3"}
    assert result.new == 2


# --- new == len(described); described == survivors (no skip/cap here) ------


def test_run_source_new_count_matches_described_postings() -> None:
    adapter = FakeAdapter(postings=survivors(4))
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert result.new == 4
    assert adapter.description_calls == 4
    assert {p.external_id for p in store_.upserted} == {"0", "1", "2", "3"}


# --- Backstop 1: per-source detail cap --------------------------------------


def test_run_source_caps_detail_fetches_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBAGENT_MAX_DETAIL_PER_SOURCE", "5")
    adapter = FakeAdapter(postings=survivors(20))
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    # 20 survivors, cap 5 -> exactly 5 described, 15 remaining (20 - 5 = 15).
    assert adapter.description_calls == 5
    assert result.new == 5
    assert result.truncated is True
    assert result.remaining == 15


# --- Backstop 2: wall-clock deadline -----------------------------------------


def test_run_source_stops_at_fetch_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBAGENT_FETCH_DEADLINE_SECONDS", "10")
    adapter = FakeAdapter(postings=survivors(20))
    store_ = FakeStore()

    # The clock is read once before the loop (start=0, deadline=10) and once at
    # the TOP of every iteration. Ticks of 4s: top reads are 4, 8 (both <=10, so
    # two survivors are described), then 12 (>10) trips the deadline before the
    # third: 2 described, 18 remaining (20 - 2 = 18).
    ticks = iter([0, 4, 8, 12, 16, 20, 24, 28])

    def fake_clock() -> float:
        return next(ticks)

    result = resilient.run_source(
        adapter, FakeSource(), criteria=CRITERIA, store_=store_, clock=fake_clock
    )

    assert adapter.description_calls == 2
    assert result.truncated is True
    assert result.remaining == 18


def test_run_source_deadline_fires_even_when_all_detail_fetches_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the deadline must be evaluated on fresh wall-clock every
    iteration, NOT only after a successful fetch. A board whose detail pages all
    fail must still hit the deadline backstop (FR-013/SC-008) rather than loop
    until the run is killed."""
    monkeypatch.setenv("JOBAGENT_FETCH_DEADLINE_SECONDS", "10")
    items = survivors(20)
    adapter = FakeAdapter(postings=items, description_error_ids={p.external_id for p in items})
    store_ = FakeStore()

    # start=0, deadline=10; top-of-loop reads 4, 8 (two failed fetches), then 12
    # (>10) trips the deadline. Nothing described, but the run is bounded.
    ticks = iter([0, 4, 8, 12, 16, 20, 24, 28])

    def fake_clock() -> float:
        return next(ticks)

    result = resilient.run_source(
        adapter, FakeSource(), criteria=CRITERIA, store_=store_, clock=fake_clock
    )

    assert adapter.description_calls == 2  # two attempts, both failed
    assert result.new == 0
    assert result.skipped == 2
    assert result.truncated is True
    assert result.remaining == 20


# --- Backstop 3 / FR-015: forward progress skips already-stored ids --------


def test_run_source_skips_already_stored_ids_for_forward_progress() -> None:
    all_survivors = survivors(20)
    existing = {p.external_id for p in all_survivors[:5]}
    adapter = FakeAdapter(postings=all_survivors)
    store_ = FakeStore(existing_ids=existing)
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    # 20 survivors - 5 already-stored ids = 15 new detail fetches.
    assert adapter.description_calls == 15
    assert set(adapter.described_ids).isdisjoint(existing)
    assert result.new == 15


# --- FR-005: per-item detail failure is skipped, not fatal ------------------


def test_run_source_skips_single_item_detail_failure_and_continues() -> None:
    items = survivors(4)
    adapter = FakeAdapter(postings=items, description_error_ids={"2"})
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert result.skipped == 1
    assert result.new == 3  # 4 survivors - 1 skipped = 3.
    assert "2" not in {p.external_id for p in store_.upserted}
    assert {p.external_id for p in store_.upserted} == {"0", "1", "3"}


# --- Inline short-circuit: already-described postings skip the detail call -


def test_run_source_skips_detail_fetch_when_description_already_present() -> None:
    described_already = make_posting(0)
    described_already.description = "desc 0 inline"
    items = [described_already, make_posting(1)]
    adapter = FakeAdapter(postings=items)
    store_ = FakeStore()
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert adapter.description_calls == 1
    assert "0" not in adapter.described_ids
    upserted_by_id = {p.external_id: p for p in store_.upserted}
    assert upserted_by_id["0"].description == "desc 0 inline"
    assert result.new == 2


# --- Staleness / persistent-degradation bookkeeping -------------------------


def test_run_source_marks_converged_when_nothing_remains() -> None:
    adapter = FakeAdapter(postings=survivors(3))
    store_ = FakeStore(last_converged=datetime.now(timezone.utc).isoformat())
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert result.remaining == 0
    assert len(store_.converge_calls) == 1
    assert result.persistent is False


def test_run_source_persistent_true_when_stale_and_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOBAGENT_MAX_DETAIL_PER_SOURCE", "1")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    adapter = FakeAdapter(postings=survivors(3))
    store_ = FakeStore(last_converged=stale)
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    # cap 1 over 3 survivors -> 2 remaining > 0, and last_converged is 30 days
    # old > the 7-day default staleness bound, so persistent must flip True.
    assert result.remaining == 2
    assert result.persistent is True


def test_run_source_not_persistent_when_recent_and_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOBAGENT_MAX_DETAIL_PER_SOURCE", "1")
    recent = datetime.now(timezone.utc).isoformat()
    adapter = FakeAdapter(postings=survivors(3))
    store_ = FakeStore(last_converged=recent)
    result = resilient.run_source(adapter, FakeSource(), criteria=CRITERIA, store_=store_)

    assert result.remaining == 2
    assert result.persistent is False


# --- Config getters: defaults + fail-loud validation ------------------------


def test_max_detail_per_source_default_is_150(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBAGENT_MAX_DETAIL_PER_SOURCE", raising=False)
    assert resilient.max_detail_per_source() == 150


def test_fetch_deadline_seconds_default_is_300(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBAGENT_FETCH_DEADLINE_SECONDS", raising=False)
    assert resilient.fetch_deadline_seconds() == 300


def test_staleness_bound_days_default_is_7(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBAGENT_STALENESS_BOUND_DAYS", raising=False)
    assert resilient.staleness_bound_days() == 7


@pytest.mark.parametrize(
    "getter, env_name",
    [
        (resilient.max_detail_per_source, "JOBAGENT_MAX_DETAIL_PER_SOURCE"),
        (resilient.fetch_deadline_seconds, "JOBAGENT_FETCH_DEADLINE_SECONDS"),
        (resilient.staleness_bound_days, "JOBAGENT_STALENESS_BOUND_DAYS"),
    ],
)
@pytest.mark.parametrize("bad_value", ["0", "abc"])
def test_config_getters_fail_loud_on_bad_env(
    monkeypatch: pytest.MonkeyPatch, getter, env_name: str, bad_value: str
) -> None:
    monkeypatch.setenv(env_name, bad_value)
    with pytest.raises((ValueError, TypeError)):
        getter()
