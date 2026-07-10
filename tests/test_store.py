import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from job_agent import store
from job_agent.schema import Posting, normalize


def _posting(**overrides: str | int) -> Posting:
    fields = dict(
        source="greenhouse",
        company="acme",
        external_id="1",
        title="Engineer",
        location="Remote",
        description="desc",
        url="https://example.com/1",
    )
    fields.update(overrides)
    return normalize(**fields)


def _spy_connect(monkeypatch, calls: list[tuple]) -> None:
    """Patch store.sqlite3.connect to record call args and still connect.

    Each call's positional/keyword arguments are appended to `calls` as a
    `(args, kwargs)` tuple, and the call is delegated to the real
    `sqlite3.connect` so `with store.connect(...)` still yields a working
    connection.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        calls: List to append `(args, kwargs)` tuples to.
    """
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store.sqlite3, "connect", spy)


# --- connect() URI/nolock behavior (Azure Files SMB fix) --------------------


def test_connect_normal_path_uses_nolock_uri(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple] = []
    _spy_connect(monkeypatch, calls)

    db = str(tmp_path / "jobs.db")
    with store.connect(db) as conn:
        conn.execute("SELECT 1")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == f"file:{db}?nolock=1"
    assert kwargs.get("uri") is True


def test_connect_memory_path_passes_through_unchanged(monkeypatch) -> None:
    calls: list[tuple] = []
    _spy_connect(monkeypatch, calls)

    with store.connect(":memory:") as conn:
        conn.execute("SELECT 1")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == ":memory:"
    assert kwargs.get("uri", False) is False


def test_connect_path_with_space_is_percent_encoded_and_round_trips(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple] = []
    _spy_connect(monkeypatch, calls)

    dir_with_space = tmp_path / "a b"
    dir_with_space.mkdir(parents=True)
    db = str(dir_with_space / "jobs.db")

    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)

    rows = store.unscored(db)
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == posting.fingerprint

    connect_call = calls[0]
    args, kwargs = connect_call
    assert args[0].startswith("file:")
    assert kwargs.get("uri") is True
    assert " " not in args[0]


def test_init_and_round_trip_on_plain_tmp_path_db(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()

    store.upsert_postings([posting], db)

    rows = store.unscored(db)
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == posting.fingerprint


def test_init_creates_tables(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    with store.connect(db) as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"postings", "applications"}.issubset(tables)


def test_upsert_postings_is_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()

    # FR-011: return value counts the postings insert only, not the
    # companion applications insert (previously total_changes double-counted
    # 1 postings row + 1 applications row == 2 for one brand-new posting).
    assert store.upsert_postings([posting], db) == 1
    # re-running with the same posting changes nothing
    assert store.upsert_postings([posting], db) == 0


def test_upsert_postings_counts_only_new_postings_not_double(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    # Distinct titles -> distinct fingerprints, so all 3 are brand-new rows.
    postings = [
        _posting(external_id=str(i), title=title, url=f"https://example.com/{i}")
        for i, title in enumerate(["Engineer", "Designer", "Analyst"])
    ]

    # K=3 brand-new postings -> 3 postings-table inserts, not 2*K=6.
    assert store.upsert_postings(postings, db) == 3


def test_upsert_postings_returns_zero_when_all_already_exist(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    postings = [
        _posting(external_id=str(i), title=title, url=f"https://example.com/{i}")
        for i, title in enumerate(["Engineer", "Designer", "Analyst"])
    ]
    store.upsert_postings(postings, db)

    assert store.upsert_postings(postings, db) == 0


def test_unscored_returns_only_unscored_postings(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)

    rows = store.unscored(db)
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == posting.fingerprint

    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=7 WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    assert store.unscored(db) == []


# --- filter_reason migration + scorable() + record_filter_rejections -------


def test_migrate_adds_filter_reason_column(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    with store.connect(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    assert "filter_reason" in cols


def test_migrate_filter_reason_is_idempotent(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    # running init/_migrate again must not error and the column stays present
    store.init(db)
    with store.connect(db) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(postings)")]
    assert cols.count("filter_reason") == 1


def test_migrate_does_not_alter_existing_skills_fit(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=7 WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    # re-running the migration must leave the existing score untouched (I4)
    store.init(db)

    with store.connect(db) as conn:
        row = conn.execute(
            "SELECT skills_fit FROM postings WHERE fingerprint=?",
            (posting.fingerprint,),
        ).fetchone()
    assert row["skills_fit"] == 7


def test_scorable_includes_unscored_unfiltered_row(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)

    rows = store.scorable(db)
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == posting.fingerprint
    assert rows[0]["filter_reason"] is None


def test_scorable_excludes_already_scored_row(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=8 WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    assert store.scorable(db) == []


def test_scorable_excludes_filtered_row_but_unscored_still_includes_it(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET filter_reason='function_denylist:sales' " "WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    assert store.scorable(db) == []
    # unscored() (skills_fit IS NULL only) is unchanged and still returns it
    unscored_rows = store.unscored(db)
    assert len(unscored_rows) == 1
    assert unscored_rows[0]["fingerprint"] == posting.fingerprint


def test_record_filter_rejections_sets_reasons(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    rejected = _posting(external_id="1", url="https://example.com/1")
    # Distinct title -> distinct fingerprint (fingerprint excludes external_id/url),
    # otherwise INSERT OR IGNORE would collapse these into one row.
    untouched = _posting(external_id="2", title="Designer", url="https://example.com/2")
    store.upsert_postings([rejected, untouched], db)

    store.record_filter_rejections([(rejected.fingerprint, "function_denylist:sales")], path=db)

    with store.connect(db) as conn:
        rejected_row = conn.execute(
            "SELECT filter_reason FROM postings WHERE fingerprint=?",
            (rejected.fingerprint,),
        ).fetchone()
        untouched_row = conn.execute(
            "SELECT filter_reason FROM postings WHERE fingerprint=?",
            (untouched.fingerprint,),
        ).fetchone()

    assert rejected_row["filter_reason"] == "function_denylist:sales"
    assert untouched_row["filter_reason"] is None


def test_init_default_path_uses_data_dir(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.chdir(cwd_dir)

    store.init()

    assert (data_dir / "jobs.db").exists()
    assert not (cwd_dir / "jobs.db").exists()


def test_data_path_defaults_to_local_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)
    assert store.data_path("jobs.db") == "jobs.db"


def test_migrate_rekeys_fingerprints_and_preserves_state(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")

    title, company, location, description = "Engineer", "Acme", "Remote", "Build things."
    old_fingerprint = hashlib.sha256(
        f"{title.lower()}|{company.lower()}|{location.lower()}".encode("utf-8")
    ).hexdigest()[:16]
    new_fingerprint = hashlib.sha256(
        f"{title.lower()}|{company.lower()}|{location.lower()}|{description.lower()}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    assert old_fingerprint != new_fingerprint

    with store.connect(db) as conn:
        conn.executescript(store.DDL)
        conn.execute("ALTER TABLE postings ADD COLUMN digest_sent_at TEXT")
        conn.execute(
            "INSERT INTO postings (fingerprint, source, company, external_id, title, "
            "location, description, url, posted_at, fetched_at, skills_fit, "
            "seniority_fit, category_risk, rationale, digest_sent_at) "
            "VALUES (?, 'greenhouse', ?, '1', ?, ?, ?, 'https://example.com/1', "
            "NULL, '2026-01-01T00:00:00+00:00', 8, 7, 2, 'looks good', '2026-01-02T00:00:00+00:00')",
            (old_fingerprint, company, title, location, description),
        )
        conn.execute(
            "INSERT INTO applications (fingerprint, status, notes, updated_at) "
            "VALUES (?, 'dismissed', 'not interested', '2026-01-02T00:00:00+00:00')",
            (old_fingerprint,),
        )

    store.init(db)

    with store.connect(db) as conn:
        posting = conn.execute(
            "SELECT * FROM postings WHERE fingerprint=?", (new_fingerprint,)
        ).fetchone()
        application = conn.execute(
            "SELECT * FROM applications WHERE fingerprint=?", (new_fingerprint,)
        ).fetchone()
        old_posting = conn.execute(
            "SELECT * FROM postings WHERE fingerprint=?", (old_fingerprint,)
        ).fetchone()

    assert old_posting is None
    assert posting is not None
    assert posting["skills_fit"] == 8
    assert posting["seniority_fit"] == 7
    assert posting["category_risk"] == 2
    assert posting["rationale"] == "looks good"
    assert posting["digest_sent_at"] == "2026-01-02T00:00:00+00:00"

    assert application is not None
    assert application["status"] == "dismissed"
    assert application["notes"] == "not interested"

    # idempotent: running init again does not error and leaves state intact
    store.init(db)
    with store.connect(db) as conn:
        postings = conn.execute("SELECT * FROM postings").fetchall()
        applications = conn.execute("SELECT * FROM applications").fetchall()
    assert len(postings) == 1
    assert len(applications) == 1
    assert postings[0]["fingerprint"] == new_fingerprint
    assert applications[0]["fingerprint"] == new_fingerprint


# --- runs table (data-model.md "runs") -------------------------------------


def test_init_creates_runs_table_with_expected_columns(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    with store.connect(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    assert cols == {
        "id",
        "digest_date",
        "started_at",
        "finished_at",
        "outcome",
        "attempt",
        "failed_sources",
        "detail",
    }


def test_start_run_assigns_one_based_attempt_per_digest_date(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    first_id = store.start_run("2026-06-11", db)
    second_id = store.start_run("2026-06-11", db)
    other_day_id = store.start_run("2026-06-12", db)

    with store.connect(db) as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM runs").fetchall()}

    assert rows[first_id]["attempt"] == 1
    assert rows[first_id]["digest_date"] == "2026-06-11"
    assert rows[first_id]["outcome"] is None
    assert rows[first_id]["finished_at"] is None
    assert rows[second_id]["attempt"] == 2
    assert rows[other_day_id]["attempt"] == 1


def test_finish_run_sets_outcome_and_failure_detail(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    run_id = store.start_run("2026-06-11", db)
    store.finish_run(
        run_id,
        outcome="degraded",
        failed_sources=[{"source": "greenhouse", "company_slug": "acme", "error": "404"}],
        detail="1 source failed",
        path=db,
    )

    with store.connect(db) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    assert row["outcome"] == "degraded"
    assert row["finished_at"] is not None
    assert row["detail"] == "1 source failed"
    assert "greenhouse" in row["failed_sources"]


def test_startup_decision_proceeds_with_no_prior_runs(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    assert store.startup_decision("2026-06-11", force=False, path=db) == "proceed"


def test_startup_decision_blocks_on_fresh_inflight_run_even_with_force(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    now = datetime.now(timezone.utc).isoformat()
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO runs (digest_date, started_at, attempt) VALUES (?, ?, 1)",
            ("2026-06-11", now),
        )

    assert store.startup_decision("2026-06-11", force=False, path=db) == "skip_inflight"
    # not bypassed by JOBAGENT_FORCE
    assert store.startup_decision("2026-06-11", force=True, path=db) == "skip_inflight"


def test_startup_decision_marks_stale_inflight_failed_and_proceeds(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    stale = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
    with store.connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO runs (digest_date, started_at, attempt) VALUES (?, ?, 1)",
            ("2026-06-11", stale),
        )
        run_id = cur.lastrowid

    assert store.startup_decision("2026-06-11", force=False, path=db) == "proceed"

    with store.connect(db) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row["outcome"] == "failed"


def test_startup_decision_skips_already_succeeded_unless_forced(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    run_id = store.start_run("2026-06-11", db)
    store.finish_run(run_id, outcome="success", failed_sources=None, detail=None, path=db)

    assert store.startup_decision("2026-06-11", force=False, path=db) == "skip_succeeded"
    assert store.startup_decision("2026-06-11", force=True, path=db) == "proceed"


def test_startup_decision_skips_degraded_outcome_too(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    run_id = store.start_run("2026-06-11", db)
    store.finish_run(run_id, outcome="degraded", failed_sources=None, detail="1 failed", path=db)

    assert store.startup_decision("2026-06-11", force=False, path=db) == "skip_succeeded"


# --- digest_date (JOBAGENT_TZ, zoneinfo) ------------------------------------


def test_digest_date_defaults_to_los_angeles(monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_TZ", raising=False)
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert store.digest_date() == expected


def test_digest_date_honors_jobagent_tz(monkeypatch) -> None:
    monkeypatch.setenv("JOBAGENT_TZ", "UTC")
    expected = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
    assert store.digest_date() == expected


# --- existing_external_ids (contracts/resilient-fetch.md §3) ---------------


def test_existing_external_ids_empty_when_nothing_stored(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    assert store.existing_external_ids("greenhouse", "acme", db) == set()


def test_existing_external_ids_returns_stored_ids_for_source_and_company(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    postings = [
        _posting(external_id="1", title="Engineer", url="https://example.com/1"),
        _posting(external_id="2", title="Designer", url="https://example.com/2"),
    ]
    store.upsert_postings(postings, db)

    assert store.existing_external_ids("greenhouse", "acme", db) == {"1", "2"}


def test_existing_external_ids_scoped_by_source_and_company(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    # Distinct titles -> distinct fingerprints so INSERT OR IGNORE doesn't
    # collapse these rows together (fingerprint = title|company|location|desc).
    same_source_other_company = _posting(
        source="greenhouse", company="globex", external_id="9", title="Other Company"
    )
    other_source_same_company = _posting(
        source="lever", company="acme", external_id="9", title="Other Source"
    )
    target = _posting(source="greenhouse", company="acme", external_id="1", title="Engineer")
    store.upsert_postings([same_source_other_company, other_source_same_company, target], db)

    assert store.existing_external_ids("greenhouse", "acme", db) == {"1"}


# --- source_progress table (data-model.md "source_progress table") ---------


def test_init_creates_source_progress_table(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    with store.connect(db) as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "source_progress" in tables


def test_get_last_converged_returns_none_before_any_mark_or_seed(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    assert store.get_last_converged("greenhouse", "acme", db) is None


def test_mark_converged_then_get_returns_stored_value_and_overwrites(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    store.mark_converged("greenhouse", "acme", "2026-06-11T00:00:00+00:00", db)
    assert store.get_last_converged("greenhouse", "acme", db) == "2026-06-11T00:00:00+00:00"

    # a second mark_converged with a different value overwrites
    store.mark_converged("greenhouse", "acme", "2026-06-12T00:00:00+00:00", db)
    assert store.get_last_converged("greenhouse", "acme", db) == "2026-06-12T00:00:00+00:00"


def test_seed_source_sets_value_when_absent_but_does_not_overwrite(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    store.seed_source("greenhouse", "acme", "2026-06-11T00:00:00+00:00", db)
    assert store.get_last_converged("greenhouse", "acme", db) == "2026-06-11T00:00:00+00:00"

    # a later seed_source with a different value leaves the original untouched
    store.seed_source("greenhouse", "acme", "2026-06-20T00:00:00+00:00", db)
    assert store.get_last_converged("greenhouse", "acme", db) == "2026-06-11T00:00:00+00:00"

    # but mark_converged after a seed DOES overwrite
    store.mark_converged("greenhouse", "acme", "2026-06-25T00:00:00+00:00", db)
    assert store.get_last_converged("greenhouse", "acme", db) == "2026-06-25T00:00:00+00:00"


def test_migrate_adding_source_progress_does_not_alter_existing_score(
    tmp_path: Path,
) -> None:
    # FR-009 preservation, following the pattern of
    # test_migrate_does_not_alter_existing_skills_fit: the new table is purely
    # additive and an existing posting/score row is untouched by store.init().
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()
    store.upsert_postings([posting], db)
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=7 WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    # re-running init (which creates source_progress) must leave the
    # existing score untouched
    store.init(db)

    with store.connect(db) as conn:
        row = conn.execute(
            "SELECT skills_fit FROM postings WHERE fingerprint=?",
            (posting.fingerprint,),
        ).fetchone()
    assert row["skills_fit"] == 7


# --- sources_by_recency (007 US2/T008: dispatch ordering, data-model.md R4) -


def test_sources_by_recency_never_fetched_sorts_first(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    store.mark_converged("greenhouse", "acme", "2026-06-01T00:00:00+00:00", db)

    # The never-fetched key ("lever", "globex") is placed SECOND in the input
    # so a naive "echo input order" implementation would not coincidentally
    # pass -- it must still sort first (NULLS FIRST).
    keys = [("greenhouse", "acme"), ("lever", "globex")]
    ordered = store.sources_by_recency(keys, db)

    assert ordered == [("lever", "globex"), ("greenhouse", "acme")]


def test_sources_by_recency_orders_converged_keys_oldest_first(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)
    store.mark_converged("greenhouse", "acme", "2026-06-03T00:00:00+00:00", db)
    store.mark_converged("lever", "globex", "2026-06-01T00:00:00+00:00", db)
    store.mark_converged("workday", "initech", "2026-06-02T00:00:00+00:00", db)

    keys = [("greenhouse", "acme"), ("lever", "globex"), ("workday", "initech")]
    ordered = store.sources_by_recency(keys, db)

    # oldest last_converged_at first: 06-01 < 06-02 < 06-03
    assert ordered == [("lever", "globex"), ("workday", "initech"), ("greenhouse", "acme")]


def test_sources_by_recency_preserves_input_order_for_equal_timestamps(
    tmp_path: Path,
) -> None:
    """Ties (including all-never-fetched) are a STABLE sort on the input
    order, so concurrency=1 reproduces registry order on a fresh db
    (contracts/fetch-stage.md)."""
    db = str(tmp_path / "jobs.db")
    store.init(db)
    same = "2026-06-01T00:00:00+00:00"
    store.mark_converged("greenhouse", "acme", same, db)
    store.mark_converged("lever", "globex", same, db)
    store.mark_converged("workday", "initech", same, db)

    keys = [("workday", "initech"), ("greenhouse", "acme"), ("lever", "globex")]
    assert store.sources_by_recency(keys, db) == keys


def test_sources_by_recency_all_never_fetched_preserves_registry_order(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "jobs.db")
    store.init(db)

    keys = [("workday", "initech"), ("greenhouse", "acme"), ("lever", "globex")]
    assert store.sources_by_recency(keys, db) == keys


# --- retention purge (FR-015, data-model.md "Retention rules") --------------


def _seed_aged_posting(db: str, *, title: str, status: str, age_days: int) -> str:
    """Insert one posting (+ its seeded application), then backdate
    fetched_at and set the application status.

    Title varies per call so each posting gets a distinct fingerprint.
    Returns the posting fingerprint.
    """
    p = _posting(title=title)
    store.upsert_postings([p], path=db)
    fetched_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE postings SET fetched_at=? WHERE fingerprint=?",
            (fetched_at, p.fingerprint),
        )
        conn.execute(
            "UPDATE applications SET status=? WHERE fingerprint=?",
            (status, p.fingerprint),
        )
    return p.fingerprint


def _fingerprints(db: str, table: str) -> set[str]:
    with store.connect(db) as conn:
        return {r["fingerprint"] for r in conn.execute(f"SELECT fingerprint FROM {table}")}


def test_purge_deletes_old_new_posting_and_its_application(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_RETENTION_DAYS", raising=False)
    db = str(tmp_path / "jobs.db")
    store.init(db)
    fp = _seed_aged_posting(db, title="Old New", status="new", age_days=90)

    deleted = store.purge_old_postings(path=db)

    assert deleted == (1, 1)
    assert fp not in _fingerprints(db, "postings")
    assert fp not in _fingerprints(db, "applications")


def test_purge_deletes_old_dismissed_and_duplicate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_RETENTION_DAYS", raising=False)
    db = str(tmp_path / "jobs.db")
    store.init(db)
    dismissed = _seed_aged_posting(db, title="Old Dismissed", status="dismissed", age_days=90)
    duplicate = _seed_aged_posting(db, title="Old Duplicate", status="duplicate", age_days=90)

    store.purge_old_postings(path=db)

    survivors = _fingerprints(db, "postings")
    assert dismissed not in survivors
    assert duplicate not in survivors


def test_purge_never_touches_active_statuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_RETENTION_DAYS", raising=False)
    db = str(tmp_path / "jobs.db")
    store.init(db)
    kept = [
        _seed_aged_posting(db, title=f"Old {status}", status=status, age_days=365)
        for status in ("applied", "interviewing", "closed")
    ]

    deleted = store.purge_old_postings(path=db)

    assert deleted == (0, 0)
    survivors = _fingerprints(db, "postings")
    assert all(fp in survivors for fp in kept)


def test_purge_keeps_recent_purgeable_postings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_RETENTION_DAYS", raising=False)
    db = str(tmp_path / "jobs.db")
    store.init(db)
    fp = _seed_aged_posting(db, title="Recent New", status="new", age_days=10)

    deleted = store.purge_old_postings(path=db)

    assert deleted == (0, 0)
    assert fp in _fingerprints(db, "postings")


def test_purge_respects_custom_retention_window(tmp_path: Path, monkeypatch) -> None:
    # Window = 30 days: age 31 is purged, age 29 is retained (strict cutoff).
    monkeypatch.setenv("JOBAGENT_RETENTION_DAYS", "30")
    db = str(tmp_path / "jobs.db")
    store.init(db)
    old = _seed_aged_posting(db, title="Just Past", status="new", age_days=31)
    recent = _seed_aged_posting(db, title="Just Inside", status="new", age_days=29)

    store.purge_old_postings(path=db)

    survivors = _fingerprints(db, "postings")
    assert old not in survivors
    assert recent in survivors
