# Scheduled ClickHouse backups

This uses ClickHouse `26.4.3` and the standard Altinity clickhouse-backup `2.8.1`
image. The chart pins the published multi-platform digest; it does not build or
patch the backup software. Source: https://github.com/Altinity/clickhouse-backup/tree/v2.8.1

A chart-supplied startup script waits for nonempty database and API passwords,
checks the API revision, and validates a private copy of the configuration with
the stock program before starting its server. The server keeps that checked
configuration and those passwords until it restarts. Missing credentials leave
the helper waiting, without making ClickHouse startup depend on the secret store.

Upgrading with backups disabled leaves ClickHouse unchanged. Enabling backups
adds a container to each ClickHouse pod and rolls the pods once, keeping the
existing data volumes. It also moves the pods to the backup AWS ServiceAccount,
so the ClickHouse server itself gains S3 access, and adds an ingress NetworkPolicy
that limits which ports callers may reach. Later changes to the backup container's
image or resources roll the ClickHouse pods again. A failed backup container makes
its whole pod NotReady and takes that replica out of client service, even if the
database is healthy. The backup container uses a `400MiB` Go memory target under
its default `512Mi` memory limit. Keep `clickhouse.backup.sidecar.goMemoryLimit`
below the container limit when changing either value.

With backups enabled, the chart sets global `s3.use_environment_credentials=0`
so an `s3()` query without supplied AWS keys cannot automatically use the server's
backup credentials. The `backups_s3` disk keeps its own setting at `1`, allowing
embedded backups to use the AWS role. Existing `s3()` queries that relied on
automatic access will now fail; supplying credentials explicitly still works.

The backup container runs as user and group `101` and must write to the shared
data volume. Enabling backups sets `fsGroup: 101` so that access works on a new
volume too. With `fsGroupChangePolicy: OnRootMismatch`, the first enable can still
walk existing files to update their permissions if the volume root's group and
permission bits do not match. Allow extra time for that first pod restart on a
large volume; a single-copy installation is unavailable while its pod restarts.
The exact behavior depends on the volume driver.

## AWS prerequisites

Create these before setting `clickhouse.backup.enabled: true`:

- An S3 bucket in the configured region. Both clients need access: ClickHouse writes
  data under `<path>/native/`, and clickhouse-backup writes its catalog under
  `<path>/catalog/`. The default `<path>` is `clickhouse`.
- An IAM role for service accounts (IRSA). The chart sets the
  `eks.amazonaws.com/role-arn` annotation; it does not configure EKS Pod Identity
  associations. The cluster must have its IAM OIDC provider and IRSA setup working.
  Trust `sts:AssumeRoleWithWebIdentity` from that provider with subject
  `system:serviceaccount:<namespace>:<serviceAccount.name>` and audience
  `sts.amazonaws.com`. Use the dedicated account, never `default`.
- Role permissions `s3:ListBucket` and `s3:GetBucketLocation` on the bucket, plus
  `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, and `s3:AbortMultipartUpload`
  on `<bucket-arn>/<path>/*`. A policy covering only one of the two subdirectories
  will fail. If restricting `ListBucket` by prefix, allow both subdirectories.
- For SSE-KMS encryption, `kms:GenerateDataKey` and `kms:Decrypt` on the bucket's
  encryption key, with a key policy that permits the role to use it.
- No lifecycle expiration, including noncurrent-version expiration, covering
  `<path>/native/` or `<path>/catalog/`. The chart cannot inspect or change bucket
  lifecycle rules; removing older files can break newer incremental backups.
- Separate database backup and freshness-probe passwords in your external secret
  store, with a working SecretStore or ClusterSecretStore that can read each key.
  Their ExternalSecrets and ClickHouse user definitions are separate from other
  database users; a failed lookup cannot stall those users' password updates.
- A separate API Secret in the release namespace containing `password` and
  `revision`, or `api.externalSecret` as described below. Set `api.passwordRevision`
  to that same revision. It is an identifier such as `1`, not a password.

Missing database backup credentials leave that user absent and the helper waiting;
missing probe credentials leave the probe user absent and cause backup Jobs to fail.
ClickHouse can still start in either case. Each separate user store has an
always-present empty `users.xml`; its optional Secret supplies the full user
definition at `users.d/auth.xml`. Empty credentials do not create passwordless users. After fixing Secret delivery, reload the database configuration
using your normal credential-change process and verify the users before resuming
backups. The helper itself waits for valid configuration, nonempty database/API
passwords, and a matching API revision.

The companion AWS Terraform module can provision the bucket, encryption key,
role, and credentials. Select a revision with `clickhouse_backup` support:
https://github.com/monte-carlo-data/terraform-aws-ao-data-platform

AWS IRSA setup:
https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html

## Install with the schedule paused

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
      path: clickhouse
    serviceAccount:
      name: clickhouse-backup
    user:
      externalSecret:
        secretStoreRef:
          name: aws-secrets-manager
        remoteRef:
          key: your-cluster/clickhouse/backup-credentials
    probe:
      externalSecret:
        secretStoreRef:
          name: aws-secrets-manager
        remoteRef:
          key: your-cluster/clickhouse/backup-probe-credentials
    api:
      existingSecret: clickhouse-backup-api
      passwordRevision: "1"
    schedule:
      suspend: true
      cron: "0 */4 * * *"
      timeoutSeconds: 10800
```

`suspend: true` pauses the schedule only; it still installs the backup containers,
changes the ServiceAccount and network rules, and rolls the ClickHouse pods.
Verify that the password Secrets have synced, all replicas are healthy and caught
up, and the access checks below pass. Then set `schedule.suspend: false` through
your normal Helm or Terraform deployment and check the scheduled runs. Suspending
does not stop a Job or backup already running.

Use a separate generated API password. The database and probe passwords come
from their own ExternalSecrets. Their optional `previousKey` supports the chart's
two-password database rotation process. Pause Jobs and wait for active work to
finish before rotating the backup database password. Reload ClickHouse to accept
both passwords, restart the backup containers so they take a new configuration
snapshot, verify access, then retire the old password and resume Jobs. A database
configuration reload alone does not update the helper's saved password.

For API credentials managed by External Secrets, omit `existingSecret` and set
`api.externalSecret` with `secretStoreRef` and `remoteRef.key`. The external value
must be JSON containing both `password` and `revision`; leave `remoteRef.property`
empty so both fields come from the same version. The target Kubernetes Secret is
`otel-backup-api` by default; `api.secret` overrides the target name. Set `api.passwordRevision` to the source's revision. This avoids
labeling an old password with a new revision while the external store is syncing.

To rotate the API password, pause new scheduled Jobs and wait for active work to
finish. Update the password and revision together in the existing Secret or its
external source, then change `api.passwordRevision` in your deployment. The
annotation change makes the ClickHouse operator roll the pods; the helper starts
only when its mounted Secret matches that revision. Jobs check the Secret and all
replica pod annotations before sending credentials, so a mixed rollout fails the
Job without trying old or new passwords against mismatched helpers. Resume the
schedule after the roll and verify a backup. Suspending first also avoids failed
Jobs during this planned change; suspension does not stop work already running.

Use only dummy passwords for denied-access tests and never put credentials in
`?user=&pass=` URLs. Stock Altinity 2.8.1 can log submitted passwords when
authentication fails. The scheduler checks revisions before sending passwords,
and rotation must stay paused until all copies use the new revision. Restrict
access to backup-container logs as well as the API.
Changing the password without changing its revision can pass the revision checks
but cause a `401` or `403` rejection. The scheduler stops on that rejection rather
than trying another copy or repeating the password. The first rejected request
may already have logged it: restrict those logs and rotate the password using
the paused procedure above. Revision files must contain exactly the configured
value, without a trailing newline; use `printf '%s' '1'`, not `echo 1`, when
preparing a file for `api.existingSecret`.

## Operation and access

By default the job runs every four hours in UTC. The first successful run each
day makes a full backup; later runs save changes from that full backup. All
`otel_traces` tables are included, including new ones. The job tries replicas in
number order, starting with copy 0; a single-replica installation has no fallback.
Before selecting a source, the authenticated `backup_probe` user checks every
`otel_traces` replicated table: at most `schedule.maxReplicaDelaySeconds` of delay
(default 5 seconds), no read-only replicas or expired Keeper sessions, and no
missing replication records. Excess delay is retried for up to
`schedule.freshnessRetrySeconds` (default 30 seconds) before trying another copy.
Queued inserts alone do not disqualify a copy. A single-copy installation can
back up ordinary nonreplicated tables; an empty database still has nothing to
back up.
If no reachable replica passes, the Job fails without starting a backup. Services
publish NotReady addresses so a recovering replica's active backup remains visible;
that does not make it eligible to supply the next backup. Probe requests verify
ClickHouse's TLS certificate when `tls.enabled` is true.

Before an incremental on another copy, it downloads the full backup's small
metadata files. Database backup data stays in S3, encrypted by the bucket's
settings. No AWS access keys or KMS key material are stored in the chart.
If an earlier download left incomplete local files, the job checks another
healthy copy before submitting any work. If every healthy copy explicitly has
an incomplete base, it creates a replacement full backup. Later runs use that
new full. This can add one full backup's storage and runtime; a failed status
read alone never triggers it. Once a download or backup request is sent, the job
does not switch copies or resubmit it.

The NetworkPolicy permits port 7171 only from this release's backup Job pods in
the same namespace. A cluster with working NetworkPolicy enforcement is required;
other policies selecting these pods can also grant access. The API additionally
requires its password. Selecting the ClickHouse pods also denies other ingress
unless a rule allows it: the chart preserves its database, replication, and
metrics ports. Add custom listener ports to
`clickhouse.backup.networkPolicy.additionalPorts`; these ports are open
to all sources; the chart rejects port 7171 in that list.

The loopback-only database user `backup` has `BACKUP`, `SHOW TABLES`, and
`SHOW DATABASES` on `otel_traces.*`, plus `SELECT ON system.*`. That last grant
includes all users' query log text, not just table metadata. It cannot restore
or change tables. The API password separately grants control over the backup
tool, including remote deletion. The separate `backup_probe` user has only
`SELECT ON system.replicas` and `SHOW TABLES` / `SHOW DATABASES` on `otel_traces.*`;
it cannot read table data or take backups. Its profile is read-only, and
`probe.networksIp` can restrict the allowed client networks. The Job can only get
the named ClickHouse pods, not list other pods, to check API revisions. It mounts only the public
ClickHouse CA certificate for the database probe.

The job waits for completion and checks the S3 catalog. If a submission response
is lost, it fails without sending another backup request or switching copies:
the first operation might still be running. Check the job and backup-container
logs before retrying; concurrent or automatic retries could duplicate work.
`schedule.timeoutSeconds` bounds the scheduler's wait (60 to 14400 seconds,
default 10800). Kubernetes separately sets the Job's `activeDeadlineSeconds` to
that value plus 60 seconds and, when cleanup is enabled, `cleanup.timeoutSeconds`.
These limits start from different events: scheduler
startup and Job start, respectively. The stock tool requires its own ClickHouse
timeout of four hours.
Ending the Job does not cancel server-side work; check for a running operation
before retrying. With `concurrencyPolicy: Forbid`, a Job that outlasts the cron interval causes scheduled
runs to be skipped; leave time between the timeout and the next run.
At the default four-hour interval, `timeoutSeconds` of 14340 or more leaves no
gap before the Job deadline reaches that interval even with cleanup disabled.
When cleanup is enabled, include its timeout in the total too. Allow time for startup and
scheduling delays as well. A successful empty operation-status response means
the backup process has lost the record, for example after a restart; the job
stops with an explanation rather than waiting until the deadline. Inspect the
selected copy before starting more work.

Before calling the installation complete, check a scheduled full and incremental
backup, an incremental on another replica based on the first replica's full where
applicable, and denied API access from an unrelated pod. Local tests do not prove
AWS permissions or network rules. Follow [the live backup test steps](verify-clickhouse-backups.md)
and record backup names and results in your change record. The copy-switch helper
requires exactly two replicas and an idle, unsuspended CronJob with schedule
`0 */4 * * *` in UTC; it does not support custom schedules.

## Optional retention previews (5.3.0+)

Enable `clickhouse.backup.cleanup.enabled` to report which older backups fall
outside `keepLast` and `keepDays` after each verified backup. It is disabled by
default. The report preserves every required base and unrelated remote backup.
It needs two available copies with matching remote catalogs. Known incomplete
local entries and scheduler-owned local files without remote backups are listed
separately; no files are removed. Other preview errors log
`Backup cleanup preview stopped:` and leave the successful backup Job successful.

Keep `cleanup.dryRun: true`: the chart and Python entry point reject deletion
because stock 2.8.1 leaves native JSON objects behind. No deletion implementation
or custom image is included. See [retention previews](backup-cleanup.md) for
configuration and checks. The Job deadline includes `cleanup.timeoutSeconds`,
so allow time for both backup and preview before the next scheduled run.

## Current limitations

- **No automatic retention.** Both keep-counts are zero, so the backup tool never
  deletes backups. With the default schedule, expect roughly one full backup's
  storage growth per day, plus incremental data and metadata; watch bucket size.
  The native S3 disk also keeps small pointer files on the database's data volume
  under `/var/lib/clickhouse/disks/backups_s3/`. A full backup adds pointers for
  its files, incrementals add pointers for changed data, and downloading a base
  on another copy adds its pointers there. Watch every copy's local disk, not
  only the S3 bucket: running out of disk space or file entries can stop database
  writes. Use `df -h /var/lib/clickhouse`, `df -i /var/lib/clickhouse`, and
  `du -sh /var/lib/clickhouse/disks/backups_s3` inside each ClickHouse container.
  For illustration, a workload with roughly 1,000 wide parts and 80–120 files per
  part can add around 100–150 thousand pointer files and 0.5 GB per full backup.
  An ext4 volume with 100 GiB and its usual fixed allocation of roughly 6.5 million
  file entries could exhaust them in about 6–9 weeks at that rate. These are
  workload estimates, not a prediction for every installation. For new volumes,
  prefer a StorageClass using XFS, which allocates file entries as needed; it
  still needs enough disk space. Do not convert an existing data volume as part
  of enabling backups.
  Bucket lifecycle rules are separate and must not expire either backup prefix.
  If manual pruning is necessary, check dependencies in the catalog first. For
  this scheduler's usual daily chains, remove the whole UTC day's full and its
  dependent incrementals, never the full alone. Manual backups can depend on an
  older day's full; retain the base until every backup depending on it is removed.
  Production enablement should wait for tested retention that removes selected
  remote backups first, then their local pointer directories on every copy,
  while preserving every backup still needed by a retained backup. The cleanup
  preview in 5.3.0 does not limit this growth; remote-first deletion and local
  pruning still need to be implemented and tested. The tool's manual `delete
  remote` also leaves native JSON files behind; see [the deletion limitation](backup-cleanup-tool-bug.md).
- **Restore is not yet supported or documented.** A successful backup Job is not
  proof that a restore works. The scheduled configuration disables cluster-wide
  backup/restore. Embedded mode ignores `restore_schema_on_cluster`; a separate
  administrator configuration and a tested restore procedure are still needed.
- **Alerts require separate setup.** This chart does not choose a monitoring
  system or send backup alerts. Connect Job status and the cleanup report to
  your existing monitoring; a successful backup Job does not prove its preview
  completed, and a failed Job alone does not prove notification delivery.
- **Freshness checks are a point-in-time check.** They exclude replicas with
  missing or delayed data before backup submission; pod Ready alone is not used.
  They do not prove that a restore succeeds or make concurrent writes a global
  database snapshot. A completed catalog entry still needs a tested restore.

## Interrupted base downloads

Stock 2.8.1 writes `metadata.json` last when downloading a backup. An interrupted
download can leave a local entry described as `broken metadata.json not found`
or `parse metadata.json error: ...`. The scheduler can work around that entry as
described above, but it does not remove the files.

To remove a specific incomplete entry manually, first pause scheduled work and
verify that no backup, restore, or cleanup is running on any copy. Check that
the local entry has one of those two descriptions and that the same name has a
healthy completed remote entry (`directory, embedded`) in the shared catalog.
On the affected copy, use the configured tool's `delete local <name>` for that
exact entry. If the tool refuses because another backup depends on it, stop and
inspect that dependency; do not force deletion. Do not use this procedure for a
healthy `embedded` entry or one without a healthy remote counterpart. Avoid
`clean/local_broken`, which acts on all broken local entries rather than only
the entry you checked. Verify the remote backup still exists before resuming.

## Updating the images

The backup image digest is pinned because the startup script and scheduler were
tested against that exact build. A registry mirror is allowed if it serves the
same digest. Within `charts/ao-data-platform`, update its default in `values.yaml`,
the CI example in `ci/backup-values.yaml`, and validation and its error in
`templates/_backup.tpl`. Also update chart assertions in `hack/tests/test_backup_chart.py`,
the runtime default in `hack/tests/runtime/test_backup_runtime.py`, the CI pull
in `.circleci/config.yml`, and version references in the scheduler, README, and
backup docs.

For a ClickHouse server upgrade, keep `charts/ao-data-platform/values.yaml`, the CI pull, the runtime
test's `CH_IMAGE` default, and the documented version together. Test the chosen
server and backup versions together, including a real full and incremental
backup; testing an older server does not validate the newly selected pair.
