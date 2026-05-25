# Atlas load-test baseline template

Record this after each staging load test and keep it under version control.

## Test metadata

- Date (UTC):
- Environment:
- Atlas image tag/digest:
- Database size snapshot (events/claims/outbox rows):
- k6 script:
- k6 params (`INGEST_VUS`, `PROVENANCE_VUS`, `INGEST_DURATION`, `DUPLICATE_RECORD_BUCKETS`):

## API performance

- Ingestion p50:
- Ingestion p95:
- Ingestion p99:
- Ingestion error rate:
- Provenance p50:
- Provenance p95:
- Provenance p99:
- Provenance error rate:

## Throughput and backlog

- Ingestion RPS (avg):
- Provenance RPS (avg):
- Peak `atlas_outbox_events_total{status="pending"}`:
- End-of-test `atlas_outbox_events_total{status="pending"}`:
- Outbox drain time back to steady state:

## Database and pool signals

- Postgres CPU peak:
- Postgres memory peak:
- Deadlocks observed:
- Lock wait spikes observed:
- PgBouncer `cl_waiting` peak:
- PgBouncer `sv_active` peak:
- PgBouncer `sv_idle` floor:

## Verdict

- Pass/Fail:
- Bottleneck summary:
- Required actions before production:
