"""Tests for the run-lifecycle orchestration in main.py.

All three pipeline stages (fetch, score, digest) are monkeypatched — no
network, no LLM, no SMTP. These tests pin down the contract in
specs/001-azure-deployment/contracts/runtime-config.md:

  - exit 0 for no-op skip paths (in-flight lock, already-succeeded date)
  - RUN_SUCCESS digest_date=<YYYY-MM-DD> printed only after a confirmed send,
    and also on a no-op skip of an already-successful date
  - RUN_FAILED_FINAL digest_date=<YYYY-MM-DD> printed on a fatal failure when
    attempt >= 3 (the day's last scheduled tick)
  - a fatal stage failure records run outcome 'failed' and exits non-zero
  - JOBAGENT_FORCE=1 bypasses skip_succeeded but not the in-flight lock
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from job_agent import digest, store

DIGEST_DATE = "2026-06-11"


def _setup_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "jobs.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("JOBAGENT_FORCE", raising=False)
    monkeypatch.setenv("JOBAGENT_TZ", "UTC")
    store.init(str(db))
    return db


_CLEAN_SCORE = {"scored": 1, "remaining": 0, "cap_reason": None}


def _patch_stages(
    monkeypatch,
    *,
    fetch_ok: bool = True,
    score_ok: bool = True,
    digest_returns: bool = True,
    fetch_failures: list[dict] | None = None,
    partial_sources: list[dict] | None = None,
    score_result: dict | None = None,
    captured: dict | None = None,
) -> list[str]:
    """Patch the three pipeline stages with the current contracts:

    fetch.main() -> (failed_sources, partial_sources) tuple, each a list of
    dicts (both empty when healthy);
    score.main() -> {scored, remaining, cap_reason};
    digest.main(failed_sources=, scoring=, partial_sources=) -> bool.

    Defaults reproduce a clean, fully-scored success. When `captured` is given,
    the kwargs digest.main() received are recorded into it.
    """
    calls = []

    def fake_fetch() -> tuple[list[dict], list[dict]]:
        calls.append("fetch")
        if not fetch_ok:
            raise SystemExit("fetch failed")
        return (
            fetch_failures if fetch_failures is not None else [],
            partial_sources if partial_sources is not None else [],
        )

    def fake_score() -> dict:
        calls.append("score")
        if not score_ok:
            raise SystemExit("score failed")
        return score_result if score_result is not None else dict(_CLEAN_SCORE)

    def fake_digest(failed_sources=None, scoring=None, partial_sources=None) -> bool:
        calls.append("digest")
        if captured is not None:
            captured["failed_sources"] = failed_sources
            captured["scoring"] = scoring
            captured["partial_sources"] = partial_sources
        return digest_returns

    monkeypatch.setattr(main, "fetch", type("M", (), {"main": staticmethod(fake_fetch)}))
    monkeypatch.setattr(main, "score", type("M", (), {"main": staticmethod(fake_score)}))
    monkeypatch.setattr(
        main,
        "digest",
        type(
            "M",
            (),
            {
                "main": staticmethod(fake_digest),
                "_degradation_facts": staticmethod(digest._degradation_facts),
            },
        ),
    )
    return calls


def _latest_run(db: Path) -> dict:
    today = store.digest_date()
    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE digest_date=? ORDER BY id DESC LIMIT 1", (today,)
        ).fetchone()
    return dict(row)


def _run_main_allow_exit(monkeypatch) -> str | int | None:
    """Run main.main(), returning the SystemExit code (or None if it
    returned)."""
    try:
        main.main()
    except SystemExit as e:
        return e.code
    return None


def test_successful_run_prints_run_success_after_confirmed_send(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    calls = _patch_stages(monkeypatch, digest_returns=True)

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    assert calls == ["fetch", "score", "digest"]
    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out


def test_inflight_run_is_a_noop_skip_and_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    calls = _patch_stages(monkeypatch)

    now = datetime.now(timezone.utc).isoformat()
    today = store.digest_date()
    with store.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO runs (digest_date, started_at, attempt) VALUES (?, ?, 1)",
            (today, now),
        )

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    assert calls == []  # no stages run on a no-op skip


def test_already_succeeded_date_is_noop_skip_and_prints_run_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    calls = _patch_stages(monkeypatch)

    today = store.digest_date()
    run_id = store.start_run(today, str(db))
    store.finish_run(run_id, outcome="success", failed_sources=None, detail=None, path=str(db))

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    assert calls == []  # no stages re-run
    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={today}" in out


def test_fatal_stage_failure_records_failed_outcome_and_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch, score_ok=False)

    code = _run_main_allow_exit(monkeypatch)

    assert code not in (0, None)

    today = store.digest_date()
    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE digest_date=? ORDER BY id DESC LIMIT 1", (today,)
        ).fetchone()
    assert row["outcome"] == "failed"

    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={today}" not in out


def test_fatal_failure_on_third_attempt_prints_run_failed_final(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch, score_ok=False)

    today = store.digest_date()
    stale = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    # two prior failed attempts for today, both finished
    for _ in range(2):
        run_id = store.start_run(today, str(db))
        store.finish_run(run_id, outcome="failed", failed_sources=None, detail="boom", path=str(db))

    code = _run_main_allow_exit(monkeypatch)

    assert code not in (0, None)
    out = capsys.readouterr().out
    assert f"RUN_FAILED_FINAL digest_date={today}" in out


def test_force_bypasses_already_succeeded_but_not_inflight(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = _setup_db(tmp_path, monkeypatch)

    today = store.digest_date()

    # Case 1: already-succeeded date is re-run with JOBAGENT_FORCE=1
    run_id = store.start_run(today, str(db))
    store.finish_run(run_id, outcome="success", failed_sources=None, detail=None, path=str(db))

    monkeypatch.setenv("JOBAGENT_FORCE", "1")
    calls = _patch_stages(monkeypatch, digest_returns=True)

    code = _run_main_allow_exit(monkeypatch)
    assert code in (0, None)
    assert calls == ["fetch", "score", "digest"], "force should bypass skip_succeeded"


def test_force_does_not_bypass_inflight_lock(tmp_path: Path, monkeypatch, capsys) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    today = store.digest_date()

    now = datetime.now(timezone.utc).isoformat()
    with store.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO runs (digest_date, started_at, attempt) VALUES (?, ?, 1)",
            (today, now),
        )

    monkeypatch.setenv("JOBAGENT_FORCE", "1")
    calls = _patch_stages(monkeypatch)

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    assert calls == [], "in-flight lock is not bypassed by JOBAGENT_FORCE"


def test_first_run_on_fresh_data_dir_does_not_crash(tmp_path: Path, monkeypatch, capsys) -> None:
    """A fresh JOBAGENT_DATA_DIR with no jobs.db must not crash
    main.main() with 'no such table: runs' -- main must ensure schema
    init before the startup check (store.startup_decision/store.start_run).
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JOBAGENT_TZ", "UTC")
    monkeypatch.delenv("JOBAGENT_FORCE", raising=False)
    # deliberately do NOT call store.init() -- jobs.db does not exist yet

    _patch_stages(monkeypatch, digest_returns=True)

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out


def test_non_systemexit_stage_failure_records_failed_outcome(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A non-SystemExit exception from a stage (e.g. RuntimeError) must
    propagate, but the run row for the digest_date should still be recorded
    as 'failed' with a non-empty detail rather than left with NULL outcome.
    """
    db = _setup_db(tmp_path, monkeypatch)
    today = store.digest_date()

    def fake_fetch() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "fetch", type("M", (), {"main": staticmethod(fake_fetch)}))
    monkeypatch.setattr(main, "score", type("M", (), {"main": staticmethod(lambda: None)}))
    monkeypatch.setattr(main, "digest", type("M", (), {"main": staticmethod(lambda: True)}))

    with pytest.raises(RuntimeError):
        main.main()

    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE digest_date=? ORDER BY id DESC LIMIT 1", (today,)
        ).fetchone()

    assert row["outcome"] == "failed"
    assert row["detail"]


def test_non_systemexit_failure_on_third_attempt_prints_run_failed_final(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """When a non-SystemExit stage failure happens on the day's 3rd attempt,
    RUN_FAILED_FINAL must still be printed."""
    db = _setup_db(tmp_path, monkeypatch)
    today = store.digest_date()

    # two prior failed attempts for today, both finished
    for _ in range(2):
        run_id = store.start_run(today, str(db))
        store.finish_run(run_id, outcome="failed", failed_sources=None, detail="boom", path=str(db))

    def fake_score() -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "fetch", type("M", (), {"main": staticmethod(lambda: ([], []))}))
    monkeypatch.setattr(main, "score", type("M", (), {"main": staticmethod(fake_score)}))
    monkeypatch.setattr(
        main,
        "digest",
        type(
            "M",
            (),
            {
                "main": staticmethod(
                    lambda failed_sources=None, scoring=None, partial_sources=None: True
                )
            },
        ),
    )

    with pytest.raises(RuntimeError):
        main.main()

    out = capsys.readouterr().out
    assert f"RUN_FAILED_FINAL digest_date={today}" in out


# --- US3a: degraded outcome wiring (FR-005, FR-020) -------------------------


def test_clean_run_sets_success_outcome(tmp_path: Path, monkeypatch) -> None:
    """No source failures and nothing left unscored -> outcome 'success' with
    NULL failed_sources/detail."""
    db = _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch)

    _run_main_allow_exit(monkeypatch)

    row = _latest_run(db)
    assert row["outcome"] == "success"
    assert row["failed_sources"] is None
    assert row["detail"] is None


def test_source_failure_sets_degraded_outcome(tmp_path: Path, monkeypatch, capsys) -> None:
    """A failed fetch source -> outcome 'degraded', failed_sources persisted as
    JSON, detail names the source count, and RUN_SUCCESS still prints (the
    digest was delivered)."""
    db = _setup_db(tmp_path, monkeypatch)
    failures = [{"source": "greenhouse", "company_slug": "acme", "error": "boom timeout"}]
    _patch_stages(monkeypatch, fetch_failures=failures)

    _run_main_allow_exit(monkeypatch)

    row = _latest_run(db)
    assert row["outcome"] == "degraded"
    assert json.loads(row["failed_sources"]) == failures
    assert "1 source" in row["detail"]

    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out


def test_scoring_backlog_sets_degraded_outcome(tmp_path: Path, monkeypatch) -> None:
    """Unscored postings remaining (a cap) -> outcome 'degraded', failed_sources
    NULL, detail states the unscored count and cap reason."""
    db = _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch, score_result={"scored": 10, "remaining": 15, "cap_reason": "cost"})

    _run_main_allow_exit(monkeypatch)

    row = _latest_run(db)
    assert row["outcome"] == "degraded"
    assert row["failed_sources"] is None
    assert "15 unscored" in row["detail"]
    assert "cap=cost" in row["detail"]


def test_both_degradations_recorded_in_detail(tmp_path: Path, monkeypatch) -> None:
    db = _setup_db(tmp_path, monkeypatch)
    failures = [
        {"source": "greenhouse", "company_slug": "acme", "error": "x"},
        {"source": "lever", "company_slug": "foo", "error": "y"},
    ]
    _patch_stages(
        monkeypatch,
        fetch_failures=failures,
        score_result={"scored": 1, "remaining": 919, "cap_reason": "cost"},
    )

    _run_main_allow_exit(monkeypatch)

    row = _latest_run(db)
    assert row["outcome"] == "degraded"
    assert "2 sources" in row["detail"]
    assert "919 unscored" in row["detail"]


def test_degradation_context_passed_to_digest(tmp_path: Path, monkeypatch) -> None:
    """main.py forwards fetch's failure list and score's signal to
    digest.main() so the email can render the notice."""
    _setup_db(tmp_path, monkeypatch)
    failures = [{"source": "greenhouse", "company_slug": "acme", "error": "boom"}]
    scoring = {"scored": 10, "remaining": 15, "cap_reason": "cost"}
    captured: dict = {}
    _patch_stages(monkeypatch, fetch_failures=failures, score_result=scoring, captured=captured)

    _run_main_allow_exit(monkeypatch)

    assert captured["failed_sources"] == failures
    assert captured["scoring"] == scoring


def test_partial_source_sets_degraded_outcome_and_prints_run_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A partially-fetched source (backstop/skip/persistent) -> outcome
    'degraded', but the digest was delivered so RUN_SUCCESS still prints
    (FR-014)."""
    db = _setup_db(tmp_path, monkeypatch)
    partials = [
        {
            "source": "workday",
            "company_slug": "globex:Globex:wd5",
            "new": 5,
            "skipped": 2,
            "truncated": True,
            "persistent": False,
        }
    ]
    _patch_stages(monkeypatch, partial_sources=partials)

    _run_main_allow_exit(monkeypatch)

    row = _latest_run(db)
    assert row["outcome"] == "degraded"
    assert "partial" in (row["detail"] or "").lower()

    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out


def test_partial_sources_forwarded_to_digest(tmp_path: Path, monkeypatch) -> None:
    """main.py forwards fetch's partial-source list to digest.main() so the
    email can render the degraded category (FR-014)."""
    _setup_db(tmp_path, monkeypatch)
    partials = [
        {
            "source": "workday",
            "company_slug": "globex:Globex:wd5",
            "new": 0,
            "skipped": 0,
            "truncated": True,
            "persistent": True,
        }
    ]
    captured: dict = {}
    _patch_stages(monkeypatch, partial_sources=partials, captured=captured)

    _run_main_allow_exit(monkeypatch)

    assert captured["partial_sources"] == partials


# --- retention purge stage (FR-015) -----------------------------------------


def test_successful_run_invokes_purge_stage(tmp_path: Path, monkeypatch) -> None:
    """The retention purge runs as a pipeline stage on a successful run."""
    _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch, digest_returns=True)

    purge_calls: list[int] = []
    monkeypatch.setattr(
        store,
        "purge_old_postings",
        lambda *a, **k: purge_calls.append(1) or (0, 0),
        raising=False,
    )

    _run_main_allow_exit(monkeypatch)

    assert purge_calls, "purge stage should run on a successful pipeline run"


def test_purge_failure_does_not_fail_the_run(tmp_path: Path, monkeypatch, capsys) -> None:
    """Purge is post-send housekeeping: an exception in it must not abort an
    already-delivered run -- exit stays 0 and RUN_SUCCESS still prints."""
    _setup_db(tmp_path, monkeypatch)
    _patch_stages(monkeypatch, digest_returns=True)

    def boom(*a, **k):
        raise RuntimeError("purge blew up")

    monkeypatch.setattr(store, "purge_old_postings", boom, raising=False)

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out
