# Scheduled ClickHouse backups

This uses Altinity `clickhouse-backup:2.8.1` with ClickHouse `26.4.3`:
https://github.com/Altinity/clickhouse-backup/tree/v2.8.1

Backups are disabled by default. Enabling them adds a container to each ClickHouse
pod and rolls the pods once. Keep the existing data volumes. The AWS bucket, role,
and database password must already exist. The role must trust the dedicated
Kubernetes account in the release's namespace, never `default`.

Example values (replace the placeholders):

```yaml
clickhouse:
  backup:
    enabled: true
    provider: aws
    aws:
      bucket: your-backup-bucket
      region: us-west-1
      roleArn: arn:aws:iam::123456789012:role/clickhouse-backup
    serviceAccount:
      name: clickhouse-backup
    externalSecret:
      secretStoreRef:
        name: aws-secrets-manager
      remoteRef:
        key: your-cluster/clickhouse/backup-credentials
    api:
      existingSecret: clickhouse-backup-api
```

The API Secret must exist in the same namespace with a `password` key. Use a
separate generated password for these controls. The database password comes from
External Secrets and is reread for each operation; its optional `previousKey`
works with the chart's existing password-change process. API credentials stay in
the container environment: changing those requires suspending jobs, waiting for
active operations to finish, and restarting the backup containers before resuming.
Version 2.8.1 logs submitted credentials on failed authentication; use only dummy
passwords when testing denied access, and restrict access to container logs.

The job runs every four hours in UTC. The first successful run each day makes a
full backup; later runs save changes from that full backup. All `otel_traces`
tables are included, including new ones. It tries copy 0 first, then copy 1.
Before an incremental on another copy, it downloads the full backup's small
metadata files. Database backup data stays in S3, encrypted by the bucket's
existing settings. No AWS access keys or KMS key material are stored in the chart.

Only the scheduled jobs may reach port 7171. A cluster with working NetworkPolicy
enforcement is required; the API also requires its password. The database user
can take backups and read metadata, but cannot restore or change tables.

The job waits for completion and checks the S3 catalog. If a submission response
is lost, it fails without sending another backup request or switching copies:
the first operation might still be running. Check the job and backup-container
logs before retrying; concurrent or automatic retries could duplicate work.

Before calling the installation complete, check a scheduled full and incremental
backup, an incremental on copy 1 based on copy 0's full, and denied API access
from an unrelated pod. Local tests do not prove AWS permissions or network rules.
Follow [the live backup test steps](verify-clickhouse-backups.md) and record the
backup names and results in the issue.

Retention and alerts belong to AO-1300. Automatic deletion is off, including S3
expiry, because newer backups can depend on older files. Restore drills belong
to AO-1301. Embedded mode ignores `restore_schema_on_cluster`; AO-1301 must test
separate administrative restore configuration with
`clickhouse.use_embedded_backup_restore_cluster: otel` and prepare the required
backup metadata on participating copies. That setting alone is not a verified
restore procedure. Never put it in the scheduled-backup configuration: it also
makes backups run on both copies.

https://linear.app/montecarloai/issue/AO-1299/add-scheduled-backups-to-the-test-cluster
https://linear.app/montecarloai/issue/AO-545/epic-f-clickhouse-backup
