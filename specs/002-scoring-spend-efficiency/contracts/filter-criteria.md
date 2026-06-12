# Contract: Filter Criteria (`filter.toml`)

**Feature**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12

The deterministic relevance gate is configured entirely by `filter.toml`, a
runtime tuning file (FR-002) parsed with stdlib `tomllib`. This contract binds
`filter.load_criteria` / `filter.classify` to the file format and gate
semantics; the committed `filter.toml.example` is the canonical schema reference.

## Trust boundary

- `filter.toml` is **git-ignored** (it encodes personal target functions and
  metros) and delivered to production via the same private Azure Files upload as
  `profile.md` / `screening_prompt.md`, resolved through `store.data_path`
  (Constitution Principle VI; [research.md](../research.md) §3).
- `filter.toml.example` **is committed** — generic, non-personal — so a forker
  has a working schema reference (Principle II forkability).
- Resolution path: `store.data_path("filter.toml")` ⇒ honors `JOBAGENT_DATA_DIR`.

## Schema

```toml
# filter.toml — deterministic pre-LLM relevance gate (feature 002).
# All criteria are tuning, not code: edit and the next run picks them up.

[denylist]
# HARD reject: a posting whose TITLE contains any of these (case-insensitive
# substring) is rejected before the LLM. This is the ONLY gate that rejects a
# posting on its own. Keep to clearly-irrelevant functions.
title_keywords = [
  "sales", "account executive", "accountant", "accounting", "finance",
  "nurse", "clinical", "recruiter", "recruiting", "talent acquisition",
  "marketing", "attorney", "paralegal", "counsel",
]

[allowlist]
# ADVISORY ONLY: target-function title keywords. MAY flag/prioritize but NEVER
# rejects a posting on its own (protects SC-004). May be empty.
title_keywords = [
  "engineer", "engineering", "software", "developer", "platform",
  "technical program manager", "tpm", "engineering manager", "scrum master",
]

[age]
# Reject postings older than this many days WHEN posted_at is present.
# Missing/unparseable posted_at -> fail-open (proceed to the LLM).
max_days = 30

[location]
# Keep a posting when it is remote (and remote_ok), OR a comma-delimited token of
# its location matches `regions`, OR its full location string contains a `metros`
# entry. Reject a present, parseable location that matches none of these. Missing
# location or the default "Unspecified" -> fail-open (proceed to the LLM).
#
# `regions` is matched on the comma-delimited state/region TOKEN (the "WA" in
# "Renton, WA" or "Seattle, WA, USA"), as an exact case-insensitive token match,
# NOT a raw substring -- so "WA" never spuriously matches "Washington, DC". This
# keeps every suburb of a target metro (Renton, Bellevue, King of Prussia)
# without enumerating them; the LLM judges commute nuance.
remote_ok = true
regions = ["WA", "OR", "PA"]
# Optional finer keeps, matched as case-insensitive substrings of the full
# location string -- for forms with no clean state token ("Greater Seattle
# Area", "Bay Area").
metros = ["Remote", "Bay Area"]
```

### Field reference

| Table.key | Type | Required | Default | Used by gate |
|---|---|---|---|---|
| `denylist.title_keywords` | list[str] | yes (may be `[]`) | — | Function denylist (hard) |
| `allowlist.title_keywords` | list[str] | no | `[]` | Target allowlist (advisory) |
| `age.max_days` | int > 0 | no | `30` | Posting-age |
| `location.remote_ok` | bool | no | `true` | Location |
| `location.regions` | list[str] | no | `[]` | Location — state/region **token** keep-list |
| `location.metros` | list[str] | no | `[]` | Location — full-string **substring** keep-list |

## Gate evaluation order and semantics

`classify(posting, criteria)` returns a **machine-readable rejection reason**
string, or `None` if the posting is plausible and should reach the LLM. Gates
run in this order; the **first** rejecting gate wins:

1. **Function denylist (hard reject)** — keys on `posting.title` (always
   present). Case-insensitive substring match against `denylist.title_keywords`.
   On match ⇒ reason `function_denylist:<matched_keyword>`. This is the only
   gate that can reject.
2. **Target allowlist (advisory)** — never returns a reason. Records a
   non-rejecting "on-target" signal only; absence from the allowlist is **not** a
   rejection (SC-004).
3. **Posting-age** — only when `posted_at` is present and parseable. If
   `age_in_days(posted_at) > age.max_days` ⇒ reason `age:<days>d`. Missing or
   unparseable `posted_at` ⇒ **fail-open** (no reason, continue).
4. **Location** — only when `posting.location` is present and not the default
   `"Unspecified"`. Keep the posting if **any** of:
   - it is remote and `remote_ok`;
   - one of its comma-delimited tokens (e.g. `WA` in `"Renton, WA"` or
     `"Seattle, WA, USA"`) **exactly equals** a `location.regions` entry
     (case-insensitive token match — never a raw substring, so `WA` never
     matches `"Washington, DC"`);
   - its full location string **contains** a `location.metros` entry
     (case-insensitive substring — for tokenless forms like
     `"Greater Seattle Area"`/`"Bay Area"`).

   If none match ⇒ reason `location:<location_value>`. Missing or `"Unspecified"`
   location ⇒ **fail-open** (no reason, continue). Region-token matching is what
   keeps target-metro suburbs (Renton, King of Prussia) without enumerating them;
   the LLM does the finer commute judgment.

If no gate rejects, return `None` (plausible ⇒ LLM).

### Reason-string grammar

`<gate>:<detail>` — stable, machine-readable, logged in the `SCORE_SUMMARY`
breakdown and persisted in `postings.filter_reason`:

| Gate | Reason format | Example |
|---|---|---|
| denylist | `function_denylist:<keyword>` | `function_denylist:sales` |
| age | `age:<days>d` | `age:47d` |
| location | `location:<value>` | `location:Berlin, Germany` |

The summary breakdown buckets by the part **before** the first `:`
(`function_denylist`, `age`, `location`).

## Fail-open / fail-loud split

- **Fail-open (per-posting, missing field)**: a posting missing the field a gate
  keys on is **never dropped by that gate** — it proceeds to the LLM (spec edge
  case; SC-004). Only the always-present title can hard-reject.
- **Fail-loud (config, whole run)**: a missing / malformed / structurally-invalid
  `filter.toml` ⇒ `load_criteria` raises and the score stage `sys.exit`s before
  any LLM call (FR-014; [research.md](../research.md) §4). Validation rejects:
  non-list keyword fields, non-string list elements, `max_days` not a positive
  int, `remote_ok` not a bool.

## Committed example

`filter.toml.example` (repo root, tracked) carries exactly the generic schema
shown above — non-personal denylist/allowlist/age/location values that a forker
can copy to `filter.toml` and tune. It is the schema source of truth referenced
by tests and the README runtime-files list.
