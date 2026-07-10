"""SQLite persistence.

Two tables:
  postings      - everything we've ever fetched, keyed by fingerprint
  applications  - your status tracking, so the agent never re-surfaces
                  things you've dismissed or already applied to

Scores from the LLM stage live on the postings row (added later) so a
digest query is a single SELECT with a WHERE on the score columns.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .schema import Posting

# In-flight runs older than this are presumed crashed (data-model.md
# "Startup check"; matches the ACA job's replicaTimeout).
INFLIGHT_TIMEOUT_SECONDS = 900

DDL = """
CREATE TABLE IF NOT EXISTS postings (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    company       TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    description   TEXT,
    url           TEXT,
    posted_at     TEXT,
    fetched_at    TEXT NOT NULL,
    -- LLM scoring stage fills these in; NULL means "not yet scored"
    skills_fit    INTEGER,
    seniority_fit INTEGER,
    category_risk INTEGER,
    rationale     TEXT,
    -- Deterministic pre-filter verdict; NULL means "not filtered"
    filter_reason TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    fingerprint TEXT PRIMARY KEY REFERENCES postings(fingerprint),
    status      TEXT NOT NULL DEFAULT 'new',   -- new|dismissed|duplicate|applied|interviewing|closed
    notes       TEXT,
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_postings_scored
    ON postings(skills_fit, category_risk);

-- One row per job execution; the idempotency lock that turns three cron
-- ticks into "one run + up to 2 retries" and the home of degraded-source
-- reporting (data-model.md "runs").
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT,   -- success|degraded|failed; NULL while running
    attempt       INTEGER NOT NULL,
    failed_sources TEXT,  -- JSON list of {source, company_slug, error}
    detail        TEXT
);

-- Per-source forward-progress watermark for resilient fetch (data-model.md
-- "source_progress table"); additive, survives postings purges.
CREATE TABLE IF NOT EXISTS source_progress (
    source            TEXT NOT NULL,
    company           TEXT NOT NULL,
    last_converged_at TEXT,
    PRIMARY KEY (source, company)
);
"""


def data_path(name: str) -> str:
    """Resolve `name` under JOBAGENT_DATA_DIR (default '.'), at call time.

    Centralizing this means the default jobs.db path, registry.toml, and the
    profile/screening-prompt runtime files all move together when
    JOBAGENT_DATA_DIR is set (e.g. an Azure Files mount), while the default
    '.' preserves today's exact local-development paths (e.g. "jobs.db",
    not "./jobs.db").
    """
    data_dir = os.environ.get("JOBAGENT_DATA_DIR", ".")
    if data_dir in ("", "."):
        return name
    return os.path.join(data_dir, name)


def _connect_args(path: str) -> tuple[str, bool]:
    """Build the (connect_string, uri_flag) pair for `sqlite3.connect`.

    Real filesystem paths are opened in URI mode with `nolock=1`, which
    disables SQLite's OS-level file locking. That locking relies on POSIX
    byte-range locks the Azure Files SMB mount doesn't support, so a plain
    `connect(path)` raises "database is locked" on first use. `:memory:` and
    paths already in `file:` URI form are passed through unchanged.

    Args:
        path: The resolved database path (e.g. "jobs.db", "/data/jobs.db",
            ":memory:", or an existing "file:..." URI).

    Returns:
        A tuple of the string to pass to `sqlite3.connect` and whether
        `uri=True` should be used.
    """
    if path == ":memory:":
        return path, False
    if path.startswith("file:"):
        return path, True
    return f"file:{quote(path)}?nolock=1", True


@contextmanager
def connect(path: str | None = None):
    """Context manager for database connections with auto-commit on success.

    Returns a sqlite3.Connection with Row factory enabled. Commits on exit
    unless an exception occurs; always closes the connection.
    """
    connect_string, uri = _connect_args(path or data_path("jobs.db"))
    conn = sqlite3.connect(connect_string, uri=uri)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(DDL)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations (SQLite lacks ADD COLUMN IF NOT EXISTS)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    if "digest_sent_at" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN digest_sent_at TEXT")
    if "filter_reason" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN filter_reason TEXT")

    _migrate_fingerprints(conn)


def _migrate_fingerprints(conn: sqlite3.Connection) -> None:
    """Re-key postings/applications to the four-part fingerprint.

    The fingerprint gained `description` as a fourth component (see
    schema.Posting.fingerprint and data-model.md "Dedupe identity
    revision"). The new key strictly subdivides the old one, so
    recomputing it can never merge two existing rows. Re-keying both
    tables in one transaction preserves scores, application status, and
    digest_sent_at; running it again is a no-op once every row already
    carries its new fingerprint.
    """
    rows = conn.execute(
        "SELECT fingerprint, title, company, location, description FROM postings"
    ).fetchall()
    for row in rows:
        new_fp = Posting(
            source="",
            company=row["company"],
            external_id="",
            title=row["title"],
            location=row["location"],
            description=row["description"] or "",
            url="",
            posted_at=None,
        ).fingerprint
        old_fp = row["fingerprint"]
        if new_fp == old_fp:
            continue
        conn.execute(
            "UPDATE postings SET fingerprint=? WHERE fingerprint=?",
            (new_fp, old_fp),
        )
        conn.execute(
            "UPDATE applications SET fingerprint=? WHERE fingerprint=?",
            (new_fp, old_fp),
        )


def upsert_postings(postings: list[Posting] | tuple[Posting, ...], path: str | None = None) -> int:
    """Insert new postings; leave existing ones (and their scores)
    untouched.

    INSERT OR IGNORE keeps us idempotent: re-running the fetcher never
    clobbers scores or re-surfaces dismissed roles. Also seeds an
    applications row in 'new' state for each brand-new posting.

    Args:
        postings: List/tuple of Posting objects to upsert.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        Count of newly inserted postings (FR-011); excludes the companion
        applications-row inserts seeded below.
    """
    rows = [p.to_row() for p in postings]
    if not rows:
        return 0
    cols = [
        "fingerprint",
        "source",
        "company",
        "external_id",
        "title",
        "location",
        "description",
        "url",
        "posted_at",
        "fetched_at",
    ]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO postings ({','.join(cols)}) VALUES ({placeholders})"
    with connect(path) as conn:
        before = conn.total_changes
        conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        after_postings = conn.total_changes
        # also seed an applications row in 'new' state for anything brand new
        conn.executemany(
            "INSERT OR IGNORE INTO applications (fingerprint, status, updated_at) "
            "VALUES (?, 'new', datetime('now'))",
            [[r["fingerprint"]] for r in rows],
        )
        return after_postings - before


def existing_external_ids(source: str, company: str, path: str | None = None) -> set[str]:
    """Fetch the external_ids already stored for a source/company pair.

    The description-independent identity resilient fetch uses to skip
    already-described survivors on later runs (FR-015 forward progress;
    contracts/resilient-fetch.md §3).

    Args:
        source: Adapter/vendor name (e.g. "greenhouse").
        company: Registry company slug.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        Set of external_id strings already stored for this source/company.
    """
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT external_id FROM postings WHERE source=? AND company=?",
            (source, company),
        ).fetchall()
        return {r["external_id"] for r in rows}


def get_last_converged(source: str, company: str, path: str | None = None) -> str | None:
    """Fetch the last-converged timestamp for a source/company pair.

    Args:
        source: Adapter/vendor name (e.g. "greenhouse").
        company: Registry company slug.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        The stored ISO-8601 UTC timestamp, or None if there is no row yet.
    """
    with connect(path) as conn:
        row = conn.execute(
            "SELECT last_converged_at FROM source_progress WHERE source=? AND company=?",
            (source, company),
        ).fetchone()
        return row["last_converged_at"] if row else None


def mark_converged(source: str, company: str, when: str, path: str | None = None) -> None:
    """Upsert the last-converged timestamp for a source/company pair.

    Called when a run finishes a source with no survivors left to describe
    (data-model.md "Convergence / staleness lifecycle"); always overwrites.

    Args:
        source: Adapter/vendor name (e.g. "greenhouse").
        company: Registry company slug.
        when: ISO-8601 UTC timestamp to store.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").
    """
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO source_progress (source, company, last_converged_at) "
            "VALUES (?, ?, ?) ON CONFLICT(source, company) DO UPDATE SET "
            "last_converged_at=excluded.last_converged_at",
            (source, company, when),
        )


def sources_by_recency(
    keys: list[tuple[str, str]], path: str | None = None
) -> list[tuple[str, str]]:
    """Order registry (source, company) keys least-recently-fully-fetched
    first (FR-006, data-model.md R4).

    Never-fetched keys (no `source_progress` row) sort first, then keys with
    the oldest `last_converged_at`; ISO-8601 timestamps compare correctly as
    plain strings. Ties -- including "everything is never-fetched" -- are
    resolved by a stable sort that preserves `keys`' input order, so a fresh
    db reproduces registry order exactly (contracts/fetch-stage.md).

    Args:
        keys: (source, company) pairs, typically built from the registry.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        `keys` reordered oldest/never-fetched first.
    """
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT source, company, last_converged_at FROM source_progress"
        ).fetchall()
    last_converged = {(r["source"], r["company"]): r["last_converged_at"] for r in rows}
    return sorted(
        keys,
        key=lambda k: (last_converged.get(k) is not None, last_converged.get(k) or ""),
    )


def seed_source(source: str, company: str, when: str, path: str | None = None) -> None:
    """Insert a last-converged timestamp only if no row exists yet.

    Called on first sighting of a source so its grace window starts now
    rather than at epoch (data-model.md "Convergence / staleness
    lifecycle"); never overwrites an existing row.

    Args:
        source: Adapter/vendor name (e.g. "greenhouse").
        company: Registry company slug.
        when: ISO-8601 UTC timestamp to store if absent.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").
    """
    with connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_progress (source, company, last_converged_at) "
            "VALUES (?, ?, ?)",
            (source, company, when),
        )


def unscored(path: str | None = None) -> list[sqlite3.Row]:
    """Fetch all postings where skills_fit is NULL (unscored).

    Args:
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        List of sqlite3.Row objects for unscored postings.
    """
    with connect(path) as conn:
        return conn.execute("SELECT * FROM postings WHERE skills_fit IS NULL").fetchall()


def scorable(path: str | None = None) -> list[sqlite3.Row]:
    """Fetch postings eligible for this run's filter + LLM pass.

    Narrower than `unscored()`: excludes rows a prior run's filter already
    rejected (`filter_reason` set), so they are never re-examined
    (data-model.md "Posting scoring lifecycle", I1).

    Args:
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        List of sqlite3.Row objects for scorable postings.
    """
    with connect(path) as conn:
        return conn.execute(
            "SELECT * FROM postings WHERE skills_fit IS NULL AND filter_reason IS NULL"
        ).fetchall()


def record_filter_rejections(
    rejections: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    path: str | None = None,
) -> None:
    """Persist the deterministic filter's verdicts in one transaction.

    Called once per run after the filter pass, before the LLM loop, so
    rejected rows are durably out of scope even if the LLM loop later
    cap-stops or crashes (FR-003, FR-007).

    Args:
        rejections: Iterable of (fingerprint, reason) pairs, e.g.
            ("abc123", "function_denylist:sales").
        path: Optional path to jobs.db; defaults to data_path("jobs.db").
    """
    rows = list(rejections)
    with connect(path) as conn:
        conn.executemany(
            "UPDATE postings SET filter_reason=? WHERE fingerprint=?",
            [(reason, fingerprint) for fingerprint, reason in rows],
        )


def purge_old_postings(
    retention_days: int | None = None, path: str | None = None
) -> tuple[int, int]:
    """Delete postings past the retention window (FR-015).

    Removes `postings` rows whose joined `applications.status` is `new`,
    `dismissed`, or `duplicate` **and** whose `fetched_at` is older than the
    retention window, together with their `applications` rows, in a single
    transaction. Postings with any other status (`applied`/`interviewing`/
    `closed`) are never purged; a posting with no `applications` row is excluded
    by the inner join and so is never purged either (data-model.md "Retention
    rules").

    `fetched_at` is an ISO-8601 UTC timestamp (schema.Posting.to_row), so the
    cutoff is a lexical string comparison.

    Args:
        retention_days: Override for the window in days; defaults to the
            JOBAGENT_RETENTION_DAYS env var, or 60.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        A (postings_deleted, applications_deleted) count tuple.
    """
    if retention_days is None:
        retention_days = int(os.environ.get("JOBAGENT_RETENTION_DAYS", "60"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    purgeable = ("new", "dismissed", "duplicate")
    status_slots = ",".join("?" for _ in purgeable)
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT p.fingerprint FROM postings p "
            f"JOIN applications a ON a.fingerprint = p.fingerprint "
            f"WHERE a.status IN ({status_slots}) AND p.fetched_at < ?",
            (*purgeable, cutoff),
        ).fetchall()
        fingerprints = [r["fingerprint"] for r in rows]
        if not fingerprints:
            return (0, 0)
        fp_slots = ",".join("?" for _ in fingerprints)
        apps_deleted = conn.execute(
            f"DELETE FROM applications WHERE fingerprint IN ({fp_slots})", fingerprints
        ).rowcount
        postings_deleted = conn.execute(
            f"DELETE FROM postings WHERE fingerprint IN ({fp_slots})", fingerprints
        ).rowcount
    return (postings_deleted, apps_deleted)


def digest_date(tz: str | None = None) -> str:
    """Today's local date (YYYY-MM-DD) in JOBAGENT_TZ
    (default America/Los_Angeles).

    This is the idempotency key for the `runs` table: FR-002 timezone
    configurability, data-model.md "runs".

    Args:
        tz: Optional timezone name; defaults to JOBAGENT_TZ env var
            or 'America/Los_Angeles'.

    Returns:
        Today's date as YYYY-MM-DD string in the specified timezone.
    """
    tz_name = tz or os.environ.get("JOBAGENT_TZ", "America/Los_Angeles")
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def start_run(digest_date: str, path: str | None = None) -> int:
    """Insert a new in-flight run row, returning its id.

    `attempt` is the 1-based count of executions for this digest_date,
    implementing the "one run + up to 2 retries" policy (FR-017, FR-018).

    Args:
        digest_date: Date key (YYYY-MM-DD) for idempotency.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        The auto-incremented id of the newly inserted run row.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    with connect(path) as conn:
        prior = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE digest_date=?", (digest_date,)
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO runs (digest_date, started_at, attempt) VALUES (?, ?, ?)",
            (digest_date, started_at, prior + 1),
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    *,
    outcome: str,
    failed_sources: list[dict] | None,
    detail: str | None,
    path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Mark a run row finished with its outcome
    (success|degraded|failed).

    If `conn` is given, the update runs on that connection without an
    intervening commit, so the caller can include it in a larger
    transaction (e.g. alongside marking postings as sent — data-model.md
    "Validation rules").

    Args:
        run_id: The run's id from start_run().
        outcome: One of 'success', 'degraded', or 'failed'.
        failed_sources: Optional list of {source, company_slug, error}
            dicts; serialized to JSON.
        detail: Optional detail message (e.g. error text).
        path: Optional path to jobs.db; ignored if conn is given.
        conn: Optional sqlite3.Connection to reuse for transaction scope.
    """
    finished_at = datetime.now(timezone.utc).isoformat()
    failed_sources_json = json.dumps(failed_sources) if failed_sources else None
    sql = "UPDATE runs SET finished_at=?, outcome=?, failed_sources=?, detail=? " "WHERE id=?"
    params = (finished_at, outcome, failed_sources_json, detail, run_id)
    if conn is not None:
        conn.execute(sql, params)
        return
    with connect(path) as c:
        c.execute(sql, params)


def startup_decision(digest_date: str, force: bool, path: str | None = None) -> str:
    """Evaluate the two-rule startup check
    (data-model.md "Startup check").

    Checks for in-flight or already-succeeded runs for the given digest_date.
    Marks stale (age > INFLIGHT_TIMEOUT_SECONDS) in-flight rows as failed.

    Args:
        digest_date: Date key (YYYY-MM-DD) for idempotency.
        force: If True, bypass skip_succeeded (but not in-flight lock).
        path: Optional path to jobs.db; defaults to data_path("jobs.db").

    Returns:
        One of:
          - "skip_inflight": another execution for digest_date is running
            (NULL-outcome row within INFLIGHT_TIMEOUT_SECONDS); not
            bypassed by force.
          - "skip_succeeded": digest_date already has a success/degraded
            row; bypassed by force=True.
          - "proceed": no blocking row, or a stale in-flight row was
            marked failed.
    """
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE digest_date=? ORDER BY id", (digest_date,)
        ).fetchall()

        now = datetime.now(timezone.utc)
        for row in rows:
            if row["outcome"] is None:
                started_at = datetime.fromisoformat(row["started_at"])
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                age = (now - started_at).total_seconds()
                if age < INFLIGHT_TIMEOUT_SECONDS:
                    return "skip_inflight"
                # stale crashed attempt: mark failed and keep checking
                finish_run(
                    row["id"],
                    outcome="failed",
                    failed_sources=None,
                    detail="marked failed: stale in-flight run at startup",
                    conn=conn,
                )

        for row in rows:
            if row["outcome"] in ("success", "degraded"):
                if force:
                    return "proceed"
                return "skip_succeeded"

    return "proceed"
