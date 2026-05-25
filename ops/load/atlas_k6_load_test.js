import http from 'k6/http';
import { check, sleep } from 'k6';
import { randomSeed } from 'k6';

randomSeed(42);

export const options = {
  scenarios: {
    duplicate_ingestion_spike: {
      executor: 'constant-vus',
      vus: Number(__ENV.INGEST_VUS || 500),
      duration: __ENV.INGEST_DURATION || '2m',
      exec: 'ingestDuplicateRecords',
    },
    provenance_during_writes: {
      executor: 'constant-vus',
      vus: Number(__ENV.PROVENANCE_VUS || 25),
      duration: __ENV.PROVENANCE_DURATION || '2m',
      exec: 'readProvenance',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.02'],
    'http_req_duration{endpoint:ingestion}': ['p(95)<750', 'p(99)<1500'],
    'http_req_duration{endpoint:provenance}': ['p(95)<500', 'p(99)<1000'],
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const API_KEY = __ENV.API_KEY || '';
const SOURCE_ID = __ENV.SOURCE_ID || '00000000-0000-0000-0000-000000000001';
const PROVENANCE_EVENT_ID = __ENV.PROVENANCE_EVENT_ID || '';

function headers(idempotencyKey) {
  return {
    'Content-Type': 'application/json',
    'X-API-Key': API_KEY,
    'Idempotency-Key': idempotencyKey,
  };
}

function duplicatePayload() {
  // Every request gets a unique source_record_id and idempotency key so the
  // test cannot accidentally benchmark only replay/idempotency paths.  The
  // registration remains bucketed to create duplicate-match pressure.
  const recordNumber = (__VU % Number(__ENV.DUPLICATE_RECORD_BUCKETS || 20));
  const uniqueSequence = `${__VU}-${__ITER}-${Math.floor(Math.random() * 1_000_000_000)}`;
  const reg = `N${10000 + recordNumber}`;
  const eventDate = __ENV.EVENT_DATE || '2024-05-01';
  return {
    raw_payload: {
      source: 'k6',
      record_number: recordNumber,
      run_uuid: `vu-${uniqueSequence}-${Date.now()}`,
      registration: reg,
      event_date: eventDate,
    },
    source_record_id: `k6-${recordNumber}-${uniqueSequence}`,
    idempotency_key: `k6-${uniqueSequence}`,
    claims: [
      { field_name: 'event_date', field_value: eventDate },
      { field_name: 'registration', field_value: reg },
      { field_name: 'operator', field_value: `Load Test Operator ${recordNumber % 3}` },
      { field_name: 'location', field_value: `Load Test Site ${recordNumber % 5}` },
    ],
  };
}

export function ingestDuplicateRecords() {
  const res = http.post(
    `${BASE_URL}/api/v1/ingestion/sources/${SOURCE_ID}`,
    JSON.stringify(duplicatePayload()),
    { headers: headers(`k6-${__VU}-${__ITER}-${Date.now()}-${Math.random()}`), tags: { endpoint: 'ingestion' } },
  );
  check(res, {
    'ingestion accepted': (r) => [200, 201, 409, 422].includes(r.status),
  });
  sleep(Number(__ENV.INGEST_SLEEP_SECONDS || 0.05));
}

export function readProvenance() {
  if (!PROVENANCE_EVENT_ID) {
    sleep(1);
    return;
  }
  const url = `${BASE_URL}/api/v1/accidents/${PROVENANCE_EVENT_ID}/provenance?limit=50`;
  const res = http.get(url, {
    headers: { 'X-API-Key': API_KEY },
    tags: { endpoint: 'provenance' },
  });
  check(res, {
    'provenance ok or absent': (r) => [200, 404].includes(r.status),
  });
  sleep(Number(__ENV.PROVENANCE_SLEEP_SECONDS || 0.1));
}
