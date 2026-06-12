from job_agent import fetch
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


def test_load_registry_default_resolves_under_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cwd_dir = tmp_path / "cwd"
    data_dir.mkdir()
    cwd_dir.mkdir()
    (data_dir / "registry.txt").write_text("greenhouse acme\n")

    monkeypatch.setenv("JOBAGENT_DATA_DIR", str(data_dir))
    monkeypatch.chdir(cwd_dir)

    assert fetch.load_registry() == [("greenhouse", "acme")]
