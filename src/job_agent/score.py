"""LLM scoring stage.

Reads unscored postings from SQLite, scores each against profile.md using the
Anthropic API in small batches, and writes the scores + rationale back.

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
  ANTHROPIC_API_KEY      required
  JOBAGENT_SALARY_FLOOR  required, base salary floor in dollars (e.g. 120000)
  JOBAGENT_MODEL         optional, defaults below
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

from . import store

MODEL = os.environ.get("JOBAGENT_MODEL", "claude-opus-4-20250514")
BATCH = 6  # postings per API call
DESC_CHARS = 3500  # truncate each JD to control token cost
_floor = os.environ.get("JOBAGENT_SALARY_FLOOR")
SALARY_FLOOR = int(_floor) if _floor else None  # keep in sync with profile.md

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


def _format_postings(rows) -> str:
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


def _score_batch(client, system, profile, rows) -> list[dict]:
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


def _write_scores(scores: list[dict], path: str = "jobs.db") -> None:
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


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set")
    if not SALARY_FLOOR:
        sys.exit("JOBAGENT_SALARY_FLOOR not set (base salary floor in dollars, e.g. 120000)")
    system = Path("screening_prompt.md").read_text()
    profile = Path("profile.md").read_text()
    client = Anthropic()

    rows = store.unscored()
    if not rows:
        print("Nothing to score.")
        return
    print(f"Scoring {len(rows)} postings in batches of {BATCH}...")

    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        try:
            scores = _score_batch(client, system, profile, batch)
            _write_scores(scores)
            print(f"  scored {i + len(batch)}/{len(rows)}")
        except Exception as e:
            print(f"  ! batch {i}-{i+len(batch)} failed: {e}", file=sys.stderr)
    print("Done.")


if __name__ == "__main__":
    main()
