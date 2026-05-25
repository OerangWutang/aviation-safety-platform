# Echo: Private-Hazard ↔ Public-Precedent Cross-Reference Engine

## What it does

Echo answers one question, defensibly: *"Which public accidents resemble this
private hazard report, and exactly why?"*

An operator submits a hazard — a FOQA exceedance, an ASAP narrative, a safety
observation. Echo searches the public investigative record (NTSB, and later
ASN) for structurally analogous accidents and surfaces them with an explainable
justification. The operator gets proactive risk intelligence — "this kind of
exceedance has preceded 6 accidents of this type" — grounded in real
investigations, not model speculation.

## Where it sits

Echo is the third gap from the review session, built after:

1. **NTSB importer** (gap 2) — populates the public precedent corpus.  
2. **Tenant RLS** (gap 1) — enforces the private/public boundary structurally.

That ordering was deliberate: Echo is the one feature that deliberately crosses
the boundary, so it had to sit on top of a boundary the database enforces, not
one we remembered to filter.

---

## Files

| Layer | Path | Responsibility |
|---|---|---|
| domain entities | `domain/crossref/entities.py` | `HazardProfile`, `PrecedentRecord`, `PrecedentMatch`, `EvidenceSupport`, `MatchComponent` |
| domain profile | `domain/crossref/profile.py` | Build `HazardProfile` from structured inputs + scrubbed text |
| domain service | `domain/services/echo_matcher.py` | Deterministic scorer + `PrecedentRanker` seam |
| application wiring | `application/crossref/__init__.py` | Bridge NTSB claims → `PrecedentRecord`; orchestrate matching |
| tests | `tests/domain/test_echo_crossref.py` | 13 unit tests |

---

## Design

### Epistemic stance (load-bearing, not decoration)

Echo produces **precedent support**, never a probability of recurrence. This is
not a disclaimer — it changes what fields are allowed to exist.

- `PrecedentMatch` has no `probability` field and never will.
- The `score` in `[0, 1]` is a *similarity* measure, banded into a coarse
  `EvidenceSupport` label (`STRONG` / `MODERATE` / `WEAK` / `NONE`), explicitly
  mirroring the `confidence_band` pattern the rest of the system already uses.
- Every match carries a `components` tuple so an analyst can audit *why* it
  surfaced, not just accept a number.

The design note in `entities.py` is the machine-checkable form: the test
`test_match_has_no_probability_and_bounded_scores` guards that no one adds a
numeric probability field to `PrecedentMatch` without going through the
definition of what that field would actually mean.

### The boundary is one-way by construction

A `HazardProfile` is a **reduced, derived** representation of a private report:
normalised taxonomy keys, structured attributes, and lexical tokens extracted
from a *scrubbed* narrative. It deliberately has no `narrative` field. Private
text is reduced to tokens *before* it reaches the matching core; the matching
core can never emit private text into a result, a shared index, or a log.

`PrecedentRecord` is built from public claim fields only. The matching runs
entirely on public data crossed with derived private signal. Nothing private
is ever written to the public side.

When wired to the database (the DB-backed use case is the documented next step):
- Private hazard reads go through `create_tenant_uow(tenant_id)` — RLS-enforced,
  per-tenant.
- Public corpus reads go through the public read model — no tenant context.
- Cross-reference results are persisted into a **tenant-private** table — RLS
  isolates them like any other tenant payload data.

### Scoring: transparent, weighted, renormalising

Three components, each with an explicit weight:

| Component | Default weight | Method | Justification |
|---|---|---|---|
| `finding_categories` | 0.50 | Jaccard over NTSB `CC.SS` taxonomy keys | The defensible spine; the Board's own coded causal categories |
| `attributes` | 0.20 | Agreement over asserted structured attrs (FAR part, aircraft category, severity) | Filters to operationally relevant fleet/rule segments |
| `lexical` | 0.30 | Overlap coefficient over scrubbed narrative tokens | Catches scenario similarity not yet coded into taxonomy |

Only components for which the **profile** has data contribute; the weights are
renormalised over those present. A hazard with no coded categories is matched
on attributes + text rather than penalised for missing taxonomy. That means the
engine degrades gracefully as profile completeness decreases.

Blended score bands:

| Score | Band |
|---|---|
| ≥ 0.60 | `STRONG` |
| ≥ 0.35 | `MODERATE` |
| ≥ 0.15 | `WEAK` |
| < 0.15 | `NONE` |

### Verified on real data

Cross-referenced a realistic Part 91 crosswind landing hazard against 12,000
real NTSB events from `avall.mdb`:

- Top result: STRONG (score 0.88) — Piper PA-28-181, Mesa AZ 2008. *"Student
  pilot's inadequate recovery from a bounced landing and failure to maintain
  directional control."* Shared: both coded cause categories, both structured
  attributes, 8 of 13 hazard terms.
- Ranked 12k precedents in 0.20s, single-threaded Python.
- All five top results were genuine directional-control/crosswind accidents.

---

## Extension seams

These are documented here because they're the obvious next questions, and the
seam is already present in the code.

**Semantic re-ranker.** `PrecedentRanker` is a `Protocol` in `echo_matcher.py`:

```python
class PrecedentRanker(Protocol):
    def rank(self, profile: HazardProfile, records: Iterable[PrecedentRecord],
             *, limit: int) -> list[PrecedentMatch]: ...
```

A future `EmbeddingRanker` (pgvector on the public probable-cause narratives)
can slot in here as a second-pass re-ranker on the structured matcher's
candidates, without changing any caller.

**Category inference from narrative.** `build_hazard_profile` takes
`finding_categories` as a caller-supplied iterable. When the analyst hasn't
coded categories, a future enricher — Orion entity extraction, or a classifier
behind the existing model seam — can populate them before profile construction.
The matching core doesn't care where the keys came from.

**ASN as a second source.** The NTSB importer established the pattern (pure
mapping core, reader, CLI). An ASN importer would register at a lower
reliability tier (say, 2), emit claims in the same canonical vocabulary, and its
events would immediately flow into the precedent corpus. The matcher scores
against all sources; the tier distinction lives in the existing winner policy,
not in Echo.

**DB-backed use case.** The pure `cross_reference()` function in
`application/crossref/__init__.py` takes an already-loaded corpus. The database
wrapper is the next build:

```python
# sketch — the full use case is documented here, not yet implemented
async def run_cross_reference(
    tenant_id: UUID, hazard_report_id: UUID, *, limit: int = 20
) -> list[PrecedentMatch]:
    async with create_tenant_uow(tenant_id) as uow:
        report = await uow.tenant_safety_reports.get(hazard_report_id)
        # deidentification service already scrubbed the narrative on ingest
        profile = build_hazard_profile(
            scrubbed_narrative=report.narrative_markdown,
            # analyst-supplied structured fields via TenantClaim
            ...
        )
    # public corpus — no tenant context; system / BYPASSRLS connection
    corpus = await load_precedent_corpus()
    matches = cross_reference(profile, corpus)
    # persist into tenant-private results table (next migration)
    async with create_tenant_uow(tenant_id) as uow:
        await uow.crossref_results.replace(hazard_report_id, matches)
    return matches
```

---

## Known v1 limitations

1. **In-memory corpus.** 12k–30k records load in ~8s; acceptable for a batch
   job or a warmed-up process, not for a synchronous request. The precedent
   index should be materialised (pgvector / a pre-built inverted index on finding
   categories) so matching runs against a persistent index rather than a live
   scan.
2. **Overlap coefficient for lexical.** Works well when profile vocabulary is
   small (a scrubbed hazard narrative). Recall degrades against very short public
   narratives. Bigrams or TF-IDF weighting are the natural v1.1 improvement.
3. **No structured result persistence.** `cross_reference()` returns a list of
   `PrecedentMatch`; there is no tenant-private table yet to store results,
   no API router to serve them, and no Argus signal that fires when a strong
   match emerges.  Those are the next three builds in sequence.
