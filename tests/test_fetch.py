from job_agent import fetch, registry

# --- US3a: per-source failure records returned (FR-005) ---------------------


def test_main_returns_failure_record_when_adapter_raises(monkeypatch) -> None:
    """A failing adapter does not kill the run: its failure is captured as a
    {source, company_slug, error} record and returned, while a healthy source
    in the same run still upserts."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="good", company="good"),
        ],
    )

    def bad(slug: str, *, company=None):
        raise RuntimeError("boom timeout")

    def good(slug: str, *, company=None):
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
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [registry.Source(vendor="workday", slug="bigco", company="bigco")],
    )
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
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="plaid", company="plaid"),
        ],
    )
    monkeypatch.setattr(
        fetch,
        "ADAPTERS",
        {"greenhouse": lambda s, *, company=None: [], "lever": lambda s, *, company=None: []},
    )
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    assert fetch.main() == []


def test_main_passes_resolved_company_to_adapter(monkeypatch) -> None:
    """main() must dispatch ADAPTERS[source.vendor](source.slug,
    company=source.company): the resolved Source.company (whether it equals
    the slug or an explicit display name) must reach the adapter call."""
    monkeypatch.setattr(fetch.store, "init", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch,
        "load_registry",
        lambda *a, **k: [
            registry.Source(vendor="greenhouse", slug="acme", company="acme"),
            registry.Source(vendor="lever", slug="plaid", company="Acme Corp"),
        ],
    )

    received: list[tuple] = []

    def recording_adapter(slug, *, company=None):
        received.append((slug, company))
        return []

    monkeypatch.setattr(
        fetch, "ADAPTERS", {"greenhouse": recording_adapter, "lever": recording_adapter}
    )
    monkeypatch.setattr(fetch.store, "upsert_postings", lambda *a, **k: 0)

    fetch.main()

    assert ("acme", "acme") in received
    assert ("plaid", "Acme Corp") in received


def test_cli_discards_return_value(monkeypatch) -> None:
    """jobagent-fetch (fetch:_cli) must return None so sys.exit(_cli()) exits 0
    on success, even though main() returns a (possibly non-empty) failure list."""
    monkeypatch.setattr(fetch, "main", lambda: [{"source": "x", "company_slug": "y", "error": "z"}])
    assert fetch._cli() is None
