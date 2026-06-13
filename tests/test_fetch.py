from pathlib import Path

from job_agent import fetch
from job_agent.fetch import load_registry


def test_load_registry_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    registry = tmp_path / "registry.txt"
    registry.write_text(
        "# header comment\n" "\n" "greenhouse stripe\n" "lever plaid # trailing comment\n"
    )
    assert load_registry(str(registry)) == [
        ("greenhouse", "stripe"),
        ("lever", "plaid"),
    ]


def test_load_registry_lowercases_vendor(tmp_path: Path) -> None:
    registry = tmp_path / "registry.txt"
    registry.write_text("Greenhouse stripe\n")
    assert load_registry(str(registry)) == [("greenhouse", "stripe")]


def test_load_registry_skips_malformed_lines(tmp_path: Path, capsys) -> None:
    registry = tmp_path / "registry.txt"
    registry.write_text("greenhouse\nlever plaid\n")
    assert load_registry(str(registry)) == [("lever", "plaid")]
    assert "malformed" in capsys.readouterr().err


def test_load_registry_default_resolves_under_data_dir(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "registry.txt").write_text("greenhouse acme\n")

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.chdir(cwd_dir)

    assert fetch.load_registry() == [("greenhouse", "acme")]


# --- US3a: per-source failure records returned (FR-005) ---------------------


def test_main_returns_failure_record_when_adapter_raises(monkeypatch) -> None:
    """A failing adapter does not kill the run: its failure is captured as a
    {source, company_slug, error} record and returned, while a healthy source
    in the same run still upserts."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch, "load_registry", lambda *a, **k: [("greenhouse", "acme"), ("lever", "good")]
    )

    def bad(slug: str):
        raise RuntimeError("boom timeout")

    def good(slug: str):
        return ["p1", "p2"]

    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": bad, "lever": good})

    upserted: list = []
    monkeypatch.setattr(
        fetch.store,
        "upsert_postings",
        lambda postings, *a, **k: (upserted.extend(postings) or len(postings)),
    )

    failures = fetch.main()

    # healthy source still ran
    assert upserted == ["p1", "p2"]
    # one structured failure record for the bad slug
    assert len(failures) == 1
    rec = failures[0]
    assert rec["source"] == "greenhouse"
    assert rec["company_slug"] == "acme"
    assert "boom timeout" in rec["error"]


def test_main_returns_failure_record_for_unknown_vendor(monkeypatch) -> None:
    """A registry vendor with no adapter is per-source degradation (A1), not a
    fatal config error: it produces a failure record and the run continues."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "load_registry", lambda *a, **k: [("workday", "bigco")])
    monkeypatch.setattr(fetch, "ADAPTERS", {})
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    failures = fetch.main()

    assert len(failures) == 1
    rec = failures[0]
    assert rec["source"] == "workday"
    assert rec["company_slug"] == "bigco"
    assert "adapter" in rec["error"].lower()


def test_main_returns_empty_list_when_all_sources_succeed(monkeypatch) -> None:
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch, "load_registry", lambda *a, **k: [("greenhouse", "acme"), ("lever", "plaid")]
    )
    monkeypatch.setattr(fetch, "ADAPTERS", {"greenhouse": lambda s: [], "lever": lambda s: []})
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    assert fetch.main() == []


def test_cli_discards_return_value(monkeypatch) -> None:
    """jobagent-fetch (fetch:_cli) must return None so sys.exit(_cli()) exits 0
    on success, even though main() returns a (possibly non-empty) failure list."""
    monkeypatch.setattr(fetch, "main", lambda: [{"source": "x", "company_slug": "y", "error": "z"}])
    assert fetch._cli() is None
