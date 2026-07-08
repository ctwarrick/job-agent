#!/usr/bin/env bash
#
# review-codex.sh — dispatch the Reviewer role to GPT-5.5 via the OpenAI
# Codex CLI (`codex exec`), the cross-vendor counterpart of the thin
# `.claude/agents/reviewer.md` adapter.
#
# Why: the Reviewer's value is distance — a fresh context that catches what
# the implementer rationalized. Running the review on a different model
# vendor than the Claude roles that planned and built the diff removes the
# shared-blind-spot risk of one model family reviewing itself (quality gate
# #3 in AGENTS.md). The role contract itself stays platform-neutral in
# agents/reviewer.md; this script only composes the dispatch.
#
# Prerequisites: Codex CLI installed and authenticated (`codex login`,
# ChatGPT plan — uses subscription quota, not API billing).
#
# Model: gpt-5.5 by default; override with JOBAGENT_REVIEW_MODEL (some
# ChatGPT tiers reject gpt-5.5 — that surfaces as a non-zero exit here, and
# the orchestrator falls back to the Claude reviewer per
# agents/orchestrator.md step 4).
#
# Sandbox: workspace-write is required so the reviewer can run
# `uv run pytest` (.pytest_cache/__pycache__ writes); the Codex sandbox
# keeps network off, which fits the stub-based no-network test suite. The
# reviewer contract forbids fixing code, so write access does not
# compromise review integrity.
#
# Usage: scripts/review-codex.sh <diff-range> <plan-path> [verdict-out]
#   e.g. scripts/review-codex.sh 'main...HEAD' specs/006-resilient-fetch/plan.md \
#          docs/work/006/review.md
#   Pass HEAD as the range to review uncommitted work. Exits 2 on preflight
#   failure (codex missing, bad args, empty diff); otherwise propagates the
#   codex exit code. The verdict prints to stdout and, if [verdict-out] is
#   given, is also written there.

set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  echo "usage: scripts/review-codex.sh <diff-range> <plan-path> [verdict-out]" >&2
  exit 2
}

[ $# -ge 2 ] && [ $# -le 3 ] || usage
range="$1"
plan_path="$2"
verdict_out="${3:-}"

command -v codex >/dev/null 2>&1 || {
  echo "review-codex: codex CLI not found — install it and run 'codex login'" >&2
  exit 2
}
[ -f "$plan_path" ] || {
  echo "review-codex: plan not found: $plan_path" >&2
  exit 2
}
[ -n "$(git diff --name-only "$range" 2>/dev/null)" ] || {
  echo "review-codex: 'git diff $range' is empty or invalid — nothing to review" >&2
  exit 2
}

out_args=()
if [ -n "$verdict_out" ]; then
  mkdir -p "$(dirname "$verdict_out")"
  out_args=(-o "$verdict_out")
fi

# The dispatch prompt per the orchestrator contract: only the diff and the
# plan, the role card as the single source of process truth, no transcripts.
prompt=$(
  cat <<EOF
You are the Reviewer for this repository, dispatched as an independent
cross-vendor context: you are not the model that planned or implemented
this change, which satisfies the fresh-context requirement of step 1 in
your role card.

Read agents/reviewer.md and follow it exactly — it defines your role,
process, output contract, and what is out of scope.

Inputs (nothing else exists; there are no transcripts):
- The approved plan: $plan_path
- The diff under review: run \`git diff $range\`

Run \`uv run pytest\` yourself — never trust a reported green.

Output contract (from agents/reviewer.md): first line APPROVE or REVISE,
then numbered findings (max 20), most severe first, each with file:line,
what is wrong, and the concrete fix. Your final message must be exactly
this verdict and nothing else.
EOF
)

# uv's default cache (~/.cache/uv) is read-only inside the Codex sandbox
# (writable roots: workdir, /tmp, $TMPDIR); point it somewhere writable so
# the reviewer can actually run the mandated `uv run pytest`.
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/jobagent-uv-cache}"

# Same for az: ~/.azure is read-only in the sandbox, and az keys its managed
# bicep binary to the config dir, so a bare AZURE_CONFIG_DIR override would
# try (and fail, offline) to re-download bicep. Seed a writable config dir
# with a symlink to the local binary so the reviewer can re-run
# `scripts/validate-infra.sh` when the diff touches infra.
export AZURE_CONFIG_DIR="${AZURE_CONFIG_DIR:-/tmp/jobagent-azure}"
if [ ! -x "$AZURE_CONFIG_DIR/bin/bicep" ] && [ -x "$HOME/.azure/bin/bicep" ]; then
  mkdir -p "$AZURE_CONFIG_DIR/bin"
  ln -sf "$HOME/.azure/bin/bicep" "$AZURE_CONFIG_DIR/bin/bicep"
fi

# bicep is a .NET single-file bundle that self-extracts under $HOME unless
# told otherwise; $HOME is read-only in the sandbox, so give it /tmp.
export DOTNET_BUNDLE_EXTRACT_BASE_DIR="${DOTNET_BUNDLE_EXTRACT_BASE_DIR:-/tmp/jobagent-dotnet}"

exec codex exec \
  --sandbox workspace-write \
  --ephemeral \
  -m "${JOBAGENT_REVIEW_MODEL:-gpt-5.5}" \
  "${out_args[@]}" \
  - <<<"$prompt"
