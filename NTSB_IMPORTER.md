# NTSB eADMS Importer

Bulk importer that loads the NTSB public accident database (`avall.mdb`, the
eADMS schema) into Atlas through the **existing** `IngestSourceData` use case.
It adds no new ingestion path — every NTSB accident enters as an ordinary
source submission (raw payload + field-level claims + stable `source_record_id`),
so conflict reconciliation, the winner policy, projections, search, maps and
causality all work on NTSB data with zero downstream changes.

## Where it lives

| Layer | File | Responsibility |
|------|------|----------------|
| application (pure) | `application/ingestion/sources/ntsb_eadms.py` | eADMS row → `IngestionClaimDTO` submission. No I/O. |
| application (pkg) | `application/ingestion/sources/__init__.py` | Public surface for source importers. |
| infrastructure | `infrastructure/ingestion/ntsb_eadms_reader.py` | `avall.mdb`→CSV export; CSV parse + join; record stream. Only Access-touching code. |
| presentation | `presentation/cli/ntsb.py` | `export` / `dry-run` / `load` CLI. DB imports deferred to `load`. |
| tests | `tests/application/ingestion/test_ntsb_eadms.py` | 10 unit tests on the mapping core. |

The split is deliberate: the mapping core is pure and unit-tested without a
database or Access driver, and is reusable as the template for the next bulk
source (ASN).

## How to run

```bash
# 1. Export the needed tables out of avall.mdb (one-time; needs mdbtools).
python -m atlas.presentation.cli.ntsb export --mdb avall.mdb --out ./ntsb_csv

# 2. Verify the mapping with no database — writes JSONL submissions.
python -m atlas.presentation.cli.ntsb dry-run --csv ./ntsb_csv --out subs.jsonl --limit 100

# 3. Load into Atlas via IngestSourceData (needs the configured DB).
python -m atlas.presentation.cli.ntsb load --csv ./ntsb_csv
```

`load` resolves-or-creates a single `Source` named **"NTSB eADMS (avall)"**,
`kind=EXTERNAL`, `reliability_tier=1` (NTSB final reports are the authoritative
US record; lower tier = more trusted in `WinnerPolicy`), with the canonical
field map stored verbatim as its `field_mapping_json` for provenance.

## Canonical vocabulary

Atlas is field-name-agnostic below the source boundary, so the importer defines
the canonical names the rest of the system sees. Each is documented in
`NTSB_FIELD_MAPPING` (raw eADMS column → canonical field), e.g. `acft_make →
aircraft_make`, `ev_date → occurred_on`, `narr_cause → probable_cause_narrative`.
Coded eADMS values (`DEST`, `091`, …) are decoded to human-readable text
(`Destroyed`, `Part 91: General Aviation`) using the `eADMSPUB_DataDictionary`
table that ships **inside** `avall.mdb` — no external code list, and the raw
code is always preserved in the payload.

## Epistemic framing (deliberate)

NTSB probable-cause narratives and coded findings are the **Board's official
determinations**, not inferences. They are emitted as authoritative claims and
carry **no synthetic probability or weight**. The `causal_findings` claim is a
structured list; each item records only the finding code, description, and the
Board's recorded **role** (`CAUSE` / `FACTOR` / `UNSPECIFIED`). This keeps NTSB
ground truth cleanly separable from any future AI-derived cross-reference,
which must be framed as *evidence support*, never as causal probability.

## Idempotency & resumability

`source_record_id = ev_id` (the NTSB accession number). The idempotency key is
`ntsb-eadms:{ev_id}:{sha256(content)[:16]}` — content-addressed over the
record's claims and raw bodies, **excluding** capture time. Therefore:

- Re-running with **identical** NTSB content → same key → the use case replays
  the stored result without writing new state.
- An NTSB record whose data **changed** → new key → a new submission that still
  attaches to the original event via the shared `source_record_id` (updated
  claims layer in; no duplicate event).

`load` commits **one unit of work per accident**, so a mid-run failure neither
rolls back prior progress nor blocks a resume — just re-run.

## Verified against the real dataset

Run over the full public `avall.mdb` (30,516 events):

- 30,516 records mapped in ~10 s (~3,000 rec/s), single-threaded.
- 0 duplicate-field-name submissions (would otherwise be rejected by ingestion).
- 17,262 records carry ≥1 coded `CAUSE` finding.
- 31.4 claims/record on average (range 14–35).
- Idempotency keys identical across two independent runs (determinism).

## Known v1 limitations / next steps

1. **Primary-aircraft only for claims.** Multi-aircraft events (mid-airs,
   ground collisions) keep *all* aircraft in the raw payload, but event-level
   aircraft claims describe the lowest-`Aircraft_Key` aircraft. Per-aircraft
   fan-out is the natural v1.1.
2. **Crew / engine / injury-detail tables** (`Flight_Crew`, `engines`,
   `injury`) are not yet mapped; injury *totals* come from the `events` row.
3. **No incremental/delta feed.** v1 re-reads the full export; idempotency makes
   that cheap, but a date-windowed mode would cut load time on refreshes.
4. **ASN importer** can reuse this exact pattern (pure mapping core + reader);
   it would land as a second, lower-tier source so the winner policy lets NTSB
   final determinations win on conflict.
