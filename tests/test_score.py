import importlib

import pytest

from job_agent import score, store
from job_agent.schema import normalize


@pytest.fixture(autouse=True)
def _reload_score_after():
    """score.py reads MODEL/SALARY_FLOOR at import time; restore module
    state for other tests after each test in this file."""
    yield
    importlib.reload(score)


def test_default_model_is_claude_sonnet_when_env_unset(monkeypatch):
    monkeypatch.delenv("JOBAGENT_MODEL", raising=False)
    reloaded = importlib.reload(score)
    assert reloaded.MODEL == "claude-sonnet-4-6"


def test_main_reads_runtime_files_from_data_dir(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "profile.md").write_text("# Profile\nstub profile content\n")
    (data_dir / "screening_prompt.md").write_text("stub screening prompt\n")

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
    }
    monkeypatch.setattr(reloaded.store, "unscored", lambda *a, **k: [fake_row])

    captured = {}

    def fake_score_batch(client, system, profile, rows):
        captured["system"] = system
        captured["profile"] = profile
        captured["rows"] = rows
        return [{"fingerprint": "abc123", "skills_fit": 8, "seniority_fit": 7,
                  "category_risk": 2, "bucket": "engineering",
                  "comp_flag": "ok", "trajectory_note": "note"}]

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


def test_main_writes_scores_into_data_dir_db(tmp_path, monkeypatch):
    """score.main()'s write path (_write_scores) must persist into the
    jobs.db under JOBAGENT_DATA_DIR, not a hardcoded "jobs.db" relative to
    cwd."""
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "profile.md").write_text("# Profile\n")
    (data_dir / "screening_prompt.md").write_text("screen\n")

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
        return [{
            "fingerprint": posting.fingerprint,
            "skills_fit": 9,
            "seniority_fit": 8,
            "category_risk": 1,
            "bucket": "engineering",
            "comp_flag": "ok",
            "trajectory_note": "good fit",
        }]

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
