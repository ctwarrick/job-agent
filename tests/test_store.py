from job_agent import store
from job_agent.schema import normalize


def _posting(**overrides):
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


def test_init_creates_tables(tmp_path):
    db = str(tmp_path / "jobs.db")
    store.init(db)
    with store.connect(db) as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"postings", "applications"}.issubset(tables)


def test_upsert_postings_is_idempotent(tmp_path):
    db = str(tmp_path / "jobs.db")
    store.init(db)
    posting = _posting()

    # one new posting row + one new applications row
    assert store.upsert_postings([posting], db) == 2
    # re-running with the same posting changes nothing
    assert store.upsert_postings([posting], db) == 0


def test_unscored_returns_only_unscored_postings(tmp_path):
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
