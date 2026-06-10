from job_agent.digest import _group, _render_text


def test_group_buckets_by_field_and_defaults_to_other():
    rows = [
        {"bucket": "engineering", "title": "A"},
        {"bucket": "tpm", "title": "B"},
        {"title": "C"},
    ]
    groups = _group(rows)
    assert set(groups) == {"engineering", "tpm", "other"}
    assert groups["other"][0]["title"] == "C"


def test_render_text_includes_posting_details():
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


def test_render_text_flags_lowball_comp():
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
