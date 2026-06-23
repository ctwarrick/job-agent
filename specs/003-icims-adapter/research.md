# Phase 0 Research: iCIMS access method

Recon to resolve the one real unknown — how to read public iCIMS postings —
before any production constant or test fixture is written. Sources are public
documentation and scraping write-ups; the **live per-tenant confirmation is an
implementation step (plan step 1)**, not done here.

## Decision

**Access the public iCIMS career site through its Jibe front-end JSON API at
`https://{host}/api/jobs?page={n}` (no auth, `page` 1-indexed). Each response is
JSON carrying a full page of jobs with descriptions inline, so parsing is the
stdlib `json` module only — no HTML/XML parsing and no per-job second
round-trip. `{host}` is the career-site host from the registry slug (a custom
domain, or `{tenant}.icims.com` when none is given). The classic
`sitemap.xml` + server-side-rendered HTML detail method is retained only as a
documented fallback for older non-Jibe iCIMS tenants.**

Verified live against one confirmed tenant in the recon spike (Phase 0; results
below). Concrete confirmed shape:

- **Endpoint**: `GET https://{host}/api/jobs?page={n}` → JSON. Top-level keys
  `jobs` (list, 10 per page), `totalCount`/`count` (int), `filter`.
- **Item shape**: each list element is `{"data": {…job…}}` — the adapter reads
  `item["data"]` (inline field access, not a helper).
- **Fields** (under `data`): `req_id` (numeric job ID → stable dedupe key),
  `title`, `description` (full HTML, **inline** — no detail fetch needed),
  `country_code` (ISO-2, e.g. `US`/`AU` → deterministic US filter),
  `full_location`/`location_name` (display location), `apply_url` (canonical
  posting URL), `posted_date` (ISO-8601), `hiring_organization`/`brand`
  (display name), `ats_code` (`"icims"` — backend confirmation).
- **Pagination**: page through `?page=n` until an empty `jobs` list or
  `n*page_size >= totalCount`.
- **Rate limit**: page requests are few (10 jobs/page); reuse the existing
  `time.sleep(0.5)` politeness pattern between pages, consistent with the
  sibling adapters.

This is materially simpler than the originally-assumed sitemap+HTML scrape:
inline descriptions remove the second round-trip, and `country_code` gives a
clean US filter without parsing free-text location strings.

## Rationale

- The public portal is **server-side-rendered HTML with no JSON-LD and no
  guaranteed public JSON API**, so unlike Greenhouse/Lever/Workday there is no
  clean JSON list endpoint to rely on universally. The sitemap gives cheap,
  complete discovery in one request; detail pages carry the fields the `Posting`
  schema needs.
- Numeric job IDs give a deterministic dedupe fingerprint input, matching how
  the schema already fingerprints postings.
- Stdlib parsing keeps Constitution IV (stdlib-first) intact; a parser
  dependency is deferred unless the spike proves it necessary.

## Alternatives considered

- **Authenticated Job Portal API** (`https://api.icims.com/customers/
  {customerId}/search/portals/{portalIdOrName}`, clean JSON, `id`-ordered
  paging): rejected — requires per-customer credentials we do not have and
  cannot obtain for third-party employers. Not viable for public scraping.
- **Standard XML / Job Feed Service** (iCIMS pushes a normalized XML feed 3×/day
  to partner job boards): rejected — it is a partner-distribution feed, not a
  per-tenant endpoint a consumer can pull on demand.
- **Internal `/jobs/intelliservices`-style JSON endpoint**: some write-ups claim
  a client-side JSON endpoint exists; others state the portal is pure SSR HTML.
  **Contradictory** — kept as a candidate the spike checks per tenant; if a
  given tenant exposes it, that tenant uses JSON (simpler), else fall back to
  sitemap+HTML.
- **`beautifulsoup4` for HTML parsing**: deferred — adopt only if stdlib
  extraction proves brittle in the spike, with a Complexity Tracking row.

## Live spike results (plan step 1 — closed)

The Phase 0 recon spike ran against the two candidate tenants. Outcome:

1. **Custom-domain tenant flagged as NOT iCIMS.** The `jobs.{co}.com` candidate
   resolved to a different vendor (a Radancy/TalentBrew front over a Workday
   backend — zero `icims.com` markers, all `*.icims.com` host variants 404).
   Per the "do not force-fit" rule it was reclassified as a Workday
   verify-then-wire target, out of scope for this adapter. The candidate list
   in the git-ignored plans doc was corrected accordingly.
2. **Confirmed iCIMS tenant verified** on a custom career-site domain. It runs
   the **Jibe** front-end (an iCIMS-owned career-site platform) exposing the
   JSON API in the Decision above; `ats_code: "icims"` confirms the backend.
3. **Internal JSON endpoint exists** (the Jibe `/api/jobs` API) — so the
   sitemap+HTML path is unnecessary for this tenant. (Its `sitemap.xml` exists
   but ships empty `<loc>` tags, since the board is client-rendered; the JSON
   API is the correct access path.)
4. **Fixtures**: the captured JSON shape seeds the stub tests. Per the
   sibling pattern (`tests/test_workday.py`) and the personal-data-privacy
   rule, committed fixtures are synthetic payloads built to match this real
   shape, not raw captures of a real employer's postings.
5. **US filter field confirmed**: `country_code` (ISO-2) is present per job —
   keep `US`, drop positively-identified non-US, retain missing/empty
   (FR-004). `posted_date` is present (ISO-8601).
