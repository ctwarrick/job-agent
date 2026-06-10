"""SQLite persistence.

Two tables:
  postings      - everything we've ever fetched, keyed by fingerprint
  applications  - your status tracking, so the agent never re-surfaces
                  things you've dismissed or already applied to

Scores from the LLM stage live on the postings row (added later) so a
digest query is a single SELECT with a WHERE on the score columns.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterable

from .schema import Posting

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
    rationale     TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    fingerprint TEXT PRIMARY KEY REFERENCES postings(fingerprint),
    status      TEXT NOT NULL DEFAULT 'new',   -- new|dismissed|applied|interviewing|closed
    notes       TEXT,
    updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_postings_scored
    ON postings(skills_fit, category_risk);
"""


@contextmanager
def connect(path: str = "jobs.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path: str = "jobs.db") -> None:
    with connect(path) as conn:
        conn.executescript(DDL)
        _migrate(conn)


def _migrate(conn) -> None:
    """Idempotent schema migrations (SQLite lacks ADD COLUMN IF NOT EXISTS)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    if "digest_sent_at" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN digest_sent_at TEXT")


def upsert_postings(postings: Iterable[Posting], path: str = "jobs.db") -> int:
    """Insert new postings; leave existing ones (and their scores) untouched.

    INSERT OR IGNORE keeps us idempotent: re-running the fetcher never
    clobbers scores or re-surfaces dismissed roles.
    """
    rows = [p.to_row() for p in postings]
    if not rows:
        return 0
    cols = ["fingerprint", "source", "company", "external_id", "title",
            "location", "description", "url", "posted_at", "fetched_at"]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO postings ({','.join(cols)}) VALUES ({placeholders})"
    with connect(path) as conn:
        before = conn.total_changes
        conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        # also seed an applications row in 'new' state for anything brand new
        conn.executemany(
            "INSERT OR IGNORE INTO applications (fingerprint, status, updated_at) "
            "VALUES (?, 'new', datetime('now'))",
            [[r["fingerprint"]] for r in rows],
        )
        return conn.total_changes - before


def unscored(path: str = "jobs.db") -> list[sqlite3.Row]:
    """Postings that still need LLM scoring."""
    with connect(path) as conn:
        return conn.execute(
            "SELECT * FROM postings WHERE skills_fit IS NULL"
        ).fetchall()
