# Atlas production cutover checklist

Use this checklist for first production launch and major re-launches.

## 1) Pre-cutover security and build gates

- [ ] CI `docker-build` job green on release branch.
- [ ] CI Trivy image scan green (`CRITICAL,HIGH --ignore-unfixed`).
- [ ] CI Trivy filesystem scan green.
- [ ] CI Gitleaks scan green (full git history).
- [ ] Local/CI release notes updated with exact scan dates and results.

## 2) Production configuration

- [ ] `ENVIRONMENT=production`
- [ ] `DATABASE_URL` / `TENANT_DATABASE_URL` / `SYSTEM_DATABASE_URL` set correctly.
- [ ] `API_KEY_HASH_SECRET` set (64+ hex chars).
- [ ] `ALLOWED_HOSTS` set to explicit hosts (no wildcard).
- [ ] `CORS_ORIGINS` set to explicit `https://` origins.
- [ ] `PROMETHEUS_ALLOWED_CIDRS` or strong `PROMETHEUS_BEARER_TOKEN` configured.
- [ ] `HSTS_ENABLED=true` once HTTPS is confirmed end-to-end.
- [ ] free/self-hosted only: `deploy/free/check-env.sh .env` passes.

## 3) Database readiness

- [ ] Run `alembic upgrade head` against production database.
- [ ] Verify `alembic current` equals the expected head revision.
- [ ] If no active admin API key exists, run `atlas bootstrap` and securely store printed admin API key.

## 4) Health and readiness

- [ ] `/health` returns 200.
- [ ] `/ready` returns 200.
- [ ] Authenticated smoke request succeeds with new API key.
- [ ] `/metrics` is inaccessible from public internet and available only to approved scraper identity/network.

## 5) Alerting and observability

- [ ] Prometheus rules from `ops/alerts/prometheus-atlas-rules.yml` loaded.
- [ ] Alert routing verified (warning + critical paths).
- [ ] Dashboards show:
  - `atlas_outbox_oldest_unprocessed_age_seconds`
  - `atlas_outbox_worker_heartbeat_present`
  - `atlas_operational_metrics_refresh_success`

## 6) Load and capacity validation

- [ ] Run k6 staging load test (`ops/load/atlas_k6_load_test.js`) with production-like data.
- [ ] Record results in `ops/load/BASELINE_TEMPLATE.md`.
- [ ] Confirm p99 latency and outbox backlog behavior are within acceptance limits.

## 7) Risk acknowledgements

- [ ] Starlette CVE mitigation accepted (TrustedHost + strict `ALLOWED_HOSTS`) until upstream upgrade path is available.
- [ ] `API_KEY_HASH_SECRET` rotation runbook reviewed: `ops/runbooks/api-key-hash-secret-rotation.md`.
- [ ] 501 behavior for `include_archive=true` endpoints accepted or hidden from external clients.

## 8) Sign-off

- [ ] Operator sign-off:
- [ ] Security sign-off:
- [ ] Release manager sign-off:
- [ ] Cutover timestamp (UTC):
