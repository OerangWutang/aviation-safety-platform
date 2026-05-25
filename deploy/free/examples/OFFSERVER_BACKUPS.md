# Off-server backup example

A compressed dump on the same free VPS is useful for quick restores, but it is
not a backup if the VPS disk or provider account disappears. Copy backups to a
separate account or machine.

## Option A: rclone

1. Install and configure rclone on the server:

```bash
rclone config
```

2. Test the remote:

```bash
rclone lsd remote:
```

3. Add this after the local backup cron line:

```cron
30 3 * * * cd /opt/atlas/deploy/free && rclone copy ./backups remote:atlas-backups --include 'atlas-*.sql.gz' --transfers 2 >> ./backups/rclone.log 2>&1
```

## Option B: restic

```bash
export RESTIC_REPOSITORY='sftp:user@example.com:/srv/restic/atlas'
export RESTIC_PASSWORD='store-this-outside-the-repo'
restic init
restic backup /opt/atlas/deploy/free/backups
restic forget --keep-daily 7 --keep-weekly 4 --prune
```

Keep the restic password outside the repository and test `restic restore` before
trusting it.
