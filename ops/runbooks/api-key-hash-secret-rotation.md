# API_KEY_HASH_SECRET rotation runbook

This runbook covers planned and emergency rotation of `API_KEY_HASH_SECRET`.

## Important behavior

Atlas stores only one-way API key hashes. Because the hash includes
`API_KEY_HASH_SECRET`, existing hashes cannot be migrated to a new secret
without the original plaintext keys.

Atlas supports a bounded dual-secret verification window:

- `API_KEY_HASH_SECRET` is the current write secret (new keys hash with this).
- `API_KEY_HASH_SECRET_PREVIOUS` is optional and verification-only.

During rotation, old keys continue to authenticate while
`API_KEY_HASH_SECRET_PREVIOUS` is set to the old secret.

## Preconditions

- Change window approved and announced.
- New secret generated: 64+ hex chars from `python -c 'import secrets; print(secrets.token_hex(32))'`.
- Owner assigned for client key redistribution.
- Backout plan documented (restore prior secret value if rotation must be rolled back quickly).

## Planned rotation (bounded dual-secret cutover)

1. Generate and stage the new secret.
2. Set:
   - `API_KEY_HASH_SECRET=<new-secret>`
   - `API_KEY_HASH_SECRET_PREVIOUS=<old-secret>`
3. Restart API and worker processes so all instances load the new secret.
4. Verify old keys still succeed during the bridge window.
5. Create replacement API keys using the new secret context, e.g.:
   - `atlas bootstrap --role admin`
   - Repeat for additional keys/roles as needed.
6. Distribute new plaintext keys to callers over a secure channel.
7. Deactivate superseded keys for hygiene/audit:
   - `UPDATE api_keys SET is_active = false WHERE is_active = true;`
8. Remove `API_KEY_HASH_SECRET_PREVIOUS` after cutover window ends.
9. Restart API instances.
10. Verify:
   - old keys fail with 401 after previous secret is removed
   - new keys succeed
   - `/ready` and `/health` stay green
11. Wait at least `API_KEY_CACHE_TTL_SECONDS` (default 5s) and re-check that old keys are rejected on every instance.

## Emergency rotation (secret compromise)

1. Rotate `API_KEY_HASH_SECRET` immediately and restart all API instances.
2. Do **not** set `API_KEY_HASH_SECRET_PREVIOUS` when the old secret is compromised.
3. Assume all prior keys are invalid and compromised.
4. Issue new keys and redistribute on priority order (admin automation first).
5. Deactivate all prior keys in DB (`is_active=false`) once new keys are confirmed.
6. Record incident timeline, blast radius, and post-incident actions.

## Rollback

If the rollout fails before new keys are fully distributed:

1. Restore the previous `API_KEY_HASH_SECRET` value.
2. Restart all API instances.
3. Confirm previous keys authenticate again.
4. Re-plan rotation with a tighter distribution window.

## Follow-up improvement (recommended)

Add observability around secret-rotation windows (for example, log/metric when
verification succeeds via `API_KEY_HASH_SECRET_PREVIOUS`) to track residual old-key usage.
