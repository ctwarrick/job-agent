# Quickstart / validation

Prereqs: `uv sync`; a populated `registry.toml` at the repo root.

## 1. Tests (red → green)

```bash
uv run pytest tests/test_registry.py -v   # new loader tests
uv run pytest                             # full suite green
```

## 2. Loader sanity check

```bash
uv run python -c "from job_agent.registry import load_registry; \
v=load_registry('registry.toml'); print(len(v), v[0])"
# expect: 14  Source(vendor='greenhouse', slug='stripe', company='stripe')
# fingerprint check: workday company == old companies.toml value
#   (e.g. chrobinson -> 'C.H. Robinson'); gh/lever company == slug
```

## 3. Fail-loud check (config error surfaces, nothing fetched)

```bash
printf '[[source]]\nvendor="nope"\nslug="x"\n' > /tmp/bad.toml
uv run python -c "from job_agent.registry import load_registry; load_registry('/tmp/bad.toml')"
# expect: ValueError naming the unknown vendor; non-zero exit
```

## 4. End-to-end dry run (no email)

```bash
DIGEST_DRY_RUN=1 uv run jobagent-fetch   # one line per source, all 4 vendors
```

## 5. Production cutover (sanctioned manual op, Principle II)

1. Upload `registry.toml` to the `jobagent-data` Files share **first**.
2. Then deploy the TOML-reading code (it fails loud if the file is absent).
3. Delete the leftover `registry.txt` **and `companies.toml`** from the share in
   the same session (company names now live in `registry.toml`).

After cutover, registry edits and code releases are independent.
