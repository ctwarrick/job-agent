"""LLM scoring stage.

Reads scorable postings from SQLite (unscored and not yet filter-rejected),
runs each through the deterministic relevance gate in filter.py, persists
rejections, and scores the remaining plausible postings against profile.md
using the Anthropic API in small batches, writing the scores + rationale
back.

Each posting gets:
  skills_fit      0-10  how well your technical/leadership substance is used
  seniority_fit   0-10  right level for 8.5 yrs + command experience
  category_risk   0-10  0 = pulls toward technical/product ownership,
                        10 = pulls toward pure process facilitation
  bucket          which target bucket: "engineering" | "tpm" | "eng_mgmt" | "other"
  comp_flag       "lowball" | "ok" | "unknown"
  trajectory_note one line: which way this role pulls your career, and why

The screening instructions (the system prompt) live in screening_prompt.md so
they can be tuned without code changes, alongside profile.md.

Env:
  ANTHROPIC_API_KEY            required
  JOBAGENT_SALARY_FLOOR        required, base salary floor in dollars (e.g. 120000)
  JOBAGENT_MODEL                optional, defaults below
  JOBAGENT_MAX_POSTINGS_PER_RUN optional, hard cap on postings scored per run
                                 (default 200)
  JOBAGENT_MAX_COST_PER_RUN      optional, hard cap on estimated dollars per
                                 run (default 5.00)
  JOBAGENT_PRICE_INPUT           optional, $/MTok uncached input (default 3.00)
  JOBAGENT_PRICE_OUTPUT          optional, $/MTok output (default 15.00)
  JOBAGENT_PRICE_CACHE_WRITE     optional, $/MTok cache-creation (default 3.75)
  JOBAGENT_PRICE_CACHE_READ      optional, $/MTok cache-read (default 0.30)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

from . import filter as jobfilter
from . import store

MODEL = os.environ.get("JOBAGENT_MODEL", "claude-sonnet-4-6")
BATCH = 6  # postings per API call
DESC_CHARS = 3500  # truncate each JD to control token cost
_floor = os.environ.get("JOBAGENT_SALARY_FLOOR")
SALARY_FLOOR = int(_floor) if _floor else None  # keep in sync with profile.md

# Rough per-posting token estimates used only for the pre-call cost
# projection (cap check). These are not billing truth -- they are a
# conservative ballpark for one posting's share of a batch prompt plus
# its share of the response.
EST_INPUT_TOKENS_PER_POSTING = 1200
EST_OUTPUT_TOKENS_PER_POSTING = 200

# Default per-MTok prices (USD), matching documented claude-sonnet-4-6
# list pricing. Overridable via env; see module docstring.
DEFAULT_PRICE_INPUT = 3.00
DEFAULT_PRICE_OUTPUT = 15.00
DEFAULT_PRICE_CACHE_WRITE = 3.75
DEFAULT_PRICE_CACHE_READ = 0.30

# Default per-run guardrails (FR-005/FR-008): the run is never unbounded.
DEFAULT_MAX_POSTINGS_PER_RUN = 200
DEFAULT_MAX_COST_PER_RUN = 5.00

PROMPT_TEMPLATE = """## CANDIDATE PROFILE
{profile}

## SALARY FLOOR
${floor:,} base. If the posting lists comp clearly below this, comp_flag="lowball". \
If it lists comp at/above, "ok". If no comp is stated, "unknown".

## POSTINGS TO SCORE ({n})
{postings}

## OUTPUT
Return a JSON array of exactly {n} objects, same order as the postings. Each:
{{
  "fingerprint": "<echo the posting's fingerprint>",
  "skills_fit": <int 0-10>,
  "seniority_fit": <int 0-10>,
  "category_risk": <int 0-10>,
  "bucket": "engineering" | "tpm" | "eng_mgmt" | "scrum_master" | "other",
  "comp_flag": "lowball" | "ok" | "unknown",
  "trajectory_note": "<one sentence: which way this role pulls the career and why>"
}}
Output ONLY the JSON array."""


def _format_postings(rows: list[dict]) -> str:
    """Format a list of posting dicts into a text block for the prompt.

    Truncates each description to DESC_CHARS and includes fingerprint,
    title, company, and location.

    Args:
        rows: List of posting dicts with at least fingerprint, title,
            company, location, description fields.

    Returns:
        Formatted text block for inclusion in PROMPT_TEMPLATE.
    """
    blocks = []
    for r in rows:
        desc = (r["description"] or "")[:DESC_CHARS]
        blocks.append(
            f"--- fingerprint: {r['fingerprint']}\n"
            f"Title: {r['title']}\n"
            f"Company: {r['company']}\n"
            f"Location: {r['location']}\n"
            f"Description: {desc}"
        )
    return "\n\n".join(blocks)


def _score_batch(client: Anthropic, system: str, profile: str, rows: list[dict]) -> list[dict]:
    """Score a batch of postings using the Anthropic API.

    Calls claude with the system prompt, profile, and formatted postings.
    Expects a JSON array response with scoring fields (skills_fit, etc.).

    Args:
        client: Anthropic client instance.
        system: System prompt text (from screening_prompt.md).
        profile: Profile text (from profile.md).
        rows: List of posting dicts with at least fingerprint, title,
            company, location, description.

    Returns:
        List of dicts, each with at least fingerprint and score fields
        (skills_fit, seniority_fit, category_risk, bucket, comp_flag,
        trajectory_note).
    """
    prompt = PROMPT_TEMPLATE.format(
        profile=profile,
        floor=SALARY_FLOOR,
        n=len(rows),
        postings=_format_postings(rows),
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # be tolerant of accidental fences
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    return json.loads(text)


def _write_scores(scores: list[dict], path: str | None = None) -> None:
    """Write LLM-generated scores back to the postings table.

    For each score dict, updates the corresponding posting row with
    skills_fit, seniority_fit, category_risk, and a JSON rationale
    (bucket, comp_flag, trajectory_note).

    Args:
        scores: List of score dicts, each with fingerprint, skills_fit,
            seniority_fit, category_risk, bucket, comp_flag,
            trajectory_note.
        path: Optional path to jobs.db; defaults to data_path("jobs.db").
    """
    with store.connect(path) as conn:
        for s in scores:
            conn.execute(
                "UPDATE postings SET skills_fit=?, seniority_fit=?, "
                "category_risk=?, rationale=? WHERE fingerprint=?",
                (
                    s.get("skills_fit"),
                    s.get("seniority_fit"),
                    s.get("category_risk"),
                    json.dumps(
                        {
                            "bucket": s.get("bucket"),
                            "comp_flag": s.get("comp_flag"),
                            "trajectory_note": s.get("trajectory_note"),
                        }
                    ),
                    s.get("fingerprint"),
                ),
            )


def _projected_batch_cost(n_postings: int) -> float:
    """Project the dollar cost of scoring a batch before issuing it.

    Uses rough per-posting token estimates (EST_INPUT_TOKENS_PER_POSTING,
    EST_OUTPUT_TOKENS_PER_POSTING) and the configured per-MTok prices read
    from JOBAGENT_PRICE_INPUT / JOBAGENT_PRICE_OUTPUT. This is a rough
    projection, not billing truth, and conservatively ignores prompt-cache
    savings (US3 not yet wired up), so it tends to over-estimate rather
    than under-estimate.

    Args:
        n_postings: Number of postings in the prospective batch.

    Returns:
        Estimated dollar cost of scoring n_postings postings.
    """
    price_input = float(os.environ.get("JOBAGENT_PRICE_INPUT", DEFAULT_PRICE_INPUT))
    price_output = float(os.environ.get("JOBAGENT_PRICE_OUTPUT", DEFAULT_PRICE_OUTPUT))
    input_tokens = n_postings * EST_INPUT_TOKENS_PER_POSTING
    output_tokens = n_postings * EST_OUTPUT_TOKENS_PER_POSTING
    return (input_tokens * price_input + output_tokens * price_output) / 1e6


def _cost_usd(
    input_tokens: int, output_tokens: int, cache_write_tokens: int, cache_read_tokens: int
) -> float:
    """Compute the estimated dollar cost of an API call's token usage.

    Args:
        input_tokens: Uncached input tokens.
        output_tokens: Output tokens.
        cache_write_tokens: Cache-creation input tokens.
        cache_read_tokens: Cache-read input tokens.

    Returns:
        Estimated dollar cost from the four usage components and the
        configured per-MTok prices (JOBAGENT_PRICE_INPUT,
        JOBAGENT_PRICE_OUTPUT, JOBAGENT_PRICE_CACHE_WRITE,
        JOBAGENT_PRICE_CACHE_READ).
    """
    price_input = float(os.environ.get("JOBAGENT_PRICE_INPUT", DEFAULT_PRICE_INPUT))
    price_output = float(os.environ.get("JOBAGENT_PRICE_OUTPUT", DEFAULT_PRICE_OUTPUT))
    price_cache_write = float(
        os.environ.get("JOBAGENT_PRICE_CACHE_WRITE", DEFAULT_PRICE_CACHE_WRITE)
    )
    price_cache_read = float(
        os.environ.get("JOBAGENT_PRICE_CACHE_READ", DEFAULT_PRICE_CACHE_READ)
    )
    return (
        input_tokens * price_input
        + output_tokens * price_output
        + cache_write_tokens * price_cache_write
        + cache_read_tokens * price_cache_read
    ) / 1e6


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set")
    if not SALARY_FLOOR:
        sys.exit("JOBAGENT_SALARY_FLOOR not set (base salary floor in dollars, e.g. 120000)")
    try:
        criteria = jobfilter.load_criteria()
    except Exception as e:
        sys.exit(f"filter.toml invalid or missing: {e}")

    max_postings_raw = os.environ.get("JOBAGENT_MAX_POSTINGS_PER_RUN")
    try:
        max_postings = (
            int(max_postings_raw) if max_postings_raw is not None else DEFAULT_MAX_POSTINGS_PER_RUN
        )
    except ValueError:
        max_postings = -1
    if max_postings <= 0:
        sys.exit(f"JOBAGENT_MAX_POSTINGS_PER_RUN invalid: {max_postings_raw!r} (must be > 0)")

    max_cost_raw = os.environ.get("JOBAGENT_MAX_COST_PER_RUN")
    try:
        max_cost = float(max_cost_raw) if max_cost_raw is not None else DEFAULT_MAX_COST_PER_RUN
    except ValueError:
        max_cost = -1.0
    if max_cost <= 0:
        sys.exit(f"JOBAGENT_MAX_COST_PER_RUN invalid: {max_cost_raw!r} (must be > 0)")
    # Display the configured value verbatim in SCORE_CAP_STOP when set.
    cost_limit = max_cost_raw if max_cost_raw is not None else max_cost

    for env_var in (
        "JOBAGENT_PRICE_INPUT",
        "JOBAGENT_PRICE_OUTPUT",
        "JOBAGENT_PRICE_CACHE_WRITE",
        "JOBAGENT_PRICE_CACHE_READ",
    ):
        raw = os.environ.get(env_var)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if value < 0:
            sys.exit(f"{env_var} invalid: {raw!r} (must be >= 0)")

    rows = store.scorable()

    plausible = []
    rejects = []
    for row in rows:
        reason = jobfilter.classify(row, criteria)
        if reason is None:
            plausible.append(row)
        else:
            rejects.append((row["fingerprint"], reason))
    store.record_filter_rejections(rejects)

    if not plausible:
        print("Nothing to score.")
        return

    system = Path(store.data_path("screening_prompt.md")).read_text()
    profile = Path(store.data_path("profile.md")).read_text()
    client = Anthropic()

    print(f"Scoring {len(plausible)} postings in batches of {BATCH}...")

    scored = 0
    projected_spend = 0.0
    for i in range(0, len(plausible), BATCH):
        batch = plausible[i : i + BATCH]

        room = max_postings - scored
        if len(batch) > room:
            batch = batch[:room]

        batch_cost = _projected_batch_cost(len(batch))
        if projected_spend + batch_cost > max_cost:
            remaining = len(plausible) - scored
            print(
                f"SCORE_CAP_STOP reason=cost scored={scored} "
                f"remaining={remaining} limit={cost_limit}"
            )
            break

        try:
            scores = _score_batch(client, system, profile, batch)
            _write_scores(scores)
            scored += len(batch)
            projected_spend += batch_cost
            print(f"  scored {scored}/{len(plausible)}")
        except Exception as e:
            print(f"  ! batch {i}-{i+len(batch)} failed: {e}", file=sys.stderr)

        if scored >= max_postings:
            remaining = len(plausible) - scored
            print(
                f"SCORE_CAP_STOP reason=postings scored={scored} "
                f"remaining={remaining} limit={max_postings}"
            )
            break

    print("Done.")


if __name__ == "__main__":
    main()
