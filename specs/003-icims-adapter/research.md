# Phase 0 Research: iCIMS access method

Recon to resolve the one real unknown — how to read public iCIMS postings —
before any production constant or test fixture is written. Sources are public
documentation and scraping write-ups; the **live per-tenant confirmation is an
implementation step (plan step 1)**, not done here.

## Decision

**Access public iCIMS career sites at `{tenant}.icims.com` (no auth), preferring
`sitemap.xml` for discovery + server-side-rendered HTML job-detail pages, with
`/jobs/search?pr={page}&in_iframe=1` paginated HTML as the listing fallback.
Parse with the stdlib (`xml.etree.ElementTree` for the sitemap, `html.parser`/
`re` for fields). Confirm the exact method per confirmed tenant in a live recon
spike and capture real fixtures before writing tests.**

Concrete shapes found (to be confirmed live):

- Career-site host: `{tenant}.icims.com`, also `careers-{tenant}.icims.com`,
  `careers.{tenant}.icims.com`, or a custom domain (e.g. `jobs.{co}.com`).
- Discovery: `https://{tenant}.icims.com/sitemap.xml` lists every job URL with
  `lastmod` in one request.
- Listing: `https://{tenant}.icims.com/jobs/search?pr={page}&in_iframe=1`,
  `pr` zero-indexed.
- Detail: `https://{tenant}.icims.com/jobs/{id}/{slug}/job?in_iframe=1`; numeric
  job ID via regex `/jobs/(\d+)/` — a clean, stable dedupe key.
- Rate limit: ~1 request / 2–3 s recommended to avoid bot detection → reuse the
  existing `time.sleep` politeness pattern.

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

## Open items the live spike must close (plan step 1)

1. A tenant on a `jobs.{co}.com` custom domain: is it an iCIMS custom domain or
   a different vendor? Detection hook: career HTML / redirects contain
   `icims.com`. If not iCIMS, flag — do not force-fit.
2. The second confirmed tenant: resolve the real `{tenant}.icims.com` host and
   access method.
3. Per tenant: does the internal JSON endpoint exist, or is it sitemap+HTML?
4. Capture one listing/sitemap + one detail response per tenant as fixtures.
5. Confirm the US-location field present on listing or detail for pre-LLM
   filtering, and the posted-date field if any.
