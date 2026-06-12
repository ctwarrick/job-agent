#!/usr/bin/env python3
"""Context-budget monitor hook for Claude Code.

Autocompact fires at ~95% of the context window, well past the point where
model performance degrades, and compaction summarizes (lossy). This hook gets
ahead of that curve: it reads the session transcript's real token usage after
each tool call / user prompt and, when usage crosses a band boundary, injects
a reminder telling the orchestrator to run the checkpoint protocol in
AGENTS.md (durable artifacts + handoff file), so a fresh session can resume
from disk with nothing dropped.

The budget is MODEL-AWARE. The roles in this repo run on different models
(scout on Haiku with a 200K window; orchestrator/planner/reviewer on
1M-window models), so a flat budget could blow a smaller model's window.
For each measurement we read the model id off the same transcript entry the
usage came from and take:

    effective_budget = min(PERFORMANCE_BUDGET, 80% of that model's window)

PERFORMANCE_BUDGET defaults to 200K because the quality sweet spot is an
absolute-token phenomenon (~<=160K) even on 1M-window models. Override with
env CONTEXT_BUDGET (tokens). Bands, as fractions of the effective budget:

  60%  ELEVATED  finish current phase, checkpoint artifacts to disk
  75%  HIGH      write handoff.md now; recommend fresh session
  85%  CRITICAL  stop dispatching; handoff immediately

Each band fires at most once per session (state kept in /tmp), so the
injected reminders don't spam every tool call.

Registered in .claude/settings.json under PostToolUse and UserPromptSubmit.
Reads the hook payload on stdin; emits hookSpecificOutput.additionalContext.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

PERFORMANCE_BUDGET = int(os.environ.get("CONTEXT_BUDGET", "200000"))
WINDOW_FRACTION = 0.80  # never let the budget exceed 80% of the model's window

# Known context windows. Unknown models fall back to the smallest (200K) so a
# new/unrecognized model is treated conservatively rather than optimistically.
DEFAULT_WINDOW = 200_000
ONE_MILLION = 1_000_000


def window_for(model: str) -> int:
    m = (model or "").lower()
    if "haiku" in m:
        return 200_000
    if "[1m]" in m:
        return ONE_MILLION
    if "fable" in m:
        return ONE_MILLION
    if "opus-4" in m and not any(v in m for v in ("opus-4-0", "opus-4-1", "opus-4-2")):
        return ONE_MILLION  # opus 4.5+ are 1M-window
    if "sonnet-4-6" in m:
        return ONE_MILLION
    return DEFAULT_WINDOW


BANDS = [
    (0.85, "CRITICAL"),
    (0.75, "HIGH"),
    (0.60, "ELEVATED"),
]

MESSAGES = {
    "ELEVATED": (
        "Context usage is at {pct:.0%} of the effective budget "
        "({used:,}/{budget:,} tokens, model {model}) - entering the managed "
        "band. Finish the current phase, then checkpoint per AGENTS.md: make "
        "sure every phase artifact so far (spec/plan/test/build/review) is "
        "written under docs/work/<task>/. Push remaining detail work to "
        "subagents; keep only artifacts and decisions in this conversation."
    ),
    "HIGH": (
        "Context usage is at {pct:.0%} of the effective budget "
        "({used:,}/{budget:,} tokens, model {model}). Write "
        "docs/work/<task>/handoff.md NOW (current state, decisions made, next "
        "steps, open questions), confirm all phase artifacts are on disk, "
        "then tell the human a fresh session resuming from handoff.md is "
        "recommended. Do not start new phases in this session."
    ),
    "CRITICAL": (
        "Context usage is at {pct:.0%} of the effective budget "
        "({used:,}/{budget:,} tokens, model {model}) - quality degradation "
        "likely and lossy autocompact is approaching. Stop dispatching work. "
        "Write or update docs/work/<task>/handoff.md immediately and ask the "
        "human to start a fresh session from it."
    ),
}


def latest_usage(transcript_path: str) -> tuple[int, str] | None:
    """(context tokens, model) from the most recent assistant turn.

    The entry's own model is what its usage is measured against, so subagent
    (sidechain) turns are checked against the subagent's model window, not
    the main session's.
    """
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        if usage and usage.get("input_tokens") is not None:
            used = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("output_tokens", 0)
            )
            return used, message.get("model", "")
    return None


def band_for(pct: float) -> str | None:
    for threshold, name in BANDS:
        if pct >= threshold:
            return name
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    transcript = payload.get("transcript_path")
    session = payload.get("session_id", "unknown")
    if not transcript:
        return

    found = latest_usage(transcript)
    if found is None:
        return
    used, model = found
    budget = min(PERFORMANCE_BUDGET, int(window_for(model) * WINDOW_FRACTION))
    pct = used / budget
    band = band_for(pct)
    if band is None:
        return

    # fire each band at most once per session
    state_path = os.path.join(tempfile.gettempdir(), f"claude-context-band-{session}")
    try:
        last = open(state_path, encoding="utf-8").read().strip()
    except OSError:
        last = ""
    order = ["", "ELEVATED", "HIGH", "CRITICAL"]
    if order.index(band) <= order.index(last if last in order else ""):
        return
    with open(state_path, "w", encoding="utf-8") as f:
        f.write(band)

    msg = MESSAGES[band].format(pct=pct, used=used, budget=budget, model=model or "unknown")
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": payload.get("hook_event_name", "PostToolUse"),
                    "additionalContext": f"[context-monitor] {msg}",
                }
            }
        )
    )


if __name__ == "__main__":
    main()
