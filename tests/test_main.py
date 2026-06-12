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

from datetime import datetime, timedelta, timezone

import pytest

import main
from job_agent import store

DIGEST_DATE = "2026-06-11"


def _setup_db(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("JOBAGENT_FORCE", raising=False)
    monkeypatch.setenv("JOBAGENT_TZ", "UTC")
    store.init(str(db))
    return db


def _patch_stages(monkeypatch, *, fetch_ok=True, score_ok=True, digest_returns=True):
    calls = []

    def fake_fetch():
        calls.append("fetch")
        if not fetch_ok:
            raise SystemExit("fetch failed")

    def fake_score():
        calls.append("score")
        if not score_ok:
            raise SystemExit("score failed")

    def fake_digest():
        calls.append("digest")
        return digest_returns

    monkeypatch.setattr(main, "fetch", type("M", (), {"main": staticmethod(fake_fetch)}))
    monkeypatch.setattr(main, "score", type("M", (), {"main": staticmethod(fake_score)}))
    monkeypatch.setattr(main, "digest", type("M", (), {"main": staticmethod(fake_digest)}))
    return calls


def _run_main_allow_exit(monkeypatch):
    """Run main.main(), returning the SystemExit code (or None if it returned)."""
    try:
        main.main()
    except SystemExit as e:
        return e.code
    return None


def test_successful_run_prints_run_success_after_confirmed_send(tmp_path, monkeypatch, capsys):
    db = _setup_db(tmp_path, monkeypatch)
    calls = _patch_stages(monkeypatch, digest_returns=True)

    code = _run_main_allow_exit(monkeypatch)

    assert code in (0, None)
    assert calls == ["fetch", "score", "digest"]
    out = capsys.readouterr().out
    assert f"RUN_SUCCESS digest_date={store.digest_date()}" in out


def test_inflight_run_is_a_noop_skip_and_exits_zero(tmp_path, monkeypatch, capsys):
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


def test_already_succeeded_date_is_noop_skip_and_prints_run_success(tmp_path, monkeypatch, capsys):
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


def test_fatal_stage_failure_records_failed_outcome_and_exits_nonzero(tmp_path, monkeypatch, capsys):
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


def test_fatal_failure_on_third_attempt_prints_run_failed_final(tmp_path, monkeypatch, capsys):
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


def test_force_bypasses_already_succeeded_but_not_inflight(tmp_path, monkeypatch, capsys):
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


def test_force_does_not_bypass_inflight_lock(tmp_path, monkeypatch, capsys):
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


def test_first_run_on_fresh_data_dir_does_not_crash(tmp_path, monkeypatch, capsys):
    """A fresh JOBAGENT_DATA_DIR with no jobs.db must not crash main.main()
    with 'no such table: runs' -- main must ensure schema init before the
    startup check (store.startup_decision/store.start_run)."""
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


def test_non_systemexit_stage_failure_records_failed_outcome(tmp_path, monkeypatch, capsys):
    """A non-SystemExit exception from a stage (e.g. RuntimeError) must
    propagate, but the run row for the digest_date should still be recorded
    as 'failed' with a non-empty detail rather than left with NULL outcome."""
    db = _setup_db(tmp_path, monkeypatch)
    today = store.digest_date()

    def fake_fetch():
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


def test_non_systemexit_failure_on_third_attempt_prints_run_failed_final(tmp_path, monkeypatch, capsys):
    """When a non-SystemExit stage failure happens on the day's 3rd attempt,
    RUN_FAILED_FINAL must still be printed."""
    db = _setup_db(tmp_path, monkeypatch)
    today = store.digest_date()

    # two prior failed attempts for today, both finished
    for _ in range(2):
        run_id = store.start_run(today, str(db))
        store.finish_run(run_id, outcome="failed", failed_sources=None, detail="boom", path=str(db))

    def fake_score():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "fetch", type("M", (), {"main": staticmethod(lambda: None)}))
    monkeypatch.setattr(main, "score", type("M", (), {"main": staticmethod(fake_score)}))
    monkeypatch.setattr(main, "digest", type("M", (), {"main": staticmethod(lambda: True)}))

    with pytest.raises(RuntimeError):
        main.main()

    out = capsys.readouterr().out
    assert f"RUN_FAILED_FINAL digest_date={today}" in out
