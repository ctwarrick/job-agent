#!/usr/bin/env bash
#
# validate-infra.sh — local compile-check of the Bicep infra so a param-file
# or deploy-command-shape mismatch is caught here, not at deploy time.
#
# Why: a `.bicepparam` file is compiled before any `az deployment` inline
# `--parameters` are merged, so a required no-default/@secure() param that is
# only ever supplied inline can never satisfy the compile — that is Bicep
# BCP258, the failure that broke the 0.2.0 deploy. `uv run pytest` is
# Python-only and proves nothing about the template, so this is the infra
# equivalent of that gate (AGENTS.md quality gate #2, "Green before done").
# It needs no Azure access: `bicep build-params` is a local compile.
#
# The three @secure() receivers (alertEmail/smsCountryCode/smsPhone) are read
# from the environment via readEnvironmentVariable. Their VALUES are irrelevant
# to a structural compile, so throwaway placeholders are injected only when the
# vars are unset — this never reads, prints, or commits a real value.
#
# Usage: scripts/validate-infra.sh   (exits non-zero on any Bicep diagnostic)

set -euo pipefail

cd "$(dirname "$0")/.."

# Placeholders only when unset/empty; real deploys set these from secrets.
: "${ALERT_EMAIL:=placeholder@example.com}"
: "${SMS_COUNTRY_CODE:=1}"
: "${SMS_PHONE:=5550000000}"
export ALERT_EMAIL SMS_COUNTRY_CODE SMS_PHONE

echo "==> az bicep build infra/main.bicep"
az bicep build --file infra/main.bicep --stdout >/dev/null

echo "==> az bicep build-params infra/main.bicepparam"
az bicep build-params --file infra/main.bicepparam --stdout >/dev/null

echo "OK: infra compiles — no BCP258/BCP-class errors."
