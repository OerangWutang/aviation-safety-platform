# Atlas free deployment restore drill

Run this before you trust your backups.

1. Pick a recent backup from `deploy/free/backups/`.
2. Start a disposable test stack, not your production volume.
3. Restore into the disposable database. The restore script intentionally requires `ATLAS_RESTORE_CONFIRM=1` so it is harder to run against production by accident:

```bash
ATLAS_RESTORE_CONFIRM=1 ./restore-postgres.sh ./backups/atlas-YYYYMMDDTHHMMSSZ.sql.gz
```

If you are intentionally restoring into a database that already has public
tables, the script also requires `ATLAS_RESTORE_ALLOW_DIRTY_DB=1`. Avoid that
for routine drills; use a fresh disposable database instead.

4. Run the app against the restored database and check:
   - `/health` returns OK
   - admin bootstrap/auth still works
   - recent events and provenance are present
   - the outbox worker can process a small batch

Keep at least one backup copy off the VPS.
