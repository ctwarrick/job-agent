"""Root conftest.

Presence of this file at the repo root makes pytest add the repo root to
sys.path (rootdir, default "prepend" import mode), so `tests/test_main.py`
can `import main` (the entry point at the repo root, alongside pyproject.toml)
the same way a deployed container would run `python main.py`.
"""
