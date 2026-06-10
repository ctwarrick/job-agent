from job_agent.schema import _clean, normalize


def test_clean_strips_html_and_collapses_whitespace():
    assert _clean("<p>Hello   <b>world</b></p>&nbsp;") == "Hello world"


def test_clean_handles_none():
    assert _clean(None) == ""


def test_normalize_strips_fields_and_defaults_location():
    posting = normalize(
        source="greenhouse",
        company="acme",
        external_id=42,
        title="  Engineer  ",
        location="   ",
        description="<p>Build things</p>",
        url=" https://example.com/job ",
    )
    assert posting.title == "Engineer"
    assert posting.location == "Unspecified"
    assert posting.description == "Build things"
    assert posting.url == "https://example.com/job"
    assert posting.external_id == "42"


def test_fingerprint_ignores_source_id_and_case():
    a = normalize(
        source="greenhouse", company="Acme", external_id="1",
        title="Engineer", location="Remote", description="", url="",
    )
    b = normalize(
        source="lever", company="acme", external_id="2",
        title="ENGINEER", location="remote", description="", url="",
    )
    assert a.fingerprint == b.fingerprint


def test_fingerprint_differs_for_different_roles():
    a = normalize(
        source="greenhouse", company="Acme", external_id="1",
        title="Engineer", location="Remote", description="", url="",
    )
    b = normalize(
        source="greenhouse", company="Acme", external_id="1",
        title="Manager", location="Remote", description="", url="",
    )
    assert a.fingerprint != b.fingerprint
