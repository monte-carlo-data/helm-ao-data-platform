# Backup cleanup

Optional cleanup runs after verified scheduled backups, in the same Job.
It is disabled by default and starts in preview mode. It does not change the backup schedule. The chart does not add
an alerting system. Cleanup checks every configured ClickHouse copy and supports
one or more copies.

This page covers `clickhouse.backup.cleanup`, the S3 preview and whole-set deletion option. The
separate [database-disk record cleanup](backup-local-cleanup.md) option can
remove older local records while preserving their completed S3 backups.

```yaml
clickhouse:
  backup:
    cleanup:
      enabled: true
      dryRun: true
      keepLast: 2
      keepDays: 0
      timeoutSeconds: 1800
```

## Existing count-and-age preview

The preview keeps the newest `keepLast` individual backups, plus every older
backup they require. `keepDays` also keeps backups inside the last N days and
their bases; zero disables that age window. For a 30-day window, set
`keepDays: 30`. Extra or missed runs do not shorten that window.

For example, with an old full/incremental pair and a new full, `keepLast: 2`
keeps all three: the old incremental still needs the old full. After a new
incremental completes, the old pair becomes eligible. The report lists the old
incremental before its full backup under `delete`; it does not remove either.
Unrelated remote backups and any bases they use are always preserved.

The defaults (`keepLast: 2`, `keepDays: 0`) keep two individual backups and their
required bases, not two days. From the second successful run of a new UTC day,
the previous day's chain can appear under `delete` unless another kept backup
still needs it. Use `keepDays: 30` for a 30-day production window.

`retentionPolicy: count-and-age` preserves this existing preview behavior.
Actual deletion requires `retentionPolicy: daily-weekly`, `dryRun: false`, and
the verified Altinity 2.8.2 image. A preview is recommended but is not an enforced
approval step: selecting deletion permits it on the next successful backup.

## Keep recent daily sets and older weekly sets

```yaml
clickhouse:
  backup:
    cleanup:
      enabled: true
      dryRun: true
      retentionPolicy: daily-weekly
      dailyDays: 7
      historyDays: 30
      timeoutSeconds: 1800
```

A set contains every scheduled full backup created on one UTC date and all
incrementals that depend on those full backups. The cleaner keeps or retires
that set together. `keepLast` and `keepDays` belong to the old preview policy
and do not select sets under `daily-weekly`.

- Keep every set with a backup from today or the previous `dailyDays - 1` UTC dates.
- For the older portion of the history, keep the most recent available day in
  each Monday-Sunday UTC week, usually Sunday. A missed Sunday therefore does
  not remove that week's only available backup. Today and the previous
  `historyDays - 1` dates are inside the window; older sets become eligible.
- A set's newest member determines its age. If an incremental still uses an
  older full backup, keep that full backup and the whole set together.
- Keep the newly verified backup's whole set, even after a long gap. Keep manual
  backups and complete sets needed by any retained or local-only backup.
- Additional full backups on a retained date stay too. With one full per day,
  the 7/30 settings normally retain about 10-11 full sets. Older recovery points
  are spaced farther apart than recent daily history.

These rules do not change daily full backups or four-hour incrementals. Broken
local records stop this policy because their missing metadata could hide a
needed base. An unavailable copy also stops cleanup.

## Enable S3 deletion

First test with disposable backups and restore one that the preview keeps.
Review the full preview for the deployment, then set `cleanup.dryRun: false`.
The scheduler verifies every copy reports Altinity 2.8.2 before removal and
again during the operation. Mixed-version upgrades cannot perform deletion.
The 2.8.1 image remains supported for previews and local-only cleanup.

For each expired backup, dependent incrementals come before their full backup:

1. Check every copy is idle and agrees about the backups and dependencies.
2. Remove matching local records on every copy while the completed S3 backup
   still exists. Records already removed by local cleanup are expected.
3. Confirm those records are absent, then ask the backup software to remove
   the S3 backup. A remote deletion from a copy with matching local records can
   otherwise leave native data behind, even with the fixed software.
4. Check the lists again before attempting another removal. Never force a
   dependency check or retry a request whose result is uncertain.

This is a deliberate retirement of a whole S3 backup set. It can remove local
records that the independent local-only two-date rule would otherwise keep,
such as an old set after a long gap in backups. It does not change that rule
for backups still retained in S3.

The scheduled job runs local cleanup and S3 cleanup sequentially. If enabled
local cleanup fails or does not finish, actual S3 deletion is skipped. A preview
may still run. Do not run separate manual backup, restore or deletion commands
at the same time: the APIs have no shared lock across copies. Pause the schedule
and wait for all active operations before recovery or manual maintenance.

## Reading the result

Find the backup Job and read its report, replacing `<namespace>` and `<job-name>`.
Scheduled Job names start with the ClickHouse name followed by `-backup-`
(`otel-backup-` with the default name):

```sh
kubectl get jobs -n <namespace> --sort-by=.metadata.creationTimestamp
kubectl logs -n <namespace> job/<job-name> -c backup | grep '^Backup cleanup'
```

The CronJob keeps the last three successful Jobs. With the default four-hour
schedule, an older report is normally removed after about 12 hours. Collect
these logs elsewhere if you need a longer history.

A completed preview writes a summary followed by one line per entry. For
example, this report keeps a full backup and its incremental, and proposes
removing one older full backup:

```text
Backup cleanup: {"broken_local_count": 0, "delete_count": 1, "deleted_count": 0, "keep_days": 0, "keep_last": 2, "kept_count": 2, "local_only_count": 0, "mode": "dry-run"}
Backup cleanup entry: {"list": "kept", "name": "ao-otel-full-20260102T000000Z-4567abcd"}
Backup cleanup entry: {"list": "kept", "name": "ao-otel-incremental-20260102T040000Z-89abcdef"}
Backup cleanup entry: {"list": "delete", "name": "ao-otel-full-20260101T000000Z-0123abcd"}
```

For scripts or alerts, parse the text after `Backup cleanup: ` or
`Backup cleanup entry: ` as JSON and read its fields. Do not match exact spacing
or key order; those may change.

`delete` entries list candidates in removal order; `deleted_count` is zero in preview mode.
In deletion mode, `deleted` lists confirmed S3 removals; per-operation events
record local and remote deletion attempts and confirmations. A time-limit report
can contain partial progress, so inspect `completed` and `stop_reason` too.
Every name is written separately, so the growing candidate list does not make
one increasingly long JSON line. Keep the summary and all entry lines to retain
the full report. Local entries also include `copy`: `0` is the first ClickHouse
replica, `1` is the second, `2` is the third, and so on.

Every configured copy must be reachable and report matching remote records. Missing
bases, broken remote metadata, busy copies, interrupted deletions, or records
changing during the check stop the preview. These cases cannot produce a
trustworthy removal list.

Preview failures log `Backup cleanup preview stopped: ...`; deletion failures log
`Backup cleanup stopped: ...`. Because the backup
has already been verified, the Job still succeeds. Read that line and the
report separately from backup Job status. Do not rerun a completed backup just
because its preview failed. This chart does not send preview alerts.

The Job deadline is `schedule.timeoutSeconds + cleanup.timeoutSeconds + 60`,
plus `localCleanup.timeoutSeconds` when database-disk cleanup is also enabled.
Keep it below the scheduled interval, with time for startup and scheduling
delays. Otherwise an unfinished Job delays the next scheduled run. It can
still start within the configured start deadline (900 seconds, or 15 minutes,
by default); after that the missed run is skipped. Defaults allow 10800 seconds
for backup and 1800 for preview, with a 60-second allowance, on a four-hour
schedule.

## Local files in the report

The count-and-age preview reports these local files without deleting them.
The daily-weekly policy stops on broken local metadata and protects local-only dependencies:

- `broken_local`: entries described by the tool as `broken metadata.json not
  found` or `parse metadata.json error: ...`. They can follow an interrupted
  download. The report does not trust their missing dependencies or copy parser
  details into logs. When a healthy remote backup with the same name exists,
  use the checks for a specific entry in
  [interrupted base downloads](clickhouse-backups.md#interrupted-base-downloads).
- `local_only`: scheduler-created entries without a remote backup of the same
  name. They can remain after an interrupted upload or an operator's manual
  removal of a remote catalog entry. They are safe to leave while you inspect
  the interrupted work, but they keep using local pointer space and may still
  refer to native S3 data. They are not remote backups or deletion candidates.
  This chart has no supported cleanup procedure for these entries.

A separate warning reports these files on each run until the entries change.
An entry can appear in both lists. In that case, follow the `local_only`
guidance: the interrupted-download removal procedure requires a healthy remote
backup and does not apply. Do not remove pointer directories by hand; they may
be the remaining record of native S3 objects.

## Preview stops that need investigation

A busy copy or a changing catalog can settle before the next run. The following
conditions can keep stopping previews even while new backups succeed. Pause
scheduled work and wait for any backup, restore, or deletion to finish before
repairing backup files or restarting a backup container.

- **An incomplete remote backup.** Upload writes table files before the top-level
  `metadata.json`, so an interrupted upload can leave an entry described as
  `broken (can't stat metadata.json)`. Check the failed upload and the local
  backup on the copy that created it. Confirm the remote metadata is incomplete,
  rather than temporarily unreadable because S3 access failed. Completing or
  repairing the upload can restore a valid remote entry. Do not remove the catalog entry as a shortcut: that can leave native files
  without the information needed to clean them up. Investigate or complete the
  interrupted upload before selecting a healthy backup for automatic cleanup.
- **A failed or cancelled deletion in API history.** The preview stops while
  any copy's history contains a failed or cancelled `delete` command, even
  if a later attempt succeeded. Inspect the affected backup and any partial
  removal first. Inspection alone does not clear the history. The tool keeps
  up to 1,000 finished actions by default (`general.status_history_size`);
  the old entry disappears when it ages out or the backup container restarts.
  Restarting clears in-memory history but does not repair backup files.
- **An unrelated local backup with no remote entry.** A manually created local
  backup outside this scheduler's names stops the count-and-age preview until it
  has a valid remote counterpart or is deliberately removed. Daily-weekly keeps
  readable local-only backups and their required sets. Decide whether that
  backup should be uploaded or retained. Do not delete it merely to silence
  the preview; local deletion can also remove native S3 objects.

After addressing the cause, check the next preview's logs. Job success alone
does not show that the preview recovered.

## Native files and saved S3 versions

Altinity 2.8.2 includes the native-file deletion fix, including files named
`serialization.json`. This chart uses that standard release, not a modified
image. The tool's built-in keep-counts remain zero; this cleaner selects the
complete sets and performs the checked deletion sequence instead.

https://github.com/Altinity/clickhouse-backup/releases/tag/v2.8.2

https://github.com/Altinity/clickhouse-backup/pull/1590

On an **unversioned bucket**, successful deletion removes the selected S3 files.
On a **versioned bucket with restricted backup permissions**, ordinary deletion
adds delete markers and leaves old object versions stored and billed. Recovery
from an accidental overwrite or removal remains possible while those versions
and the original key are kept.

Before enabling deletion, check both the ClickHouse and backup-container AWS
roles. Keep `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion` out of their permissions. The shared Terraform backup
role already omits them. Check all role policies and bucket grants, including
wildcard permissions. Helm users supplying their own roles must check this
themselves: the upstream tool can permanently remove versions when given those
broader permissions, so an administrator role does not provide the same recovery
protection. Test ordinary deletion and a denied version-deletion request with
the actual backup role before enabling cleanup on existing backups.

Upstream uses the same version-aware deletion paths for catalog and native files:
https://github.com/Altinity/clickhouse-backup/blob/v2.8.2/pkg/storage/s3.go#L557
and https://github.com/Altinity/clickhouse-backup/blob/v2.8.2/pkg/storage/s3.go#L622.

Configure a separate S3 rule if older object versions should eventually expire.
Choose how many days they remain recoverable after deletion or overwrite. That
clock starts when a version becomes old, not when its backup was created, and
expiry can affect versions already present when the rule is enabled. For
example, seven days is a recovery window, not seven more days of ordinary
backup history. S3 processes expiry asynchronously, so removal is not immediate.

Only expire **noncurrent versions** and empty delete markers. Do not use an S3
age rule to delete current files from either backup prefix: S3 cannot know
which full backup an incremental needs. The chart does not manage bucket
lifecycle rules and does not grant the backup writer permanent-version deletion
permissions. The corresponding Terraform setting is optional and disabled by
default. Read-only recovery permissions remain read-only.

https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html

## Local disk growth and S3 deletion

ClickHouse keeps small files describing S3 object locations under
`/var/lib/clickhouse/disks/backups_s3/`. The separate
[database-disk record cleanup](backup-local-cleanup.md) removes older local
records while preserving their S3 backups. S3 cleanup removes any remaining
local records only for sets being deliberately retired. Both options are off
by default; watch disk space and available file entries on every copy.

## Checks after installation

1. Start with a preview. Read the summary and every entry, and compare retained
   sets and the ordered removal list with the backup catalog. No files should change.
2. In an isolated test, enable deletion and verify both the catalog and actual
   native S3 files. Test when matching local records exist on both, one or neither
   copy. A missing catalog entry alone does not prove that the data was removed.
3. Restore a retained incremental, compare the data, and verify a later backup.
4. For versioned storage, verify older versions are still recoverable within the
   chosen protection period and eventually expire under the AWS rule. Local
   S3-compatible tests do not prove AWS lifecycle timing.
5. Inspect `broken_local`, `local_only`, partial-progress results and cleanup
   errors separately from the successful backup Job. Preserve the logs before
   Kubernetes removes older successful Jobs.

The local Docker tests use MinIO for disposable S3-compatible storage. Its
versioned-bucket listing can retain an empty prefix after ordinary deletion:
https://github.com/minio/minio/issues/17810. The backup API then keeps listing
the deleted entry, so the cleaner stops rather than trusting an inconsistent
result. The test verifies that stop and the preserved versions; it does not
prove a complete versioned cleanup run on AWS. Test that run, recovery, and
eventual version expiry in an isolated AWS bucket before enabling them on
existing backups. Do not bypass the catalog check to work around MinIO.
