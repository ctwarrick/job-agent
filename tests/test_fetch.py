from job_agent.fetch import load_registry


def test_load_registry_skips_comments_and_blank_lines(tmp_path):
    registry = tmp_path / "registry.txt"
    registry.write_text(
        "# header comment\n"
        "\n"
        "greenhouse stripe\n"
        "lever plaid # trailing comment\n"
    )
    assert load_registry(str(registry)) == [
        ("greenhouse", "stripe"),
        ("lever", "plaid"),
    ]


def test_load_registry_lowercases_vendor(tmp_path):
    registry = tmp_path / "registry.txt"
    registry.write_text("Greenhouse stripe\n")
    assert load_registry(str(registry)) == [("greenhouse", "stripe")]


def test_load_registry_skips_malformed_lines(tmp_path, capsys):
    registry = tmp_path / "registry.txt"
    registry.write_text("greenhouse\nlever plaid\n")
    assert load_registry(str(registry)) == [("lever", "plaid")]
    assert "malformed" in capsys.readouterr().err
