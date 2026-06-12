# Runtime image for the scheduled pipeline (Azure Container Apps Job).
#
# Built from the official uv image so `uv sync --frozen` reproduces exactly
# the locked dependency set with no separate pip/venv bootstrapping. The
# image contains only public repo code + dependencies — profile.md,
# screening_prompt.md, registry.txt, and jobs.db are excluded via
# .dockerignore and supplied at runtime via the Azure Files mount
# (Constitution VI / FR-021).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Copy lock + project metadata first so dependency installation is cached
# independently of source changes. README/LICENSE are project metadata too:
# hatchling refuses to build without the files pyproject.toml declares.
COPY pyproject.toml uv.lock README.md LICENSE ./

# --frozen: fail if uv.lock is out of date rather than silently re-resolving.
# --no-dev: pytest etc. have no place in the runtime image.
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the application code and install the project itself into the venv.
COPY src/ src/
COPY main.py ./
RUN uv sync --frozen --no-dev

# Run the full pipeline (fetch -> score -> digest) with the already-synced
# venv's interpreter directly — `uv run` would re-sync on every container
# start, pointless work for an immutable image.
CMD ["/app/.venv/bin/python", "main.py"]
