# Atlas load testing

This k6 scenario intentionally stresses the paths hardened for scale:

- duplicate-heavy ingestion (`/api/v1/ingestion/sources/{source_id}`) with unique source records and idempotency keys on every request,
- provenance pagination during concurrent writes,
- auth caching, JSON rendering, outbox creation, event matching, and conflict/review paths.

Run against a disposable environment only:

```bash
BASE_URL=https://atlas.example.com \
API_KEY=... \
SOURCE_ID=00000000-0000-0000-0000-000000000001 \
PROVENANCE_EVENT_ID=<large-event-id> \
k6 run ops/load/atlas_k6_load_test.js
```

Useful knobs:

- `INGEST_VUS` default `500`
- `PROVENANCE_VUS` default `25`
- `INGEST_DURATION` default `2m`
- `DUPLICATE_RECORD_BUCKETS` default `20` (registrations are bucketed; source records are still unique)
- `EVENT_DATE` default `2024-05-01`

Watch `/metrics`, Postgres CPU, lock waits, and PgBouncer pool saturation while the test runs.

After each run, capture baseline results in `ops/load/BASELINE_TEMPLATE.md` so
future changes can be compared against a known-good performance profile.
