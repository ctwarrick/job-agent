import importlib
from pathlib import Path

import pytest

from job_agent import score, store
from job_agent.schema import normalize


@pytest.fixture(autouse=True)
def _reload_score_after() -> None:
    """score.py reads MODEL/SALARY_FLOOR at import time; restore module
    state for other tests after each test in this file."""
    yield
    importlib.reload(score)


def test_default_model_is_claude_sonnet_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("JOBAGENT_MODEL", raising=False)
    reloaded = importlib.reload(score)
    assert reloaded.MODEL == "claude-sonnet-4-6"


def test_main_reads_runtime_files_from_data_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "profile.md").write_text("# Profile\nstub profile content\n")
    (data_dir / "screening_prompt.md").write_text("stub screening prompt\n")
    # feature 002: main() now loads filter.toml and reads store.scorable()
    (data_dir / "filter.toml").write_text(_FILTER_TOML)

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("JOBAGENT_SALARY_FLOOR", "120000")
    monkeypatch.chdir(cwd_dir)

    reloaded = importlib.reload(score)

    fake_row = {
        "fingerprint": "abc123",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "desc",
        "posted_at": None,
    }
    monkeypatch.setattr(reloaded.store, "scorable", lambda *a, **k: [fake_row])
    monkeypatch.setattr(reloaded.store, "record_filter_rejections", lambda *a, **k: None)

    captured = {}

    def fake_score_batch(client, system, profile, rows):
        captured["system"] = system
        captured["profile"] = profile
        captured["rows"] = rows
        return [
            {
                "fingerprint": "abc123",
                "skills_fit": 8,
                "seniority_fit": 7,
                "category_risk": 2,
                "bucket": "engineering",
                "comp_flag": "ok",
                "trajectory_note": "note",
            }
        ]

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    monkeypatch.setattr(reloaded, "_write_scores", lambda scores: None)

    class _StubAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(reloaded, "Anthropic", _StubAnthropic)

    reloaded.main()

    # wrong-path resolution of profile.md/screening_prompt.md (cwd instead of
    # JOBAGENT_DATA_DIR) would raise FileNotFoundError before reaching here
    assert "stub profile content" in captured["profile"]
    assert captured["system"] == "stub screening prompt\n"


def test_main_writes_scores_into_data_dir_db(tmp_path: Path, monkeypatch) -> None:
    """score.main()'s write path (_write_scores) must persist into the
    jobs.db under JOBAGENT_DATA_DIR, not a hardcoded "jobs.db" relative to
    cwd."""
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "profile.md").write_text("# Profile\n")
    (data_dir / "screening_prompt.md").write_text("screen\n")
    # feature 002: main() fail-loud requires filter.toml before scoring
    (data_dir / "filter.toml").write_text(_FILTER_TOML)

    db = data_dir / "jobs.db"
    store.init(str(db))
    posting = normalize(
        source="greenhouse",
        company="acme",
        external_id="1",
        title="Engineer",
        location="Remote",
        description="desc",
        url="https://example.com/1",
    )
    store.upsert_postings([posting], str(db))

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("JOBAGENT_SALARY_FLOOR", "120000")
    monkeypatch.chdir(cwd_dir)

    reloaded = importlib.reload(score)

    def fake_score_batch(client, system, profile, rows):
        return [
            {
                "fingerprint": posting.fingerprint,
                "skills_fit": 9,
                "seniority_fit": 8,
                "category_risk": 1,
                "bucket": "engineering",
                "comp_flag": "ok",
                "trajectory_note": "good fit",
            }
        ]

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)

    class _StubAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(reloaded, "Anthropic", _StubAnthropic)

    reloaded.main()

    assert not (cwd_dir / "jobs.db").exists()

    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT skills_fit FROM postings WHERE fingerprint=?",
            (posting.fingerprint,),
        ).fetchone()
    assert row["skills_fit"] == 9


# --- US1: filter-before-you-spend integration (feature 002) -----------------

_FILTER_TOML = """
[denylist]
title_keywords = ["sales", "accountant", "recruiter"]

[allowlist]
title_keywords = ["engineer", "engineering", "software", "developer"]

[age]
max_days = 30

[location]
remote_ok = true
regions = ["WA", "OR", "PA"]
metros = ["Remote", "Bay Area"]
"""


def _write_runtime_files(data_dir: Path) -> None:
    """Write profile.md, screening_prompt.md, and a valid filter.toml into
    data_dir, matching the resolution path score.main() reads via
    store.data_path."""
    (data_dir / "profile.md").write_text("# Profile\nstub profile content\n")
    (data_dir / "screening_prompt.md").write_text("stub screening prompt\n")
    (data_dir / "filter.toml").write_text(_FILTER_TOML)


def _setup_score_env(tmp_path: Path, monkeypatch, write_filter: bool = True) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "profile.md").write_text("# Profile\nstub profile content\n")
    (data_dir / "screening_prompt.md").write_text("stub screening prompt\n")
    if write_filter:
        (data_dir / "filter.toml").write_text(_FILTER_TOML)

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("JOBAGENT_SALARY_FLOOR", "120000")
    monkeypatch.chdir(cwd_dir)
    return data_dir, cwd_dir


def _stub_anthropic(reloaded, monkeypatch) -> None:
    class _StubAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(reloaded, "Anthropic", _StubAnthropic)


def test_main_sends_only_plausible_postings_to_llm(tmp_path: Path, monkeypatch) -> None:
    """Denylisted postings must never reach _score_batch; only the
    plausible (non-rejected) postings are passed through."""
    _setup_score_env(tmp_path, monkeypatch)
    reloaded = importlib.reload(score)

    plausible_row = {
        "fingerprint": "plausible1",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "build things",
        "posted_at": None,
    }
    denylisted_row = {
        "fingerprint": "denylisted1",
        "title": "Senior Sales Engineer",
        "company": "Beta Corp",
        "location": "Remote",
        "description": "sell things",
        "posted_at": None,
    }

    monkeypatch.setattr(
        reloaded.store, "scorable", lambda *a, **k: [plausible_row, denylisted_row]
    )
    monkeypatch.setattr(reloaded.store, "record_filter_rejections", lambda *a, **k: None)

    captured = {}

    def fake_score_batch(client, system, profile, rows):
        captured["rows"] = rows
        return [
            {
                "fingerprint": r["fingerprint"],
                "skills_fit": 8,
                "seniority_fit": 7,
                "category_risk": 2,
                "bucket": "engineering",
                "comp_flag": "ok",
                "trajectory_note": "note",
            }
            for r in rows
        ]

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    monkeypatch.setattr(reloaded, "_write_scores", lambda scores: None)
    _stub_anthropic(reloaded, monkeypatch)

    reloaded.main()

    scored_fingerprints = {r["fingerprint"] for r in captured["rows"]}
    assert scored_fingerprints == {"plausible1"}
    assert "denylisted1" not in scored_fingerprints


def test_main_persists_filter_rejections_with_reasons(tmp_path: Path, monkeypatch) -> None:
    """Rejected postings are persisted via store.record_filter_rejections
    with their fingerprint + machine-readable reason."""
    _setup_score_env(tmp_path, monkeypatch)
    reloaded = importlib.reload(score)

    plausible_row = {
        "fingerprint": "plausible2",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "build things",
        "posted_at": None,
    }
    denylisted_row = {
        "fingerprint": "denylisted2",
        "title": "Staff Accountant",
        "company": "Gamma LLC",
        "location": "Remote",
        "description": "crunch numbers",
        "posted_at": None,
    }

    monkeypatch.setattr(
        reloaded.store, "scorable", lambda *a, **k: [plausible_row, denylisted_row]
    )

    recorded = {}

    def fake_record_filter_rejections(rejections, *a, **k):
        recorded["rejections"] = list(rejections)

    monkeypatch.setattr(
        reloaded.store, "record_filter_rejections", fake_record_filter_rejections
    )

    def fake_score_batch(client, system, profile, rows):
        return [
            {
                "fingerprint": r["fingerprint"],
                "skills_fit": 8,
                "seniority_fit": 7,
                "category_risk": 2,
                "bucket": "engineering",
                "comp_flag": "ok",
                "trajectory_note": "note",
            }
            for r in rows
        ]

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    monkeypatch.setattr(reloaded, "_write_scores", lambda scores: None)
    _stub_anthropic(reloaded, monkeypatch)

    reloaded.main()

    rejections = dict(recorded["rejections"])
    assert "denylisted2" in rejections
    assert rejections["denylisted2"].startswith("function_denylist:")
    assert "plausible2" not in rejections


def test_main_reads_from_scorable_not_unscored(tmp_path: Path, monkeypatch) -> None:
    """score.main() must source rows from store.scorable() (the
    filter+score eligibility set), not the legacy store.unscored()."""
    _setup_score_env(tmp_path, monkeypatch)
    reloaded = importlib.reload(score)

    called = {"scorable": False, "unscored": False}

    def fake_scorable(*a, **k):
        called["scorable"] = True
        return []

    def fake_unscored(*a, **k):
        called["unscored"] = True
        return []

    monkeypatch.setattr(reloaded.store, "scorable", fake_scorable)
    monkeypatch.setattr(reloaded.store, "unscored", fake_unscored)
    monkeypatch.setattr(reloaded.store, "record_filter_rejections", lambda *a, **k: None)

    def fake_score_batch(client, system, profile, rows):
        raise AssertionError("messages.create / _score_batch must not be called")

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    _stub_anthropic(reloaded, monkeypatch)

    reloaded.main()

    assert called["scorable"] is True
    assert called["unscored"] is False


def test_main_exits_before_scoring_when_filter_toml_missing(tmp_path: Path, monkeypatch) -> None:
    """A missing filter.toml is fail-loud (FR-014): sys.exit before any
    _score_batch / messages.create call."""
    _setup_score_env(tmp_path, monkeypatch, write_filter=False)
    reloaded = importlib.reload(score)

    monkeypatch.setattr(reloaded.store, "scorable", lambda *a, **k: [])
    monkeypatch.setattr(reloaded.store, "record_filter_rejections", lambda *a, **k: None)

    def fake_score_batch(client, system, profile, rows):
        raise AssertionError("_score_batch must not be called when filter.toml is missing")

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    _stub_anthropic(reloaded, monkeypatch)

    with pytest.raises(SystemExit):
        reloaded.main()


def test_main_exits_before_scoring_when_filter_toml_malformed(tmp_path: Path, monkeypatch) -> None:
    """A structurally-invalid filter.toml (max_days not an int) is
    fail-loud (FR-014): sys.exit before any _score_batch call."""
    data_dir, _ = _setup_score_env(tmp_path, monkeypatch, write_filter=False)
    (data_dir / "filter.toml").write_text(
        """
[denylist]
title_keywords = ["sales"]

[allowlist]
title_keywords = []

[age]
max_days = "thirty"

[location]
remote_ok = true
regions = []
metros = []
"""
    )
    reloaded = importlib.reload(score)

    monkeypatch.setattr(reloaded.store, "scorable", lambda *a, **k: [])
    monkeypatch.setattr(reloaded.store, "record_filter_rejections", lambda *a, **k: None)

    def fake_score_batch(client, system, profile, rows):
        raise AssertionError("_score_batch must not be called when filter.toml is malformed")

    monkeypatch.setattr(reloaded, "_score_batch", fake_score_batch)
    _stub_anthropic(reloaded, monkeypatch)

    with pytest.raises(SystemExit):
        reloaded.main()
