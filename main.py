"""Pipeline entry point.

Runs the full daily pipeline in a single process — fetch new postings, score
the unscored ones with the LLM, then email the digest. This is the entry point
to target from a container CMD / scheduled job:

    python main.py

Stages run in order and fail loud: if a stage calls sys.exit (e.g. score with
no ANTHROPIC_API_KEY, or digest on SMTP failure), the run stops there rather
than emailing a stale or empty digest — the same fail-fast behaviour the old
run.sh got from `set -e`.

Each stage is also exposed as its own console script (jobagent-fetch /
jobagent-score / jobagent-digest) for debugging, or for a split serverless
deployment where each stage is a separate function.
"""
from __future__ import annotations

from job_agent import digest, fetch, score


def main() -> None:
    fetch.main()    # pull + store new postings
    score.main()    # LLM-score the unscored ones
    digest.main()   # email high-fit, low-risk, not-yet-sent postings


if __name__ == "__main__":
    main()
