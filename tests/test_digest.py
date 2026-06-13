from pathlib import Path

from job_agent import digest
from job_agent.digest import _group, _render_text


def test_group_buckets_by_field_and_defaults_to_other() -> None:
    rows = [
        {"bucket": "engineering", "title": "A"},
        {"bucket": "tpm", "title": "B"},
        {"title": "C"},
    ]
    groups = _group(rows)
    assert set(groups) == {"engineering", "tpm", "other"}
    assert groups["other"][0]["title"] == "C"


def test_render_text_includes_posting_details() -> None:
    groups = {
        "engineering": [
            {
                "title": "Engineer",
                "company": "Acme",
                "location": "Remote",
                "skills_fit": 8,
                "category_risk": 2,
                "url": "https://example.com",
                "comp_flag": "ok",
            }
        ]
    }
    text = _render_text(groups)
    assert "Engineer" in text
    assert "Acme" in text
    assert "https://example.com" in text
    assert "LOWBALL" not in text


def test_render_text_flags_lowball_comp() -> None:
    groups = {
        "engineering": [
            {
                "title": "Engineer",
                "company": "Acme",
                "location": "Remote",
                "skills_fit": 8,
                "category_risk": 2,
                "url": "https://example.com",
                "comp_flag": "lowball",
            }
        ]
    }
    assert "LOWBALL" in _render_text(groups)


# --- empty-day notice + confirmed-send return (FR-003, FR-004) -------------


def _set_smtp_env(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("DIGEST_TO", "me@example.com")
    monkeypatch.delenv("DIGEST_DRY_RUN", raising=False)
    monkeypatch.delenv("JOBAGENT_DATA_DIR", raising=False)


def test_main_sends_no_new_matches_notice_when_nothing_qualifies(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "jobs.db"
    from job_agent import store

    store.init(str(db))
    monkeypatch.chdir(tmp_path)

    _set_smtp_env(monkeypatch)

    sent = {}

    def fake_send(subject: str, text: str, html: str) -> None:
        sent["subject"] = subject
        sent["text"] = text

    monkeypatch.setattr(digest, "_send", fake_send)

    result = digest.main()

    assert sent, "expected an email to be sent for an empty day"
    assert "no new" in sent["subject"].lower() or "no new" in sent["text"].lower()
    assert result is True


def test_main_returns_true_only_after_confirmed_send(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "jobs.db"
    from job_agent import store
    from job_agent.schema import normalize

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
    with store.connect(str(db)) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=8, seniority_fit=7, category_risk=2, "
            "rationale='{}' WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    monkeypatch.chdir(tmp_path)
    _set_smtp_env(monkeypatch)

    monkeypatch.setattr(digest, "_send", lambda subject, text, html: None)

    result = digest.main()

    assert result is True
    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT digest_sent_at FROM postings WHERE fingerprint=?",
            (posting.fingerprint,),
        ).fetchone()
    assert row["digest_sent_at"] is not None


def test_main_returns_false_when_send_fails(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "jobs.db"
    from job_agent import store

    store.init(str(db))
    monkeypatch.chdir(tmp_path)

    _set_smtp_env(monkeypatch)

    def boom(subject: str, text: str, html: str) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr(digest, "_send", boom)

    result = digest.main()

    assert result is False


def test_main_dry_run_does_not_send_but_still_reports(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "jobs.db"
    from job_agent import store

    store.init(str(db))
    monkeypatch.chdir(tmp_path)

    _set_smtp_env(monkeypatch)
    monkeypatch.setenv("DIGEST_DRY_RUN", "1")

    called = {"sent": False}
    monkeypatch.setattr(digest, "_send", lambda *a, **k: called.__setitem__("sent", True))

    digest.main()

    assert called["sent"] is False


# --- JOBAGENT_DATA_DIR resolution (defect 2) --------------------------------


def test_main_reads_and_marks_sent_in_data_dir_db(tmp_path: Path, monkeypatch) -> None:
    """digest.main() must resolve jobs.db through JOBAGENT_DATA_DIR, not a
    hardcoded "jobs.db" relative to cwd: a qualifying posting seeded into the
    data-dir db must be picked up and marked sent there, even when cwd is
    elsewhere and has no jobs.db of its own."""
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    db = data_dir / "jobs.db"

    from job_agent import store
    from job_agent.schema import normalize

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
    with store.connect(str(db)) as conn:
        conn.execute(
            "UPDATE postings SET skills_fit=8, seniority_fit=7, category_risk=2, "
            "rationale='{}' WHERE fingerprint=?",
            (posting.fingerprint,),
        )

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.chdir(cwd_dir)
    _set_smtp_env_keep_data_dir(monkeypatch)

    monkeypatch.setattr(digest, "_send", lambda subject, text, html: None)

    result = digest.main()

    assert result is True
    assert not (cwd_dir / "jobs.db").exists()

    with store.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT digest_sent_at FROM postings WHERE fingerprint=?",
            (posting.fingerprint,),
        ).fetchone()
    assert row["digest_sent_at"] is not None


def _set_smtp_env_keep_data_dir(monkeypatch) -> None:
    """Like _set_smtp_env but leaves JOBAGENT_DATA_DIR alone."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("DIGEST_TO", "me@example.com")
    monkeypatch.delenv("DIGEST_DRY_RUN", raising=False)


# --- US3a: degradation notices (FR-005, FR-020) -----------------------------

from job_agent.digest import _degradation_html, _degradation_text, _render_html

_FAILED_SOURCES = [
    {"source": "greenhouse", "company_slug": "acme", "error": "boom timeout"},
    {"source": "lever", "company_slug": "foo", "error": "404 secret-token-xyz"},
]
_SCORING_BACKLOG = {"scored": 10, "remaining": 15, "cap_reason": "cost"}


def test_degradation_text_names_each_failed_source() -> None:
    text = _degradation_text(_FAILED_SOURCES, None)
    assert "greenhouse" in text and "acme" in text
    assert "lever" in text and "foo" in text


def test_degradation_html_names_each_failed_source() -> None:
    html = _degradation_html(_FAILED_SOURCES, None)
    assert "greenhouse" in html and "acme" in html
    assert "lever" in html and "foo" in html


def test_degradation_text_omits_raw_error_text() -> None:
    """A2/FR-007: the email notice names sources but must not leak the raw
    exception string (which may carry a token/secret)."""
    text = _degradation_text(_FAILED_SOURCES, None)
    assert "secret-token-xyz" not in text
    html = _degradation_html(_FAILED_SOURCES, None)
    assert "secret-token-xyz" not in html


def test_degradation_text_scoring_backlog_notice() -> None:
    text = _degradation_text(None, _SCORING_BACKLOG)
    assert "15" in text  # unscored count surfaced


def test_degradation_html_scoring_backlog_notice() -> None:
    html = _degradation_html(None, _SCORING_BACKLOG)
    assert "15" in html


def test_no_degradation_notice_when_healthy() -> None:
    clean = {"scored": 3, "remaining": 0, "cap_reason": None}
    assert _degradation_text(None, clean) == ""
    assert _degradation_text([], clean) == ""
    assert _degradation_html(None, clean) == ""
    assert _degradation_html([], clean) == ""


def test_main_includes_notice_in_body_even_with_no_matches(tmp_path: Path, monkeypatch) -> None:
    """FR-005/FR-020: the degradation notice rides along in the delivered
    digest, including on an otherwise empty (no-matches) day."""
    db = tmp_path / "jobs.db"
    from job_agent import store

    store.init(str(db))
    monkeypatch.chdir(tmp_path)
    _set_smtp_env(monkeypatch)

    sent = {}

    def fake_send(subject: str, text: str, html: str) -> None:
        sent["text"] = text
        sent["html"] = html

    monkeypatch.setattr(digest, "_send", fake_send)

    result = digest.main(failed_sources=_FAILED_SOURCES, scoring=_SCORING_BACKLOG)

    assert result is True
    assert "acme" in sent["text"] and "foo" in sent["text"]
    assert "15" in sent["text"]
    assert "acme" in sent["html"] and "foo" in sent["html"]
    # FR-007: raw error text never reaches the wire.
    assert "secret-token-xyz" not in sent["text"]
    assert "secret-token-xyz" not in sent["html"]


def test_main_default_args_send_no_notice(tmp_path: Path, monkeypatch) -> None:
    """Backward compat: digest.main() with no degradation args (the
    jobagent-digest entry point) behaves exactly as before."""
    db = tmp_path / "jobs.db"
    from job_agent import store

    store.init(str(db))
    monkeypatch.chdir(tmp_path)
    _set_smtp_env(monkeypatch)

    sent = {}
    monkeypatch.setattr(
        digest, "_send", lambda subject, text, html: sent.update(text=text, html=html)
    )

    result = digest.main()

    assert result is True
    # an empty, healthy day carries no degraded-source name
    assert "greenhouse" not in sent["text"]


def test_render_html_unaffected_when_no_degradation() -> None:
    """The bucket renderers stay degradation-agnostic; notices are composed
    separately by _degradation_* and prepended by main()."""
    groups = {
        "engineering": [
            {
                "title": "Engineer",
                "company": "Acme",
                "location": "Remote",
                "skills_fit": 8,
                "category_risk": 2,
                "url": "https://example.com",
                "comp_flag": "ok",
            }
        ]
    }
    html = _render_html(groups)
    assert "Engineer" in html


def test_cli_discards_return_value(monkeypatch) -> None:
    monkeypatch.setattr(digest, "main", lambda: True)
    assert digest._cli() is None
