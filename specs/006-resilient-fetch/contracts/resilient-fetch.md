# Contract: Resilient, Time-Bounded Fetch

This is the shared contract every in-scope adapter (Workday, iCIMS, Talemetry)
and the orchestration honor. Out-of-scope adapters (Greenhouse, Lever) are
exempt and keep their single-call `fetch`.

## 1. Two-phase in-scope adapter interface

```python
def list_postings(slug: str, *, company: str | None = None,
                  timeout: int = 20) -> list[Posting]:
    """Paginate the LISTING only. Return listing-level Posting stubs
    (title, location, posted_at, url, external_id; description="" unless the
    board returns it inline, as iCIMS does). MUST NOT fetch per-posting detail.
    Per-page failure is logged and skipped; postings from earlier pages are
    retained (FR-005). Raises only on a whole-source failure (FR-006)."""

def fetch_description(posting: Posting, *, timeout: int = 20) -> str:
    """Retrieve the single posting's full description (the expensive call).
    iCIMS: pass-through returning posting.description (inline). Raises on a
    per-item failure; the orchestration logs+skips and continues (FR-005)."""
```

Registration: in-scope adapters are routed through `resilient.run_source`;
`fetch.ADAPTERS` maps vendor → an object/namespace exposing the two functions
(out-of-scope vendors keep their plain `fetch`).

## 2. Orchestration contract

```python
def run_source(adapter, source, *, criteria, store_, clock=time.monotonic,
               now=lambda: datetime.now(timezone.utc)) -> SourceResult: ...
```

Behavior (the durable contract — see plan D2):

1. `stubs = adapter.list_postings(source.slug, company=source.company)`.
2. `survivors = [s for s in stubs if classify(dict_view(s), criteria) is None]`
   — listing-level filter; descriptions are NOT fetched for rejects (FR-003/4).
3. `already = store_.existing_external_ids(source.vendor, source.company)`;
   `todo = [s for s in survivors if s.external_id not in already]`
   — forward progress (FR-015): only not-yet-stored survivors.
4. For each `s` in `todo`, in order, until a backstop bound is hit:
   - **cap**: stop when described count ≥ `JOBAGENT_MAX_DETAIL_PER_SOURCE`.
   - **deadline**: stop when `clock() > start + JOBAGENT_FETCH_DEADLINE_SECONDS`.
   - else `desc = s.description or adapter.fetch_description(s)`; on success
     collect `replace(s, description=desc)`; on exception, `skipped += 1`, log,
     continue (FR-005 skip-not-abort).
5. `store_.upsert_postings(described)` — fingerprint final at insert (D3).
6. Convergence/staleness (FR-015):
   - `remaining = len(todo) - processed`.
   - if `remaining == 0`: `store_.mark_converged(now())`; `persistent=False`.
   - else: `truncated=True`; `persistent = now() - last_converged > BOUND_DAYS`.
   - seed `last_converged_at=now()` on first sighting of the source.
7. Return `SourceResult(source, company_slug, new=len(described),
   skipped, truncated, remaining, persistent, error=None)`.

A wholly-failed `list_postings` (raises) yields the existing failed-source path:
`SourceResult(..., error=str(e))`, contained by `fetch.main` (FR-006).

## 3. New `store` functions

```python
def existing_external_ids(source: str, company: str,
                          path: str | None = None) -> set[str]: ...
def get_last_converged(source: str, company: str,
                       path: str | None = None) -> str | None: ...
def mark_converged(source: str, company: str, when: str,
                   path: str | None = None) -> None: ...
def seed_source(source: str, company: str, when: str,
                path: str | None = None) -> None: ...
```

`upsert_postings` return value is corrected to the postings-insert count only
(FR-011): snapshot `conn.total_changes` between the postings `executemany` and
the applications `executemany`, and return that delta.

## 4. Backstop / config surface

| Env var | Default | Validated | Effect |
|---|---|---|---|
| `JOBAGENT_MAX_DETAIL_PER_SOURCE` | 150 | int > 0, else fail-loud | per-source detail-fetch cap |
| `JOBAGENT_FETCH_DEADLINE_SECONDS` | 300 | int > 0, else fail-loud | per-source wall-clock budget |
| `JOBAGENT_STALENESS_BOUND_DAYS` | 7 | int > 0, else fail-loud | days truncated → persistent alert |

`JOBAGENT_MAX_POSTINGS_PER_EMPLOYER`: **removed** from adapters. Registry
`max_per_employer`: accepted-but-ignored (deprecated), no fail-loud.

## 5. SourceResult → digest degradation (FR-014)

`fetch.main` returns `(failed_sources, partial_sources)`:

- `failed_sources`: `{source, company_slug, error}` — wholly unreachable (existing).
- `partial_sources`: `{source, company_slug, new, skipped, truncated, persistent}`
  for any source with `skipped>0` or `truncated` or `persistent`.

`digest._degradation_facts/_messages` gain a **partial / degraded** category,
rendered distinct from "unreachable" and from a healthy run:

- truncated (within bound): "_source partially fetched; N described, M skipped,
  more queued for the next run_".
- persistent (beyond bound): "_source has been behind for >D days; raise the
  budget, tighten the filter, or drop it_" (FR-015 loud alert).

No raw adapter error text is included in digest-facing output (Principle VI,
existing rule).

## 6. Test contract (TDD red-first, stub-based — FR-012)

- `classify` runs at listing level; a rejected stub triggers **zero**
  `fetch_description` calls (assert call count). SC-004.
- Nth `fetch_description` raises ⇒ that posting skipped, others stored, run
  continues (FR-005). Whole listing page raises ⇒ earlier pages retained.
- Backstop: with `MAX_DETAIL_PER_SOURCE=K`, exactly K detail calls; a fake clock
  past the deadline stops mid-list; `truncated=True`, `remaining>0`.
- Forward progress: pre-seed store with some survivors' external_ids ⇒ this run
  describes only the rest; across two runs the whole survivor set is covered with
  no re-described prefix.
- Staleness: `last_converged_at` older than the bound + `remaining>0` ⇒
  `persistent=True`; `remaining==0` ⇒ `mark_converged` called, `persistent=False`.
- `upsert_postings` of K brand-new postings returns K (not 2K); 0 when all exist.
- Digest: a `partial_sources` entry renders the degraded category in text+HTML.
- Greenhouse/Lever paths and tests are unchanged (FR-008).
