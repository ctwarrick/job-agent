# Phase 0 Research: LLM Scoring Spend Efficiency

**Feature**: `002-scoring-spend-efficiency` | **Date**: 2026-06-12 | **Plan**: [plan.md](plan.md)

The spec's three NEEDS CLARIFICATION markers were resolved in the 2026-06-12
`/speckit-clarify` session (spec `## Clarifications`). This document records the
remaining **design** decisions — how the cleared requirements become concrete
module shapes, file formats, and algorithms — before any code or test is written.
Each decision is stated as Decision / Rationale / Alternatives considered.

---

## §1 — Filter as a pure module (`filter.py`)

**Decision**: Implement the deterministic gate as a new pure module
`src/job_agent/filter.py` exposing exactly two functions:

- `load_criteria(path: str | None = None) -> Criteria` — read and validate
  `filter.toml` (stdlib `tomllib`), returning a small immutable value object.
- `classify(posting, criteria) -> str | None` — return a machine-readable
  rejection reason string (e.g. `"function_denylist:sales"`) or `None` if the
  posting is plausible and should reach the LLM.

No class hierarchy, no I/O beyond `load_criteria` reading the file. `classify`
is a pure function of (posting fields, criteria).

**Rationale**: Constitution Principle IV (Simplicity & Stdlib-First) prefers
small pure helpers over classes (CLAUDE.md conventions; existing
`_format_postings`/`_score_batch`). A pure `classify` is testable with zero
stubbing — `tests/test_filter.py` needs no Anthropic or SQLite monkeypatching,
unlike `tests/test_score.py`. Keeping I/O in `load_criteria` only means the hot
loop in `score.py` calls `classify` against already-fetched rows.

**Alternatives considered**:
- *Inline the gate in `score.py`*: rejected — couples the deterministic logic to
  the LLM stage's stubbing harness and bloats the most-edited file.
- *A `Filter` class holding criteria + a `.classify()` method*: rejected — a
  frozen dataclass `Criteria` + free function is the idiomatic shape here and
  matches the codebase's function-first style.

---

## §2 — Gate set and semantics

**Decision**: v1 applies four gates, in this order, with these rejection powers:

1. **Function denylist (hard reject)** — the *only* gate that rejects on its own.
   Match the posting **title** (always present) against configurable keywords for
   clearly-irrelevant functions (sales, finance/accounting, clinical/nursing,
   recruiting/HR, marketing, legal …). First match wins; reason
   `function_denylist:<keyword>`.
2. **Target-function allowlist (advisory only)** — configurable target-function
   title keywords (engineering, TPM, engineering management, scrum master). MAY
   flag/prioritize but MUST NOT reject a posting alone (protects SC-004). In v1
   it records a non-rejecting signal only; it never returns a reason.
3. **Posting-age gate** — reject when `posted_at` is present AND older than
   `[age].max_days` (default 30); reason `age:<days>d`. **Fail-open** when
   `posted_at` is NULL/unparseable.
4. **Location gate** — keep remote roles (configurable `remote_ok`), roles whose
   comma-delimited **state/region token** matches a configurable `regions`
   keep-list (e.g. `WA`/`OR`/`PA`), and roles whose full location string contains
   a configurable `metros` substring; reject clearly out-of-region roles; reason
   `location:<value>`. **Fail-open** when location is missing or the default
   `"Unspecified"`. Region-token (not raw-substring) matching keeps target-metro
   suburbs (Renton→WA, King of Prussia→PA) without enumerating them and avoids
   `WA`-in-`"Washington, DC"` false positives.

Salary floor and seniority stay **LLM** judgments (`comp_flag`, `seniority_fit`):
there is no structured comp field pre-call, and seniority is a weak title-only
signal. Neither is a deterministic gate in v1.

**Rationale**: Directly encodes the clarify answers (spec `## Clarifications`,
FR-004). The denylist-only-hard-reject rule plus age/location fail-open is what
makes SC-004 ("zero plausible matches silently dropped") hold: a missing field
can never cause a drop, and the advisory allowlist can never reject. The title
is the one field guaranteed present (`schema.Posting`), so the hard gate keys on
it. Location default `"Unspecified"` is the existing `schema.Posting` default —
treating it as fail-open is consistent with the upstream contract.

**Alternatives considered**:
- *Make the allowlist a hard gate (reject anything not on it)*: rejected by the
  clarify session — too aggressive, would silently drop plausible adjacent roles
  and violate SC-004.
- *Add a salary-floor deterministic gate*: rejected — no structured comp field
  exists pre-call; comp stays an LLM `comp_flag` judgment (FR-004 note).
- *Description-keyword matching*: deferred — title-only keeps the gate cheap,
  explainable, and low-false-positive for v1.
- *City/metro-name substring for the location gate*: rejected — a metro name is
  not a substring of its suburbs (`"Seattle"` ∉ `"Renton, WA"`,
  `"Philadelphia"` ∉ `"King of Prussia, PA"`), so it would wrongly reject
  in-metro suburbs. v1 keeps at **region/state-token** granularity (plus remote
  and an optional `metros` substring list); the LLM does fine commute judgment.
  Enumerating every suburb was rejected as high-maintenance (a forgotten suburb
  becomes a false reject); a geo-database was rejected as a non-stdlib dependency
  (Principle IV).

---

## §3 — `filter.toml` runtime file and delivery

**Decision**: Filter criteria live in a new runtime file `filter.toml`, parsed
with stdlib `tomllib` (Python ≥ 3.11). It is **git-ignored** and delivered to
production via the same private mechanism as `profile.md`/`screening_prompt.md`
(Azure Files upload under `JOBAGENT_DATA_DIR`, resolved by `store.data_path`). A
generic, **non-personal** `filter.toml.example` IS committed as the schema
reference and forkability aid.

**Rationale**: FR-002 (criteria as runtime tuning, not code) + Constitution
Principle VI (no personal data in repo). The metros and target functions encode
personal preferences, so the live file is private — same trust boundary as the
profile. Unlike the profile/registry (which have no committed example because
they are wholly personal), the filter's denylist/allowlist/age values are mostly
generic, so a committed `.example` lowers the barrier for a forker (Principle II
forkability) without leaking anything personal. `tomllib` is stdlib (Principle IV
— no new dependency); TOML's `[table]`/array syntax fits the
denylist/allowlist/age/location structure cleanly.

**Alternatives considered**:
- *Reuse an existing file* (e.g. append to `screening_prompt.md`): rejected —
  mixes free-text LLM instructions with structured machine-read config; TOML
  parses deterministically.
- *JSON or YAML*: JSON has no comments (criteria benefit from inline notes); YAML
  would add a third-party dependency. `tomllib` is the stdlib win.
- *Commit the real `filter.toml`*: rejected — metros/target functions are
  personal (Principle VI).

---

## §4 — Fail-loud configuration validation

**Decision**: Before any LLM call, the score stage validates configuration and
`sys.exit`s (non-zero, no partial external effect) on:

- `filter.toml` missing, unreadable, or malformed TOML;
- `filter.toml` present but structurally invalid (e.g. a keyword list is not a
  list of strings, `[age].max_days` not a positive int);
- cap env vars present but non-numeric or ≤ 0
  (`JOBAGENT_MAX_POSTINGS_PER_RUN`, `JOBAGENT_MAX_COST_PER_RUN`);
- price env vars present but non-numeric or < 0 (`JOBAGENT_PRICE_*`).

This sits alongside score.py's existing `sys.exit` on missing `ANTHROPIC_API_KEY`
/ `JOBAGENT_SALARY_FLOOR`, and runs **before** the client is exercised.

**Rationale**: Constitution Principle V (Fail Loud) + FR-014. A mis-typed cap or
a corrupt filter file must stop the run rather than silently scoring everything
(the exact failure mode the cold-start surprise taught us). `main.py` already
treats a stage `sys.exit` as fatal, so this composes with existing behavior.
Validation precedes the LLM so no spend occurs on a misconfigured run.

**Alternatives considered**:
- *Warn and continue with defaults*: rejected — silently scoring everything on a
  broken filter is precisely the unbounded-spend risk the feature exists to kill.
- *Validate lazily per-posting*: rejected — fail at load time, once, before the
  first call; cheaper and louder.

---

## §5 — Per-run budget cap algorithm

**Decision**: The run stops at the first of two limits:
`JOBAGENT_MAX_POSTINGS_PER_RUN` (default 200 postings scored) **OR**
`JOBAGENT_MAX_COST_PER_RUN` (default $5.00 estimated). Algorithm:

- Iterate scorable postings in existing `BATCH`-sized chunks.
- **Posting cap**: trim the *final* batch so the total scored never exceeds the
  cap — honored at posting granularity, not rounded up to a whole batch (spec
  edge case "cap smaller than one batch").
- **Cost cap**: before issuing a batch, add its *projected* cost (from a simple
  per-posting token estimate × configured prices, see §7) to the running spend;
  if that projection would cross the dollar cap, stop **before** the call rather
  than after.
- On hitting either limit, stop the loop, emit `SCORE_CAP_STOP …`, and **return
  normally** (the process exits 0). Already-scored batches are already committed
  (FR-007 — existing incremental per-batch commit), so the next scheduled run
  resumes the remaining scorable postings.

**Rationale**: FR-005/006/008 + spec Assumptions. Trimming the final batch honors
the cap exactly (SC-002). Checking the *projected* cost pre-call keeps actual
spend ≤ cap rather than overshooting on the batch that crosses it. A cap stop is
a **normal** event (a large backlog draining over several runs), so it must not
mark the scheduled job failed — hence a clean exit 0, consistent with the spec
Assumption that defaults to "loud-but-non-failing." The existing per-batch commit
(proven: 366 rows survived a kill) means "resume" needs no new transactional
design — just a scorable-set query (data-model §`scorable()`).

**Alternatives considered**:
- *Post-call cost check*: rejected — overshoots the cap by up to one batch.
- *Cap rounded up to whole batches*: rejected by spec edge case — must honor
  posting granularity.
- *Exit non-zero on cap stop*: rejected — would mark a normal partial-progress
  run as failed and could trip the 001 missed-deadline alert; cap stop is success
  with remaining backlog.

---

## §6 — Prompt caching of the static prefix

**Decision**: Move the candidate profile + screening prompt into a single cached
`system` block using `cache_control: {"type": "ephemeral"}`; the per-batch
postings stay in the volatile `user` turn. Concretely, `_score_batch` changes
from "screening prompt in `system`, profile in the user message" to "screening
prompt **and** profile in a cached `system` block, only the postings block in the
user message."

- The static prefix (profile + screening prompt) comfortably clears the
  **2048-token** minimum cacheable prefix for `claude-sonnet-4-6`, so the cache
  marker is honored.
- Within a run, batches 2..N read the prefix from cache; FR-009 satisfied.
- FR-010 is **free**: a profile/screening-prompt change alters the cached
  content, which changes the cache key, producing a cache miss that
  automatically bills and serves the new content — no stale prefix is ever
  served across a content change. No manual invalidation needed.
- Verify in tests/observability via `usage.cache_read_input_tokens` (> 0 on
  batches after the first) and `usage.cache_creation_input_tokens` (> 0 on the
  first batch).

**Rationale**: FR-009/010 + Constitution Principle VII. The profile + screening
prompt are byte-identical across every batch in a run, so they are the natural
cache prefix; the postings are the only volatile part and belong in the user
turn. Anthropic ephemeral caching keys on content, so content-change
invalidation is automatic — the cleanest possible FR-010. The 2048-token floor
for sonnet-4-6 is the only correctness constraint, and the profile + screening
prompt clear it.

**Alternatives considered**:
- *Cache per-posting or per-batch content*: rejected — postings differ every
  batch; caching them yields no hit and adds cache-write cost.
- *Manual cache-version key tied to a profile hash*: rejected — content-keyed
  ephemeral caching already invalidates on change; a manual version is redundant.
- *Leave profile in the user turn*: rejected — splitting the static prefix across
  `system` and `user` shrinks the cacheable prefix and risks dropping below the
  2048-token floor.

---

## §7 — Cost estimation from configurable prices

**Decision**: Accumulate the Anthropic `usage` fields across all batches in a run
and compute an estimated dollar cost from **env-configurable per-MTok prices**.
The four `usage` fields consumed: `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`. Defaults match
`claude-sonnet-4-6` list pricing (per million tokens):

| Component | Env var | Default ($/MTok) | Relation |
|---|---|---|---|
| Input (uncached) | `JOBAGENT_PRICE_INPUT` | `3.00` | base |
| Output | `JOBAGENT_PRICE_OUTPUT` | `15.00` | base |
| Cache write (5-min) | `JOBAGENT_PRICE_CACHE_WRITE` | `3.75` | 1.25× input |
| Cache read | `JOBAGENT_PRICE_CACHE_READ` | `0.30` | 0.10× input |

`estimated_cost = (input_tokens·P_in + output_tokens·P_out +
cache_creation·P_cw + cache_read·P_cr) / 1e6`. Emitted once per run on the
`SCORE_SUMMARY` line alongside fetched/filtered(with reason breakdown)/scored/
remaining counts and the raw token totals.

**Rationale**: FR-011/012 + spec Assumption "estimated cost is a configurable
approximation, not a billing source of truth." Splitting cache-write vs.
cache-read prices is what makes the §6 caching win visible in the summary (and
keeps the §5 pre-call cost projection honest). Defaults track the documented
sonnet-4-6 rates so an unconfigured deployment still estimates sensibly; env
overrides absorb future price drift without a code change (FR-012, Principle I
dual-bill reconciliation). All four `usage` fields are present on the Anthropic
response `usage` object.

**Alternatives considered**:
- *Single blended price*: rejected — hides the cache savings and mis-estimates
  output-heavy vs. input-heavy runs.
- *Hardcode prices*: rejected — FR-012 requires env-configurability; prices drift.
- *Query a live pricing API*: rejected — adds a network dependency and a failure
  mode for an approximation that only needs to be "close enough" for the cap and
  the morning-after summary.

---

## Resolved — no open questions

All NEEDS CLARIFICATION are closed (spec markers in the clarify session; design
choices above). Phase 1 artifacts ([data-model.md](data-model.md),
[contracts/filter-criteria.md](contracts/filter-criteria.md),
[contracts/runtime-config.md](contracts/runtime-config.md),
[quickstart.md](quickstart.md)) derive directly from these decisions.
