# Atlas Backend — Architecture

## Core philosophy

The projected public record is **not** the truth.  
The evidence chain is the truth.  
The projection is the current best explanation of that evidence.

---

## Data model overview

```
Source → IngestionRun → RawSnapshot
                         ↓
                        Claim (field_name, field_value, source_id, event_id)
                         ↓
              ClaimConflict (when ≥2 sources disagree on same field)
                         ↓
              ProjectedAccidentRecord (built from winning claims)
```

---

## Ingestion flow

1. **Validate JSON/size/fingerprint** — raw payload and claim values must be real JSON; the full submission is fingerprinted before mutable state is consulted.
2. **Idempotency guard** — a client-provided idempotency key maps to `(source_id, ingestion_run_id)`.
   - Same run + same full submission → replay the persisted ingestion result.
   - Same run + different submission → reject with an idempotency mismatch.
   - Snapshot exists but no completed result/claims → concurrent ingestion in progress → `IngestionInProgressError`.
3. **Validate source/event** — source and event (if supplied) must exist before anything new is written.
4. **Normalize** — durable `Source.field_mapping_json` source-specific field mappers run before conservative generic fallback aliases, then the source-kind normalizer coerces types (dates, integers, registrations). Ambiguous names like plain `date` must be mapped per source, not globally. Unknown fields keep their raw value under a normalized field key. Identity-critical normalization errors reject the ingestion; non-identity failures preserve the raw value under the canonical field name.
5. **ensure_started** — create an `IngestionRun` row with `status=running`.
6. **Canonical event resolution** — explicit `event_id`, stable `source_record_id`, and synchronous `event_identity_index` matching determine the canonical event before claims are written.
7. **Maintain identity index** — every successful ingestion with identity-bearing claims updates the canonical event's synchronous identity row so later anonymous ingestions can find it before projections exist.
8. **Write claims** — one `Claim` row per field, plus a `ClaimHistory` row with `action=created`.
9. **Conflict detection** — for each field with ≥2 active claims that disagree:
   - OPEN conflict exists → merge new claim into it; reopen if it was previously RESOLVED and the new evidence contradicts the winner.
   - No open conflict → `try_add_open` (DB-level uniqueness via partial index prevents duplicates under concurrent ingestion).
10. **Outbox** — write a `CLAIMS_UPDATED` `OutboxEvent` so downstream projection rebuild is decoupled from ingestion latency.
11. **Finish** — `IngestionRun.status = finished`.

---

## Conflict lifecycle

```
                          ┌─────────────────────────────────────┐
  New contradicting       │                                     │  Another source
  evidence arrives   ─────▶  OPEN                              │  contradicts resolved
                          │  (unresolved_conflict_fields shows  │  winner
                          │   DISPUTED in projection)           │
                          └────────────┬────────────────────────┘
                                       │  Curator picks winner
                                       │  (resolve endpoint)
                                       ▼
                                  RESOLVED
                            (projection shows concrete value;
                             losing claims are SUPERSEDED)
                                       │
                                       │  Curator changes mind
                                       │  (reopen endpoint)
                                       ▼
                              OPEN  (again)
                            (SUPERSEDED losers are reactivated to their
                             prior claim type from history; projection shows
                             DISPUTED again)
```

### Claim types

| Type | Meaning |
|---|---|
| `RAW` | Directly from a source, not yet adjudicated |
| `CONFIRMED` | Elevated by a curator but not from an override |
| `MANUAL_OVERRIDE` | A curator-entered value (new claim, not from any source) |
| `SUPERSEDED` | Lost a conflict resolution; excluded from projections |

Only `RAW`, `CONFIRMED`, and `MANUAL_OVERRIDE` claims are "active" and appear in projections (`ClaimType.active_values()`).

### Resolve — what happens

1. **Version check** — `expected_version` must match the DB row; returns `409 ConflictModifiedError` if stale.
2. For manual override: a new `MANUAL_OVERRIDE` claim is created under the `CuratorOverride` source.
3. For existing claim win: `claim.can_win()` must be true (claim type must be active).
4. All other active claims for the same field are `bulk_supersede`d (`claim_type = SUPERSEDED`, `superseded_by_claim_id = winner.id`).
5. Conflict row: `status = RESOLVED`, `winning_claim_id`, `last_modified_note` (curator reason), version incremented.
6. Activity log entry: `OPEN → RESOLVED`.
7. `ReProjectEvent` runs immediately; the field becomes a concrete value in the projection.

### Reopen — what happens

1. **Version check** — same optimistic lock.
2. `find_superseded_by(winner_id)` finds all claims the previous winner displaced.
3. `bulk_unsupersede` restores their previous claim type from `ClaimHistory` and clears `superseded_by_claim_id`.
4. Conflict row: `status = OPEN`, `winning_claim_id = None`, `last_modified_note`, version incremented, reason = `USER_REOPENED`.
5. Activity log entry: `RESOLVED → OPEN`.
6. `ClaimHistory` rows with `action=reactivated` for each un-superseded claim.
7. `ReProjectEvent` re-runs; the field becomes `DISPUTED` again in the projection.

### Reopen paths are intentionally asymmetric

There are **two** routes from `RESOLVED` to `OPEN`, and they behave differently on purpose:

| Trigger | Use case | Prior losers (`SUPERSEDED`) |
|---|---|---|
| Curator clicks "reopen" | `ReopenConflict.execute` | **Reactivated** via `bulk_unsupersede`; `ClaimHistory` action=`reactivated` |
| New source arrives with contradicting value | `ConflictReconciler._reopen_resolved_for_evidence` | **Left SUPERSEDED**; no `reactivated` history row |

The reasoning:

- A curator reopen means "my earlier resolve decision is suspect — restore the prior dispute exactly as it was."  Reactivating the losers is required.
- An ingestion-triggered reopen means "a new dispute has emerged — the earlier resolve decision still stood at the time it was made."  The new dispute is between the prior winner and the new claim only; the older losers were correctly displaced and stay that way.

If a curator wants the older losers in the active set after an ingestion-triggered reopen, they must resolve the now-open conflict first, then use the explicit `ReopenConflict` endpoint.  `ReopenConflict` refuses to run on a conflict that is already `OPEN`, so the two paths cannot accidentally interleave.

This invariant is pinned by `tests/domain/test_reopen_path_asymmetry.py`.

---

## Projection builder

`ProjectedAccidentRecord` is built from scratch on every reproject:

1. Load all **active** claims for the event.
2. For each field, run `WinnerPolicy.choose_winner`:
   - Claims with `MANUAL_OVERRIDE` always win.
   - Among equal-type claims, higher `reliability_tier` wins.
   - Ties broken by `claim.created_at` (oldest wins) then `claim.id` (deterministic).
3. Fields with ≥2 active claims that disagree (after normalization) and no open conflict are listed in `unresolved_conflict_fields` as `DISPUTED`.
4. `completeness_score = non-null fields / REQUIRED_FIELDS_COUNT` (float in [0, 1]).
5. A `ProjectionHistory` row is written with `changed_fields` diff vs the previous projection.

Reprojection is **idempotent** for a given `caused_by_outbox_event_id`: if a `ProjectionHistory` row already exists for that outbox event, the reproject returns the current projection without writing new rows.

---

## Outbox worker state machine

```
PENDING ──(lock)──▶ PROCESSING ──(success)──▶ PROCESSED
                         │
                         ├──(failure, attempts < max)──▶ FAILED
                         │                                  │
                         │                     (next_attempt_at ≤ now)
                         │                                  │
                         │                                  └──▶ PENDING (retry)
                         │
                         └──(failure, attempts ≥ max)──▶ DEAD_LETTER
```

Backoff: `next_attempt_at = now + min(2^attempt_count, 1800) seconds`.

Stale lock recovery: PROCESSING events locked longer than `OUTBOX_STALE_LOCK_MINUTES` are returned to PENDING by `requeue_stale_locked`.

---

## Transaction boundaries

| Layer | Responsibility |
|---|---|
| Use case | Owns the unit of work; calls `await uow.commit()` exactly once at the end |
| Repository | Queries and mutations only; never commits |
| `get_uow` dependency | Yields UoW; rolls back on unhandled exceptions |
| `last_used_at` | Independent short-lived session (never the request session) |
| Outbox worker | Each event processed in its own transaction; lock acquisition is a separate committed transaction |
| Rebuild | Each event commits individually so failures are isolated |

---

## API authentication

All non-health endpoints require an `X-API-Key` header. Keys are stored as HMAC-SHA256 (when `API_KEY_HASH_SECRET` is set) or plain SHA-256. The `role` column on `api_keys` gates write operations:

| Role | Can read | Can ingest | Can resolve/reopen | Can rebuild/admin |
|---|---|---|---|---|
| `analyst` | ✓ | | | |
| `reviewer` | ✓ | ✓ | ✓ | |
| `admin` | ✓ | ✓ | ✓ | ✓ |

---

### Public surface (Phase 1)

```
GET  /api/v1/public/events                   — keyset-paginated list of PUBLISHED
GET  /api/v1/public/events/{slug}            — detail (editorial + projection)
GET  /api/v1/public/events/{slug}/evidence   — public-safe claims + sources
GET  /api/v1/public/events/{slug}/timeline   — Chronos timeline
GET  /api/v1/public/events/{slug}/related    — related events via Orion
```

Public read paths still require `X-API-Key` with a reader role in
Phase 1. Anonymous public access is an open product decision deferred
to the Phase 10 CMS-like content work, where it shares the same auth
shape question.

### What does *not* live in the publication layer

- structured projected fields → stay in `ProjectedAccidentRecord`;
- claim and source data → stay in the claims/sources subsystem;
- conflict and audit semantics → stay in conflicts/provenance.

If a request seems to want publication-layer ownership of a fact (for
example, "let editors fix the operator on a page"), the right path is
a `MANUAL_OVERRIDE` claim through the existing conflict-resolution
flow, not a new editable field on `public_event_pages`.

---

## Adding a new migration

```bash
alembic revision -m "describe_the_change"
# Edit the generated file in alembic/versions/
alembic upgrade head
```

Always test migrations from scratch against a disposable database only: `ATLAS_ALLOW_DB_RESET=1 make migrate-check`.

---

## Publication overlay (Phase 1)

The public encyclopedia surface lives in its own bounded context,
`atlas.domain.publication`, deliberately separate from the core
claim/evidence/projection model. It is a thin **editorial overlay**:
nothing in the publication tables is the source of truth for any
structured fact.

```
accident_events  ──┐
                   │
claims ──► conflicts ──► projected_accident_records  ← SOURCE OF TRUTH
                                          ▲
                                          │   (read at response time)
                                          │
                          ┌───────────────┴───────────────┐
                          │      public_event_pages       │
                          │  (slug, title, summaries,     │
                          │   narrative, publication      │
                          │   state, version)             │
                          └───────────────────────────────┘
```

### Invariants

- **Editorial ≠ evidence.** `public_event_pages.title`,
  `short_summary`, and `narrative_markdown` are curator-supplied prose.
  Structured fields (operator, aircraft type, fatalities, location)
  are read from `projected_accident_records` at response time. A
  curator cannot silently override a projected fact by editing the
  page — that work is the manual-override claim path, not the page.
- **Slug is the only public stable identifier.** Internal `event_id`
  UUIDs are never exposed on public surfaces (the detail response
  surfaces `canonical_event_id` for audit linking; that is a single
  intentional exception, not a pattern to copy).
- **One canonical page per event.** Unique indexes pin both
  `slug` and `event_id`. The repository maps these to
  `SlugAlreadyTakenError` and `PublicEventPageAlreadyExistsError`.
- **Merge-aware reads.** Every slug-keyed read walks
  `merged_into_event_id` to the surviving event before pulling the
  projection. Cycles fail closed (404), not loop. The walker mirrors
  `QueryAccidentPublicView._canonical_event_id`.
- **Status gates response codes.** DRAFT pages return 404 (so DRAFT
  existence is not observable). RETRACTED pages return 410 with the
  retraction note in the body. PUBLISHED with a missing projection
  also returns 404 (curator bug surface, not crash).

### Lifecycle (Phase 9)

```
              create
                 │
                 ▼
              DRAFT ◄──── request_changes ─── IN_REVIEW
                 ▲ │  ▲                          │
                 │ │  │                          │ approve
                 │ │  │ reject                   ▼
                 │ │  └──────────────────── APPROVED
                 │ │                            │
        reopen   │ │ submit                     │ publish
                 │ │                            │
              ARCHIVED ◄─── archive ─── PUBLISHED ── retract ──► RETRACTED  (terminal)
                 │                          ▲
                 └──── publish ─────────────┘
```

Six states, with RETRACTED as the only terminal state. Key contracts:

- **RETRACTED is forever.** Once a page is retracted it stays at the
  slug as a 410 Gone forever — curators of inbound links can update
  references. If the content is meant to be re-published later, it
  must go out under a new slug.
- **ARCHIVED is the soft-hide.** Use this when content is temporarily
  withdrawn but may return. `publish` from ARCHIVED goes directly to
  PUBLISHED and preserves `first_published_at`.
- **Edit only in DRAFT.** Phase 9's update use case rejects edits to
  pages in any other state. Editors send a page back to DRAFT via
  `request_changes` (from IN_REVIEW) or `reject` (from APPROVED), or
  via `reopen` (from ARCHIVED).
- **Optimistic concurrency via `version`.** Every mutation passes
  `expected_version` and the repository raises
  `PublicEventPageModifiedError` (→ HTTP 409) on a clash. The
  response body carries `actual_version` so a UI can refetch and
  retry.

### Editorial surface (Phase 9)

```
POST   /api/v1/editorial/pages                    create DRAFT
GET    /api/v1/editorial/pages                    list (any non-RETRACTED)
GET    /api/v1/editorial/pages/{id}               load
PATCH  /api/v1/editorial/pages/{id}               edit in place (DRAFT only)
POST   /api/v1/editorial/pages/{id}/submit        DRAFT      → IN_REVIEW
POST   /api/v1/editorial/pages/{id}/request-changes  IN_REVIEW → DRAFT
POST   /api/v1/editorial/pages/{id}/approve       IN_REVIEW  → APPROVED
POST   /api/v1/editorial/pages/{id}/reject        APPROVED   → DRAFT
POST   /api/v1/editorial/pages/{id}/publish       APPROVED   → PUBLISHED
                                                  ARCHIVED   → PUBLISHED
POST   /api/v1/editorial/pages/{id}/archive       PUBLISHED  → ARCHIVED
POST   /api/v1/editorial/pages/{id}/reopen        ARCHIVED   → DRAFT
POST   /api/v1/editorial/pages/{id}/retract       PUBLISHED  → RETRACTED  (admin)
GET    /api/v1/editorial/pages/{id}/revisions     audit trail
```

All editorial endpoints require reviewer or admin. `retract` is
admin-only because RETRACTED is terminal and forever-visible on the
public surface.

### Revision audit trail

Every transition writes an immutable `public_event_page_revisions`
row capturing:

- the page's version after the transition;
- the from-status and to-status (NULL → DRAFT on creation);
- a snapshot of editorial content at that moment (title, summary,
  narrative);
- the authenticated `editor_user_id` (always from the session, never
  from the request body);
- an optional `transition_reason` and `correction_note`.

The repository surface for revisions exposes `add_revision` and
`list_revisions` only — no update or delete. The audit table is
append-only by convention; future review of repository code should
hold this line.

### Editorial fields vs. evidence

The editorial workflow can write only four fields on a page row:
`title`, `short_summary`, `narrative_markdown`, `slug`. The update
use case rejects anything else with `EditorialFieldLockedError` (and
the request schema's `extra='forbid'` catches it at the boundary).
Changing a projected fact (operator, fatalities, ...) is not an
editorial action — it's a manual-override claim through the existing
conflict-resolution flow.

---

## Search (Phase 2)

The search index is a materialised projection of the publication
layer, owned by `atlas.domain.search` and `atlas.infrastructure.db.repositories.search`.

```
PublicEventPage (PUBLISHED) ──upsert──► search_index_entries
                            ◄──delete──   (on archive/retract)
```

### Invariants

- **The index contains exactly the PUBLISHED rows.** Phase 9's
  `PublishPublicEventPage`, `ArchivePublicEventPage`, and
  `RetractPublicEventPage` hold the hooks; the search index is
  written/deleted inside the same unit of work as the state change so
  a failed index write rolls back the transition.
- **No DRAFT or IN_REVIEW page ever appears in search results.**
  Editorial content under review is curator-only; search would leak
  it otherwise.
- **No tenant-private data.** Tenant overlays (Phase 5) have their
  own search surface; the public index is exclusively for public
  PUBLISHED rows.

### Backend

Phase 2 ships `SqlPostgresFtsSearchRepository` — Postgres FTS with a
weighted `tsvector` (title=A, summary=B, projection facets=C,
narrative=D), GIN-indexed, ranked by `ts_rank_cd`. The
`SearchRepository` interface is small enough that swapping in
OpenSearch / Meilisearch / Typesense later is a single new module.

### Public surface (Phase 2)

```
GET   /api/v1/search/events                  full-text + filters
POST  /api/v1/admin/search/reindex           admin-only full rebuild
```

Filters shipped in Phase 2: `q`, `operator`, `aircraft_type`,
`country`, `event_date_from`/`event_date_to`, `fatalities_min`/
`fatalities_max`, `confidence_bands`. Remaining filters from the
spec (phase of flight, occurrence category, source type,
investigation status) are deferred to later phases — they require
either Orion-relationship indexing or new projection fields.

### Pagination

Keyset over `(rank DESC, page_id DESC)`. The empty-query path
ranks by `extract(epoch from last_published_at)` so cursor shape is
uniform across text and no-text modes. `rank` is hidden from the
public response by default (debug-only flag) so the API contract
stays stable across ranking-algorithm changes.

### Reindex

`POST /api/v1/admin/search/reindex` walks every PUBLISHED page
and rebuilds the index atomically inside one UoW. Synchronous and
bounded at 50 000 pages — beyond that the right answer is a
resumable, batched, outbox-driven reindex, not a bigger ceiling.

---

## Audit (Phase 11)

A read-only explanation layer over existing evidence. Four endpoints,
no new tables, no new domain concepts — Phase 11 makes the existing
evidence chain legible to a journalist, family member, or
investigator who doesn't read SQL.

### Endpoints

```
GET   /api/v1/public/events/{slug}/audit                      page summary
GET   /api/v1/audit/events/{event_id}/fields/{field}/explanation   field deep-dive
GET   /api/v1/audit/claims/{claim_id}/explanation             claim role + history
GET   /api/v1/audit/sources/{snapshot_id}/verification        hash + recipe
```

All endpoints accept reader-and-above roles. Slug-keyed surfaces
live on the public router (Phase 1 visibility gates: 404 on DRAFT,
410 on RETRACTED). Event-id-keyed surfaces live under `/audit/`.

### Two-mode responses

Field explanation supports `?detail=expert` to add claim ids,
reliability tiers, and timestamps. The default `summary` mode omits
the expert block entirely so non-technical consumers never see it.
The contract is whitelist-by-construction: the Pydantic
`ExpertDetail` shape is the only carrier of internal-style fields,
and it appears as `null` in summary responses.

### Hash verification, public-safe

`GetSourceVerification` exposes the snapshot's `raw_payload_hash`
plus a versioned canonicalisation recipe (`v1-utf8-sha256-sorted-keys`).
The raw payload is never returned — verifiers re-fetch from the
original publisher and apply the recipe to confirm the hash. This
preserves source content rights while keeping the chain externally
auditable. A test pins that the response schema has no `payload`
key.

### Winner explanation reuses existing rules

Field-level audit calls the existing
`select_projected_claims_by_field` helper (the same one used by
provenance), then translates the `WinnerPolicy` priority into plain
English. There is no second winner algorithm to keep in sync — the
audit can't disagree with the rest of the system by construction.

### Field probing is bounded

A public caller can only ask about field names already present in
the projection. Unknown field names return 404, not an empty
response — the audit endpoint cannot be used to enumerate fields
the public projection deliberately omits.

---

## Tenants and Private Overlays (Phase 5)

Tenant-private data lives in **parallel tables**, not as a
``tenant_id`` column on existing public tables. This makes
accidental contamination of public projections impossible by
construction: a query against the public ``claims`` table cannot
return a tenant row because there are no tenant rows in that table.

### Tables added

- ``tenants`` — directory.
- ``tenant_memberships`` — user↔tenant role assignments.
- ``tenant_sources`` — tenant-private sources (unique by name within
  a tenant).
- ``tenant_ingestion_runs`` — tenant-side ingestion provenance.
- ``tenant_claims`` — tenant-private claims about public events.
  References ``accident_events.id`` (the public canonical identity)
  so tenant views are anchored to public ground truth.
- ``tenant_event_overlays`` — one row per (tenant, event), carrying
  tenant-private notes and a JSONB field bag.

The existing ``api_keys`` table gets two nullable columns —
``tenant_id`` and ``tenant_role`` — both governed by a "both null
or both non-null" CHECK constraint. A system-only key keeps both
NULL; a tenant key carries both.

### Three isolation layers

The most important contract in Phase 5 is that tenant data cannot
leak into public surfaces. Three independent layers enforce it:

1. **Auth gate** — ``require_tenant_membership(tenant_id)`` verifies
   the API key is bound to the path tenant_id, the tenant is active,
   and the role is allowed. 403 ``CROSS_TENANT_ACCESS`` /
   ``TENANT_INACTIVE`` / ``NOT_A_TENANT_API_KEY`` on failure.
2. **Use case** — every tenancy use case re-checks
   ``caller_tenant_id == path tenant_id``. Defence in depth: keeps
   the invariant intact when use cases are called directly (CLI,
   tests, workers).
3. **Repository** — every tenant repo method takes ``tenant_id`` as
   a required parameter and includes it in the WHERE clause. A test
   pinned by reflection over every tenant repo method asserts this
   signature contract.

The combination means a single missing check in any one layer does
not result in a leak — the other two still hold.

### Public/tenant API surfaces

```
GET   /api/v1/enterprise/tenants/{tenant_id}/events                  list overlaid events
GET   /api/v1/enterprise/tenants/{tenant_id}/events/{id}/overlay     read overlay + public context
PUT   /api/v1/enterprise/tenants/{tenant_id}/events/{id}/overlay     upsert overlay (OWNER/MEMBER)
POST  /api/v1/enterprise/tenants/{tenant_id}/sources                 register source (OWNER/MEMBER)
```

Public endpoints (``/public/*``, ``/search/*``, ``/audit/*``,
``/accidents``, ``/provenance``) get **zero** changes in Phase 5.
They cannot see tenant data because they query the public tables
exclusively.

### Out of scope for Phase 5

The vertical slice ships isolation correctness for read paths and
two write paths. Deferred:

- Full tenant ingestion pipeline (``POST /ingestions``) — needs
  FOQA/ASAP design (Phase 6).
- Tenant-scoped search.
- Tenant audit endpoint (analogous to ``/audit/events/...``).
- Versioned editorial workflow for overlays (analogous to Phase 9
  for public pages).

---

## Geospatial Maps (Phase 3)

A materialised geo-index over PUBLISHED public event pages, parallel
to the search index from Phase 2. Same isolation invariant: only
PUBLISHED, only pages with parseable coordinates, only public data.

### Endpoints

```
GET   /api/v1/maps/events             points inside a bounding box
GET   /api/v1/maps/events/cluster     grid-bucketed cluster cells
```

Both reader-gated. Filter parameters mirror ``/search/events``
(operator, aircraft_type, country, event_date range, fatalities
range, confidence_bands) so a user moving between map and search
doesn't relearn query semantics.

### Why a parallel index

Coordinates aren't a fixed column on ``projected_accident_records``
— they live in the JSONB ``fields`` blob under whatever name the
source used. The map index does the canonicalisation once: try
``latitude``/``longitude``, fall back to ``lat``/``lon``/``lng``,
validate bounds, drop the row if no coordinate is parseable. Pages
without coordinates publish normally; they just don't appear on the
map.

The index column is ``geography(Point, 4326)`` with a GiST index.
Bounding-box queries use ``ST_Intersects`` against
``ST_MakeEnvelope``. The PostGIS expressions are emitted via
``sqlalchemy.text()`` rather than GeoAlchemy2 to keep the build
surface small.

### Antimeridian crossing

A bounding box with ``west > east`` is the antimeridian-crossing
case (e.g. Sydney → Wake Island). The repository splits it into two
``ST_Intersects`` predicates (``[west, 180]`` and ``[-180, east]``)
OR'd together. The cluster grid math uses a shifted-longitude
expression so ``FLOOR((lng - west) / cell_w)`` stays monotonic
across the wrap.

### Cluster grid math

The cluster endpoint divides the bounding box into a uniform grid
of ``cluster_precision`` cells across the longitude span. Cell
height matches cell width by sharing the same precision, so cells
are square in degrees (they're not square in metres at non-equator
latitudes — acceptable for Phase 3; a future iteration could use
Web Mercator tile coordinates). Each cell carries:

- Its bounding box (``cell_west`` / ``south`` / ``east`` / ``north``).
- The centroid (``AVG(lng), AVG(lat)`` of the contained points), so
  a UI marker sits where the data is densest within the cell.
- The point ``count``.

Cells are ordered by count DESC and capped at 2000 per response.

### Lifecycle

The Phase 9 publication hooks own writes:

- ``PublishPublicEventPage._post_transition_hook`` calls both
  ``index_published_page`` (Phase 2) and
  ``index_published_page_in_map`` (Phase 3).
- ``ArchivePublicEventPage`` and ``RetractPublicEventPage`` call
  both removes.
- The admin ``POST /admin/search/reindex`` rebuilds both indices in
  one pass; the response carries ``map_pages_reindexed`` (the
  subset of ``pages_reindexed`` that had parseable coordinates).

### Fail-soft index philosophy

The indexer never blocks a publish. Invalid coordinates, missing
keys, out-of-range values — all yield a no-op. The page still
publishes; it just doesn't appear on the map. The defensive-delete
inside the upsert path means a page whose coordinates *become*
invalid between publishes is removed from the index automatically.

---

## CMS-Like Content (Phase 10)

Three content kinds — glossary terms, methodology pages, changelog
entries — all sharing the editorial workflow from Phase 9.

### Endpoints

**Public reads** (reader-gated, same visibility contract as Phase 1):

```
GET   /api/v1/public/glossary                     all PUBLISHED terms
GET   /api/v1/public/glossary/{term}              one term
GET   /api/v1/public/methodology                  PUBLISHED, grouped by section
GET   /api/v1/public/methodology/{slug}           one page
GET   /api/v1/public/changelog?limit&cursor       PUBLISHED, recency-paginated
GET   /api/v1/public/changelog/{slug}             one entry
```

**Editorial writes** (analyst+ for CRUD, reviewer+ for transitions,
admin-only for retract):

```
POST/PUT  /api/v1/editorial/glossary[/term_id]
POST      /api/v1/editorial/glossary/{term_id}/{transition}
POST/PUT  /api/v1/editorial/methodology[/page_id]
POST      /api/v1/editorial/methodology/{page_id}/{transition}
POST/PUT  /api/v1/editorial/changelog[/entry_id]
POST      /api/v1/editorial/changelog/{entry_id}/{transition}
```

Eight transitions per kind: submit, request-changes, approve,
reject, publish, archive, reopen, retract.

### Why three tables instead of one polymorphic table

Each kind has fields that don't apply to the others — glossary's
``term`` key, methodology's ``section`` + ``section_order``,
changelog's ``effective_date``. A single polymorphic table with
nullable columns would force every read path to filter by ``kind``
and would invite drift where a column added for one kind doesn't
apply to another. Three small tables, three small repos, one shared
state machine.

### Shared state machine

The workflow logic from Phase 9 (``PublicationStatus``,
``validate_transition``) is reused unchanged. A parallel
:class:`_CmsTransition` base class in
``atlas.application.use_cases.cms`` handles the load → validate →
mutate → update → revision → commit loop for all three kinds,
using a small :class:`_CmsContentSlot` protocol to abstract over
the repository operations. A signature-contract test pins that
every transition use case for every kind descends from
:class:`_CmsTransition`, so any future state-machine fix touches
one file.

### Why not refactor Phase 9 to share the base?

Phase 9's :class:`_TransitionUseCase` is tightly coupled to
:class:`PublicEventPage` (it carries the search-index lifecycle
hook, the map-index hook, the projection-aware payload). Lifting
that out into a shared base would either lose the
publication-specific hooks or push them into every CMS subclass.
The parallel implementation pays for the duplication by keeping
both surfaces clean — and the contract test ensures they cannot
disagree on the state machine.

### Effective date vs publication date (changelog)

Changelog carries two dates: ``effective_date`` (when the change
took place in the real world) and ``last_published_at`` (when the
entry was published to readers). A retroactive entry can describe
a change that took effect weeks earlier; both dates are correct,
both are surfaced, and the public list orders by
``effective_date DESC, id DESC``.

### Bounded vs unbounded listings

- **Glossary** is bounded (dozens to low hundreds of entries):
  ``ListPublicGlossary`` returns the full list, sorted by term.
- **Methodology** is bounded: returned pre-grouped by section, then
  by ``section_order``, then by title.
- **Changelog** is unbounded: keyset-paginated by
  ``(effective_date, id)``.

---

## FOQA / ASAP Tenant Ingestion (Phase 6)

Turns the Phase 5 tenant tables from "schema exists" into "data
flows". Operators can now open ingestion runs, submit batches of
FOQA exceedance claims, file ASAP-style narrative reports, and
explicitly associate either with public events.

### Endpoints (all under ``/api/v1/enterprise/tenants/{tenant_id}``)

```
POST   /ingestions                          open a new run
POST   /ingestions/{run_id}/claims          append claims (batch ≤ 1000)
POST   /ingestions/{run_id}/complete        SUCCEEDED or FAILED
POST   /safety-reports                      file an ASAP report
GET    /events/{event_id}/tenant-evidence   composite read
```

Write paths require OWNER or MEMBER role; READ_ONLY rejected.
Cross-tenant URLs return 403 (caller_tenant_id mismatch).

### FOQA vs ASAP distinction

- **FOQA** — machine-generated structured exceedance claims. Stored
  as ``TenantClaim`` rows with ``claim_kind=FOQA``. Volume can run
  to hundreds per batch; per-batch cap is 1000 to keep transaction
  sizes manageable.
- **ASAP** — narrative-heavy, identity-sensitive crew self-reports.
  Stored as ``TenantSafetyReport`` rows (separate table) because
  the row shape (narrative + attestation + scrubbing) is too
  different from the structured-claim shape to combine cleanly.

A FOQA claim may carry ``claim_kind=ASAP`` if an analyst extracted
a structured cue from a narrative report ("crew mentioned
fatigue"); the narrative itself stays in ``tenant_safety_reports``.

### Ingestion run state machine

```
        ┌──────────┐  succeed    ┌────────────┐
RUNNING │  RUNNING ├───────────► │ SUCCEEDED  │
        └────┬─────┘             └────────────┘
             │
             │ fail
             ▼
        ┌────────┐
        │ FAILED │
        └────────┘
```

One-way door. A non-RUNNING run rejects further appends (409
``TENANT_INGESTION_RUN_CLOSED``) and re-finalisation (same code).
Claims already in a FAILED run remain stored as audit trail; the
tenant evidence read shows them but the operator's analyst can
choose to filter on run status.

### Deidentification posture

Two-tier:

1. **Operator attestation is primary.** The submitter must set
   ``deidentified_attested=True`` or the report is refused (422
   ``DEIDENTIFICATION_REQUIRED``). This is the operator's signed
   record that their safety office has run their own
   deidentification process.

2. **Atlas's pattern scrubber is second line.** A conservative
   regex pass over the narrative redacts tail numbers, employee
   IDs, emails, phones, and flight numbers. The scrubbed text is
   what's stored — the raw narrative is never persisted. The
   substrings that were redacted are returned to the caller for
   the operator's own audit trail (Atlas does not store the audit
   list).

   This is **explicitly best-effort**. False positives are
   preferable to false negatives but no regex scrubber is a
   substitute for the operator's review. Anyone shipping this
   should pair it with a proper NER-based deidentifier.

A minimum 20-word floor applies *after* scrubbing — a narrative
made entirely of identifying details is not a safety report we
can usefully store.

### Public surface never reads safety reports

Hard rule, enforced at the routing layer:
``tenant_safety_reports`` and ``tenant_event_associations`` are
**never** read by any public-side route. The invariant is
co-enforced with the router import discipline (no public router
imports the use cases that read these tables). A CI-time grep test
could pin this; for now it's a code review concern.

### Event associations as a separate table

A FOQA exceedance or ASAP report can be explicitly associated with
a public event via ``tenant_event_associations`` (one row per
edge). Three editorial kinds:

- ``RELATED`` — weak claim, default.
- ``CONTRIBUTED_TO`` — analyst believes the evidence describes a
  contributing factor.
- ``PRECEDED`` — the evidence describes a leading indicator that
  preceded the event.

Why separate from claims/reports:

1. The same safety report can attach to multiple events (a
   fatigue ASAP report might bear on three approaches) or none
   (a general operational concern).
2. The association is itself editorial — an analyst made the
   connection — and deserves its own audit row independent of the
   underlying evidence.

The schema CHECK ``(claim_id IS NOT NULL)::int +
(safety_report_id IS NOT NULL)::int = 1`` enforces "exactly one
source"; the entity's ``model_post_init`` does the same at the
in-memory level for test paths.

### Three layers of isolation, unchanged

Inherited from Phase 5 without modification:

1. **Auth gate** (router-level): ``require_tenant_membership()``
   verifies the API key is tenant-bound and the path's tenant
   matches the key's tenant.
2. **Use-case check**: every Phase 6 use case asserts
   ``caller_tenant_id == path tenant_id`` even though layer 1
   already enforces it.
3. **Repository**: every method takes ``tenant_id`` as a required
   keyword and filters every WHERE clause on it. The
   ``add_many`` path verifies tenant_id per-row defensively.

### Carry-forward risks

- **PII scrubber is best-effort.** Documented in the module
  docstring and in this section. Operators retain primary
  responsibility for deidentification.
- **No real-time/streaming ingestion.** Batch only. A FOQA
  exporter that wants to ship 10k claims splits across 10 calls
  at the 1000-claim cap.
- **No cross-tenant aggregation.** Industry-wide anonymised FOQA
  insights need their own privacy thinking and are out of scope.
- **``tenant_safety_reports`` invariant is documented-not-
  enforced.** A future CI test should grep for any public router
  importing the safety-report use cases.
- **Run.status is stored as raw string** for Phase 5
  backwards-compat. Phase 6's enum coerces to the same string
  value; everywhere we compare, we use the ``.value`` form.

---

## Audit explanations (Phase 11)

The audit layer is the project's identity statement made legible:

> "The projected public record is not the truth. The evidence chain
> is the truth. The projection is the current best explanation of
> that evidence."

A non-technical reader — journalist, investigator, family member —
should be able to read a page, ask *why does it say this*, and get
a plain-English answer that points at the actual evidence.

### Invariants

- **No new sources of truth.** Phase 11 added zero tables and zero
  schema changes. Every value rendered comes from existing rows
  (`ProjectedAccidentRecord`, `Claim`, `ClaimConflict`, `ClaimHistory`,
  `Source`, `RawSnapshot`).
- **No re-implementation of `WinnerPolicy`.** The audit reads use the
  existing `select_projected_claims_by_field` helper that Provenance
  already uses, so "what the system says won" and "what the audit
  endpoint explains" stay aligned by construction.
- **Field-locked field names.** Public callers can only ask about
  field names that appear in the current projection. Probing for
  fields that aren't in the projection returns 404, not a half-shaped
  response — the endpoint cannot be used as a schema discovery tool.
- **Hash verification stays public-safe.** Source verification
  surfaces `raw_payload_hash` plus a canonicalization recipe; it
  never returns the raw payload. Verifiers fetch the source directly
  from the publisher and apply the recipe.

### Audit surface

```
GET  /api/v1/public/events/{slug}/audit
       — page-level overview: which fields are disputed, which were
         set by an editor, overall confidence with plain-English
         meaning

GET  /api/v1/audit/events/{event_id}/fields/{field_name}/explanation
       ?detail={summary,expert}
       — per-field deep audit: winner + why it won, losing/superseded
         claims + why each lost, current conflict status

GET  /api/v1/audit/claims/{claim_id}/explanation
       — per-claim role: winning, active, superseded; source + history

GET  /api/v1/audit/sources/{snapshot_id}/verification
       — hash + canonicalization recipe for independent verification
```

All audit endpoints are reader-gated. They expose evidence that is
already public via Provenance — Phase 11 just makes the existing
evidence chain legible to non-engineers.

### Two response modes

The field-explanation endpoint exposes a `?detail=summary` (default)
and `?detail=expert` mode:

- **summary**: non-technical prose plus the minimum structured
  fields a UI needs to render. This is the contract for
  general-audience views.
- **expert**: adds claim ids, source reliability tiers, and exact
  timestamps. Reserved for index-tuning / investigative audit
  consumers.

The expert block is omitted entirely in summary mode, so changing
the expert shape later cannot break the non-technical contract.

### Verification recipe

`SOURCE_VERIFICATION_RECIPE_VERSION = "v1-utf8-sha256-sorted-keys"`.
Recipe: fetch the source payload directly from the publisher; parse
as JSON; re-serialise with sorted keys, no extra whitespace, UTF-8
encoding; SHA-256; compare against `raw_payload_hash`. The version
tag is a forward-compatibility hook: a future recipe version (CSV,
HTML normalisation, ...) bumps the tag so a saved verification
response is unambiguous.

---

## Causality: HFACS + SHELO (Phase 4)

Structured causal claims about an event. Two parallel sub-models
sharing one bounded context because they live in the same
conceptual space ("why did this accident happen?") even though
their shapes are independent.

### Endpoints

**Public reads** (reader-gated, visibility inherited from
``PublicEventPage``):

```
GET   /api/v1/public/hfacs/taxonomy                 reference data
GET   /api/v1/public/events/{slug}/hfacs            event attributions
GET   /api/v1/public/events/{slug}/shelo            event factor graph
```

**Editorial writes** (reviewer+):

```
POST    /editorial/events/{event_id}/hfacs                       attach
PUT     /editorial/events/{event_id}/hfacs/{attribution_id}      update
DELETE  /editorial/events/{event_id}/hfacs/{attribution_id}      remove
POST    /editorial/events/{event_id}/shelo/factors               attach factor
PUT     /editorial/events/{event_id}/shelo/factors/{factor_id}   update
DELETE  /editorial/events/{event_id}/shelo/factors/{factor_id}   remove
POST    /editorial/events/{event_id}/shelo/interactions          attach edge
DELETE  /editorial/events/{event_id}/shelo/interactions/{id}     remove
```

### HFACS taxonomy as seed data

The HFACS taxonomy is fixed reference data — the four-tier
hierarchy and the canonical category set come from the public
domain HFACS specification. Migration 042 inserts 19 canonical
categories via a data migration (rows live alongside the schema in
the same revision so they version together). Subcategories are
empty by default; operators populate the ones they need.

### HFACS attributions

An attribution is the editorial claim that "this event manifested
this HFACS subcategory" — editorial judgement, not data derivation.
Each carries ``category_id``, optional ``subcategory_id``,
``confidence`` (0..1), ``note``, ``editor_user_id``, and
``version`` for optimistic concurrency.

**Natural key**: ``(event_id, category_id, COALESCE(subcategory_id,
sentinel))`` via partial unique index. Two category-only
attributions for the same category collide; the same category with
different subcategories is allowed — they're distinct claims.

### SHELO factors and interactions

SHELO (Software, Hardware, Environment, Liveware, Other) classifies
contributory factors and the **interactions** between them. Each
event has its own small graph:

- **Nodes** are ``SheloFactor`` rows: ``factor_class`` + free-form
  ``label`` + optional ``description``.
- **Edges** are ``SheloFactorInteraction`` rows: typed source→target
  with one of four kinds (``PRECONDITION``, ``AGGRAVATED``,
  ``MITIGATED``, ``MASKED``).

**Cycles are permitted** at the schema, entity, and use-case
levels. Real causal models sometimes contain mutual feedback loops
(A aggravated B which masked A's detectability). The editorial
workflow surfaces cycles to reviewers rather than rejecting at
INSERT.

**Self-loops are rejected three times**: schema CHECK, entity
``model_validator``, and use-case explicit check.

**Natural key for edges**: ``(event_id, source, target, kind)``.
The same source→target pair can carry multiple edges of different
kinds — they're distinct editorial claims.

### Visibility inherits from PublicEventPage

Phase 4 deliberately does not introduce its own publication state
machine. Visibility is determined by the parent page:

- PUBLISHED parent → 200 with the causal data.
- RETRACTED parent → 410 (reuses Phase 1's
  ``PublicEventPageRetractedError``).
- Anything else → 404 (reuses Phase 1's
  ``PublicEventPageNotPublishedError``).

The Phase 1 exception handlers already cover both HTTP responses
without modification. Causality-specific error types are 404 / 409
/ 422 only.

Editorial **writes** ignore the parent's status — analysts can
attach attributions to a DRAFT event so the analysis is ready by
the time the page reaches PUBLISHED.

### Carry-forward risks

1. **HFACS subcategories are populated by SQL, not API.** Phase 4
   ships the schema and the read path; populating subcategories
   requires direct SQL or a follow-up admin surface.

2. **No causal inference engine.** All attributions and factors
   are entered manually by analysts. Auto-derivation from
   narratives is a future ML concern.

3. **No cross-event causal aggregation.** "Show me all events
   where supervision failures contributed" needs its own indexing
   strategy.

4. **No tenant-private causal claims.** Extending Phase 6's tenant
   evidence surface to HFACS is symmetric and obvious, but deferred.

5. **SHELO factors are event-local.** Cross-event factor identity
   (fleet-wide aggregation) needs its own modelling.

---

## Natural-Language Search (Phase 7)

A deterministic NL parser layered over Phase 2 FTS, Phase 3
spatial, and Phase 4 HFACS. Free-text queries become structured
filters which dispatch into existing search infrastructure. No
new index — Phase 7 is an orchestrator.

### Endpoints

```
POST    /api/v1/search/nl                       execute query
POST    /api/v1/search/nl/saved                 pin a saved query
GET     /api/v1/search/nl/saved                 list saved queries
DELETE  /api/v1/search/nl/saved/{saved_id}      remove saved query
```

All endpoints reader-gated (ADMIN/REVIEWER/ANALYST). Saved queries
are per-user; cross-user delete returns 404 to avoid leaking
existence of other users' queries.

### Deterministic, rule-based parser

Phase 7 ships a stdlib-only regex/keyword parser, **not** an LLM
call. Three reasons:

1. No model dependency in production. Determinism makes debugging
   tractable.
2. The parser's output shape is what an LLM-routed pipeline would
   also produce — so swapping to LLM parsing in a future Phase 7.5
   doesn't change any downstream surface.
3. Latency: NL search responds in milliseconds, not seconds.

The parser runs six passes, each consuming substrings:

1. **Date phrases** — years (`2023`), ranges (`between 2015 and
   2020`), comparisons (`before 2020`, `after 2018`), month ranges
   (`Jan-Mar 2024`).
2. **Fatality predicates** — `fatal`, `non-fatal`, `more than
   100 fatalities`, `fewer than 10 deaths`.
3. **Aircraft aliases** — small curated alias table (`737` →
   `Boeing 737`); longest alias wins.
4. **Operator aliases** — same shape; `delta` → `Delta Air Lines`.
5. **HFACS category names** — matched against the live taxonomy
   loaded from `hfacs_categories`.
6. **SHELO factor keywords** — `software`, `pilot`, `weather`,
   etc., grouped by SHELO class.

After all six passes, the **free-text remainder** (characters
not claimed by any pass) is passed to Phase 2 FTS for keyword
coverage on whatever the parser didn't recognise.

### Confidence echo

The response carries a structured `parsed: ParsedQuery` object
showing what each pass extracted, plus a `confidence` score: the
fraction of *significant* tokens (non-stop-word, non-empty) that
the parser claimed. Stop words like `the`, `in`, `show`, `me` are
excluded from the denominator so a query like *"show me the 737
in 2023"* gets full confidence (every significant token was
matched) rather than being penalised for noise.

The confidence echo is the editorial-honesty signal: low confidence
tells the user the system didn't understand much, and they can
refine. Mirrors the Phase 11 audit-explanation philosophy ("the
system explains itself").

### Query log is anonymous by design

The `nl_query_log` table records every NL call with raw text,
parsed filters, result count, parser confidence, and an
hour-bucketed timestamp — but **no `user_id` column**. This is a
deliberate privacy choice.

Analysts running NL queries sometimes describe sensitive
operational concerns ("crew fatigue on overnight cargo runs to
[carrier name]"); aggregate query patterns can inform parser
improvements and an eventual embeddings-based replacement without
exposing individual analyst behaviour. The table is designed to be
safe to share with external researchers if asked.

The `query_hash` (SHA256 of lowercased raw text) lets analytics
group repeats without re-hashing on read.

### Saved queries freeze their filters

When a user saves an NL query, both the original raw text and the
parser's structured output are stored. Re-running uses the **frozen
filters**, not a fresh parse — so behaviour is stable even if the
parser's behaviour drifts in a future revision.

A user can save many queries with the same label (no uniqueness
constraint) while iterating. Saved queries are per-user and never
shared; cross-user reads aren't exposed.

### HFACS intersection at the orchestrator layer

When the parser identifies HFACS category mentions, the
orchestrator filters Phase 2's result set down to events with a
matching attribution. This is done as a post-filter against the
already-bounded result page rather than a pushed-down SQL join
because:

1. Phase 2's search index isn't joined to attributions; adding
   that join would require a new index.
2. The Phase 2 result page is small (default 25, max 100);
   post-filtering is cheap.
3. For deeper queries (1000s of results), pushing this down to
   SQL is the right Phase 7.5 work.

A `SearchHit` carries `page_id`; the orchestrator resolves it
through `public_event_pages.get_by_id` to find the underlying
`event_id` for the attribution lookup. Hits whose page has been
retracted or deleted are silently dropped (no `event_id` to look
up).

### Translation of fatal_only into fatalities_min

The parser produces `fatal_only` and `non_fatal_only` booleans for
human-friendly query phrasing. The orchestrator translates these
into Phase 2's `fatalities_min` / `fatalities_max` filters:

- `fatal_only=True` → `fatalities_min=1` (if not already set)
- `non_fatal_only=True` → `fatalities_max=0` (if not already set)

Explicit `more than 100 fatalities` from the parser takes
precedence — the conversion only fills in defaults when the
caller didn't specify a range explicitly.

### Extension seam for Phase 7.5

`index_for_embeddings()` exists as a no-op stub in
`atlas.application.services.nl_query_parser`. A future Phase 7.5
can swap the deterministic passes for an embedding similarity
search against a vector store without changing the
`parse_nl_query` signature or the orchestrator's call site.

### Carry-forward risks

1. **No semantic similarity.** "engine problem" and "engine
   failure" are independent strings to the parser. Embedding-based
   parsing in 7.5 is the obvious next step.
2. **Alias tables are hand-coded and small.** Phase 7 ships a
   minimal curated set; expanding the aircraft and operator
   aliases is mechanical but ongoing. A future iteration could
   generate them from the projected accident corpus.
3. **HFACS intersection isn't pushed to SQL.** Acceptable for
   25-result pages; deferred for 1000-result analytics queries.
4. **No multi-turn refinement.** Each query stands alone. A
   conversational refinement loop ("now narrow to just 2023") is
   a future product concern, not a Phase 7 commitment.
5. **NL over tenant data is out of scope.** The privacy design
   for cross-cutting NL across tenant + public is its own work.
   Phase 7 is public-only.
6. **No typo fuzzy matching.** The parser is case-insensitive but
   doesn't fuzzy-match `boing` against `boeing`. Editorial-honesty
   choice: false-positive fuzzy matches confuse more than they
   help.
7. **Parser confidence is a heuristic, not a calibrated
   probability.** A query at 0.8 confidence isn't 80% likely to
   give the right answer — it's "80% of significant tokens
   matched". Documented explicitly so callers don't over-interpret.

---

## Metering (Phase 8)

Turns the write-side actions of earlier phases into measurable,
billable units. Phase 8 ships *units*, not money — pricing and
invoicing live in the operator's external billing system.

### Endpoints

```
GET   /api/v1/enterprise/tenants/{tenant_id}/usage   tenant reads own usage
GET   /api/v1/admin/usage/summary                    operator-wide breakdown
POST  /api/v1/admin/usage/rollups                    trigger rollup computation
```

Tenant usage is member-gated and tenant-isolated (three-layer
rule). Admin endpoints are ADMIN-only.

### Two-table model: events + rollups

- **`usage_events`** — one immutable row per metered action.
  Append-only audit trail. Carries the metric kind, optional
  tenant id, optional user id, an optional resource id (the
  event/claim/report the action operated on, denormalised with no
  FK so the meter survives downstream deletes), and a timestamp.
- **`usage_daily_rollups`** — per-tenant, per-day, per-metric
  aggregates keyed on `(tenant_id, metric_kind, day)`. Billing
  queries hit the rollup (O(days)); audit queries hit the events.

Same trade-off as Phase 11's audit hash chain over Phase 9's
revision events: an immutable fine-grained log plus a queryable
aggregate.

### The metric catalogue is closed

Five metered actions: `TENANT_CLAIM_INGESTED`,
`TENANT_REPORT_FILED`, `TENANT_INGESTION_RUN_COMPLETED`,
`NL_QUERY_EXECUTED`, `HFACS_ATTRIBUTION_CREATED`. The `metric_kind`
is a string column (not a Postgres enum type) pinned by a CHECK
constraint, so adding a new metric in a future phase is:

1. A new `MetricKind` enum value.
2. A migration updating the CHECK on both tables.
3. A one-line `MeteringService.record(...)` call at the action's
   use-case site.

### Service-layer call sites

Each metered action's use case calls `MeteringService.record()`
right before `await uow.commit()`, so the action and its meter
land in the same transaction — either both commit or neither does.
`record(quantity=N)` emits N rows, keeping per-item granularity in
the audit trail while letting batch use cases stay terse (one call
records a whole claims batch). The five wired sites span Phases 4,
6, and 7.

### Tenant-scoped vs system-wide metrics

- **Tenant-scoped** (`TENANT_CLAIM_INGESTED`, `TENANT_REPORT_FILED`,
  `TENANT_INGESTION_RUN_COMPLETED`) carry a real tenant id.
- **System-wide** (`NL_QUERY_EXECUTED`, `HFACS_ATTRIBUTION_CREATED`)
  are public-corpus / editorial actions with no tenant. On
  `usage_events` their `tenant_id` is NULL; in `usage_daily_rollups`
  they roll up under a sentinel UUID
  (`00000000-0000-0000-0000-000000000000`) so the natural-key
  unique constraint works without a partial index. The admin
  summary maps the sentinel back to `tenant_id=None` on the way
  out, so consumers never see the sentinel.

### Rollup computation is a use case, not a trigger

`ComputeDailyRollups` takes a date range and an optional tenant
filter, counts matching events per `(tenant, metric, day)` cell,
and UPSERTs into `usage_daily_rollups`. Operators schedule it
however suits their billing cadence (nightly cron, hourly, manual
reconciliation) — the `POST /admin/usage/rollups` endpoint exposes
it for whatever scheduler the operator uses.

Key properties:

- **Idempotent.** Re-running for the same day replaces the count
  via `ON CONFLICT DO UPDATE`; never double-counts.
- **Tenant enumeration from events, not a directory.** The
  `TenantRepository` deliberately doesn't expose enumeration, so
  the rollup computer derives the tenant set per day from
  `distinct_tenants_in_range`. A tenant only gets rollup rows for
  days it had activity — "no usage" is distinguishable from
  "rollup hasn't run yet" because a day with events writes
  zero-count rows for its metrics, whereas a day never rolled up
  has no rows at all.
- **Inclusive-start, exclusive-end day bounds** so an event at
  exactly midnight belongs to the day that's starting.

### Carry-forward risks

1. **Units, not money.** Phase 8 emits counts; multiplying by
   per-unit price and issuing invoices is the operator's external
   billing system (Stripe, Recurly, etc.). Atlas exports
   counts via the API; it doesn't price them.
2. **No real-time quota enforcement.** The metering surface is
   read-only counts. "You've hit your 10k claims this month" gates
   are a Phase 8.5 / policy concern that would read the rollups
   and reject at the action's use case.
3. **Public-corpus reads aren't metered.** Phase 8 measures
   tenant-private writes and editorial/NL actions. Metering
   anonymous public reads is a different conversation about
   anonymous-user billing.
4. **Rollup is full-recompute per cell, not incremental.** For a
   day with millions of events this counts per (tenant, metric)
   with a SQL `COUNT`. Fine at current scale; a high-volume
   operator might want an incremental or streaming rollup.
5. **No cross-tenant benchmarking.** Same privacy concerns as the
   Phase 6 deferred items; aggregate industry usage stats need
   their own k-anonymity design.

---

## Live-Postgres validation (Phases 4-8)

The Phase 4-8 SQL repositories and use cases have been validated
against a real PostgreSQL 16 + PostGIS instance, not just the
in-memory fakes. The full migration chain (001 -> 044) applies
cleanly, and `tests/integration/test_phase4_8_live.py` drives the
real async UnitOfWork to confirm:

- the HFACS 19-row taxonomy seed loads;
- the `event_hfacs_attributions` partial-unique index (with the
  `COALESCE(subcategory_id, sentinel)` expression) actually rejects
  duplicate category-only attributions at the database level;
- the SHELO interaction natural-key constraint holds, and distinct
  interaction kinds on the same edge coexist;
- `saved_nl_queries` round-trips JSONB filter dicts (including
  nested lists) faithfully, and cross-user delete returns False;
- the metering rollup `ON CONFLICT DO UPDATE` is idempotent when
  driven through the real `ComputeDailyRollups` use case;
- the bulk `add_many` path persists every row;
- the admin summary maps the no-tenant sentinel UUID back to None.

### Migration 012 JSONB-default fix

Validating the chain against a real database surfaced a latent bug
no fake could catch: migration 012 declared
`server_default="'[]'::jsonb"` as a plain Python string, which
SQLAlchemy emitted as a quoted literal
(`DEFAULT '''[]''::jsonb'`), and Postgres rejected as invalid JSON.
The fix wraps it in `sa.text("'[]'::jsonb")` so the default is
emitted as a raw SQL expression. This blocked the *entire*
migration chain — meaning every prior test run had used
`create_all` or fakes, never the migrations. All migrations were
scanned for the same antipattern; 012 was the only instance.

### Integration-harness fixes

Two pre-existing harness defects were fixed while validating:

1. The `test_engine` / `test_session_factory` fixtures were
   session-scoped, but `pytest-asyncio` (auto mode) uses a
   per-function event loop. asyncpg connections are loop-bound, so
   a session-scoped engine stranded its pooled connection on the
   first test's loop and raised `InterfaceError` on the next test's
   reset. The fixtures are now function-scoped and dispose their
   engine in a `finally` (also closing a connection leak).
2. The `_reset_db` TRUNCATE list predated Phases 4-8 and omitted
   their tables, so metering/causality/NL data accumulated across
   tests in a run. The Phase 4-8 tables are now truncated (the
   HFACS taxonomy seed and the per-test tenants are deliberately
   left intact).

The older pre-Phase-4 integration tests (Hermes/Orion/merge/
conflict) still fail on a `claim_history` FK ordering issue in
their own ingestion-path seeding; that is a separate, pre-existing
concern untouched by this work.

---

### Insert-ordering and the missing `relationship()` declarations

Live-DB validation surfaced a **systemic** correctness bug that no
fake could catch. The ORM layer declares **zero SQLAlchemy
`relationship()` definitions** — every association is a bare
`ForeignKey` column. This is a deliberate design choice (the model
is append-heavy and avoids lazy-load surprises), but it has a sharp
consequence: **SQLAlchemy builds its flush insert-ordering graph
from `relationship()` definitions, not from `ForeignKey` column
metadata.** With no relationships, SQLAlchemy does not reliably
order a parent insert before a child insert in the same flush.

A minimal reproduction — `session.add(parent)`, `session.add(child)`
(child FK→parent), `commit()` — fails against real Postgres with a
foreign-key violation, because SQLAlchemy emits the child INSERT
first. This affected the core ingestion path (claim →
claim_history), the merge and conflict-resolution paths (new claim →
history), and the Hermes seed path (source → crawl_target). It was
invisible for the project's entire history because all prior tests
used in-memory fakes (no FK enforcement) or `create_all` flows that
happened to flush in a benign order.

**Fix.** An explicit `await uow.flush()` between adding a parent and
adding a child that references it, forcing the parent row to
materialize first. A `flush()` method was added to the `UnitOfWork`
protocol (and its SQL and fake implementations) as a first-class
sibling of `commit`/`rollback`: it means "materialize pending
writes without ending the transaction," and a later `rollback`
still undoes everything. The fix was applied at the confirmed
parent→child boundaries in `_claim_writer.py`,
`merge_duplicate_events.py`, and `resolve_conflict.py`.

**Carry-forward risk.** Because the root cause is architectural
(no relationships), *any* future code that inserts a parent and an
FK-child in the same flush is a latent FK-ordering bug. Two durable
options exist: (a) introduce `relationship()` definitions so
SQLAlchemy orders inserts automatically (correct, but large blast
radius against a deliberately relationship-free schema), or (b) keep
the explicit-flush discipline and enforce it with review plus the
source-level guard tests in `test_phase4_8_invariants.py`. The
current code takes (b). A broader audit of every same-flush
parent→child insert across all phases is recommended before
production.

---

## Environment variables

See `.env.example` for a complete annotated reference.
