"""Pipeline entry point.

Runs the full daily pipeline in a single process — fetch new postings, score
the unscored ones with the LLM, then email the digest. This is the entry point
for a scheduled container job:

    python main.py

Run lifecycle (specs/001-azure-deployment/data-model.md "runs",
contracts/runtime-config.md):

  1. Compute `digest_date` (JOBAGENT_TZ, default America/Los_Angeles).
  2. Startup check (`store.startup_decision`):
       - "skip_inflight":  another execution for today is running -> exit 0,
         no-op (not bypassed by JOBAGENT_FORCE).
       - "skip_succeeded": today already has a success/degraded run -> exit 0,
         no-op, but still print RUN_SUCCESS (the alert query keys on this
         marker, and a no-op skip of an already-successful date is success).
         Bypassed by JOBAGENT_FORCE=1.
       - "proceed": record a new run row and continue.
  3. fetch -> score -> digest, in order, fail loud (sys.exit) on a fatal
     stage error.
  4. On a confirmed digest send, print RUN_SUCCESS. The run's success outcome
     is committed via `store.finish_run` immediately after that confirmation
     (digest.main() has already committed digest_sent_at inside its own
     transaction by the time it returns -- see "Deviation" below).
  5. On fatal failure, record outcome "failed" and exit non-zero; if this was
     the day's 3rd-or-later attempt, also print RUN_FAILED_FINAL.

Deviation from "one transaction" (data-model.md "Validation rules"):
digest.main() marks digest_sent_at and commits via its own `store.connect()`
context manager before returning True; `finish_run` for the run row is a
separate connection/transaction immediately afterward. A crash in the gap
between these two commits leaves digest_sent_at set but the run row without
a success outcome -- the next tick's startup check sees no success/degraded
row for today and retries, re-running fetch/score/digest. Because postings
already marked digest_sent_at are excluded from the digest query, the retry
sends only the no-new-matches notice (or any newly-qualifying postings) --
never a duplicate of the already-sent digest. This is an intentional,
narrower instance of the spec's accepted at-least-once behavior.

Each stage is also exposed as its own console script (jobagent-fetch /
jobagent-score / jobagent-digest) for debugging, or for a split serverless
deployment where each stage is a separate function.
"""

from __future__ import annotations

import os
import sys

from job_agent import digest, fetch, score, store

# Attempts at/after this count are the day's last scheduled tick
# (data-model.md: "one run + up to 2 retries" -> attempts 1, 2, 3).
FINAL_ATTEMPT = 3


def main() -> None:
    store.init()  # ensure schema exists before any run-tracking query
    digest_date = store.digest_date()
    force = os.environ.get("JOBAGENT_FORCE") == "1"

    decision = store.startup_decision(digest_date, force=force)
    if decision == "skip_inflight":
        print(f"Skipping run: another execution for digest_date={digest_date} is in flight.")
        return
    if decision == "skip_succeeded":
        print(f"Skipping run: digest_date={digest_date} already succeeded.")
        print(f"RUN_SUCCESS digest_date={digest_date}")
        return

    run_id = store.start_run(digest_date)
    with store.connect() as conn:
        attempt = conn.execute("SELECT attempt FROM runs WHERE id=?", (run_id,)).fetchone()[
            "attempt"
        ]

    try:
        fetch.main()  # pull + store new postings
        score.main()  # LLM-score the unscored ones
        sent = digest.main()  # email high-fit, low-risk, not-yet-sent postings (or notice)
    except SystemExit as e:
        store.finish_run(
            run_id,
            outcome="failed",
            failed_sources=None,
            detail=str(e),
        )
        if attempt >= FINAL_ATTEMPT:
            print(f"RUN_FAILED_FINAL digest_date={digest_date}")
        sys.exit(e.code if e.code else 1)
    except Exception as e:
        store.finish_run(
            run_id,
            outcome="failed",
            failed_sources=None,
            detail=str(e),
        )
        if attempt >= FINAL_ATTEMPT:
            print(f"RUN_FAILED_FINAL digest_date={digest_date}")
        raise

    if not sent:
        store.finish_run(
            run_id,
            outcome="failed",
            failed_sources=None,
            detail="digest send failed",
        )
        if attempt >= FINAL_ATTEMPT:
            print(f"RUN_FAILED_FINAL digest_date={digest_date}")
        sys.exit("digest send failed")

    # digest.main() has already committed digest_sent_at by this point
    # (see module docstring "Deviation"); record the run's success outcome.
    store.finish_run(run_id, outcome="success", failed_sources=None, detail=None)
    print(f"RUN_SUCCESS digest_date={digest_date}")


if __name__ == "__main__":
    main()
