# Remove older backup records from database disks

Native S3 backups leave small files on each ClickHouse database disk describing
where their data lives in S3. These files grow as backups are created or
downloaded. `clickhouse.backup.localCleanup` can remove older records after a
successful backup, while keeping the completed backups in S3.

This option is off by default. It supports the stock Altinity
2.8.1 and 2.8.2 images and requires no new AWS permissions. Changing this option alone
changes the backup Job scripts and schedule settings, not the ClickHouse or
Keeper installation. Review other changes in a chart upgrade separately: chart 5.8.0 changes the
default backup image to 2.8.2, which rolls ClickHouse pods when backups are enabled.

## Start with a preview

Preview is the default mode and the recommended first step. It is not an
approval step enforced by the software: setting `dryRun: false` permits
deletion on the next successful backup run, even without an earlier preview.

With backups already enabled, add:

```yaml
clickhouse:
  backup:
    localCleanup:
      enabled: true
      dryRun: true
      keepFullBackupDays: 2
      timeoutSeconds: 1800
```

After the next successful backup, read the backup Job's logs:

```sh
kubectl get jobs -n <namespace> --sort-by=.metadata.creationTimestamp
kubectl logs -n <namespace> job/<job-name> -c backup
```

Lines starting `Backup local cleanup: ` contain a JSON summary. Lines starting
`Backup local cleanup entry: ` list each kept or proposed record with its
backup name and copy number (`0` is the first copy). `delete` is the proposed
list; `deleted` lists confirmed removals. `delete` is informational and
`deleted` is empty in preview mode. Save these logs if you need a longer
history than the CronJob's three successful Jobs.

The default protects every full backup from the latest two distinct UTC dates
with successful full backups, plus the incrementals that depend on them. Extra
full backups on one date do not count as extra days. Missed days do not shorten
the protected history. `keepFullBackupDays` must be an integer of at least two;
it is not a fixed 48-hour age limit.

Only names created by this scheduler can be removed. Unrelated backups and
any local records they depend on stay. A record without a matching completed
S3 backup also stays. If any local record has broken metadata, no records are
removed: its missing information could hide a dependency on another backup.
Inspect `broken_local` and `local_only` entries before proceeding.

The tool's built-in `backups_to_keep_local` stays at zero. It counts individual
backups and deletes with force, bypassing the check for dependent local
backups. This cleaner instead keeps successful UTC days and their dependency
chains, checks every copy, and requires a matching completed S3 backup before
each removal. These are different retention rules. See the upstream
implementation:

https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/delete.go#L199-L223

## Enable removal

Test cleanup and recovery with disposable data first, then review the preview
for your installation. Set `localCleanup.dryRun: false` to permit removal on
later successful backup runs. This does not enable S3 deletion; the separate
`cleanup` option has its own preview and deletion controls. When S3 deletion
is explicitly enabled, it also removes remaining local records for the sets
it retires. See [S3 backup cleanup](backup-cleanup.md).

Every configured database copy must be reachable and idle, and must agree on
the completed S3 backups. Before and after each removal, cleanup checks the
backup lists and their dependencies on every copy. It removes dependent
incremental records before full-backup records and never uses force deletion.
If a check fails, it stops sending deletion requests. Lines starting
`Backup local cleanup: ` record `delete_requested` and `delete_verified` for
each record, preserving the result even when a later removal fails.

Do not run manual backup, restore, or remote-deletion commands while scheduled
cleanup runs. The backup APIs do not provide one lock covering all copies.
The scheduled Job forbids overlapping scheduled runs, but cannot block an
independent command. In particular, the matching S3 backup must remain present:
Altinity's local-deletion command can remove native S3 data when it is absent.

A failed cleanup logs `Backup local cleanup stopped: ...` separately; it does
not turn an already verified backup into a failed Job. Check these logs rather
than relying on Job success. If a deletion request times out, cleanup does not
retry or switch copies. Check the backup service and current lists before
starting more work; the server may still be finishing the request. The chart
does not add cleanup alerts.

A large backlog may take several scheduled runs to clear. If the time limit
is reached after a plan is available and without an uncertain request, the
summary reports `completed: false`, `stop_reason: time_limit`, and the
removals already verified in `deleted`. This is an expected pause; the next
successful backup run checks the remaining records again. Reaching the limit
before a plan is available, a request timeout, or an unverified deletion still
reports an error. Do not retry an uncertain deletion to clear the backlog.

Before starting each removal, cleanup reserves five minutes for the deletion,
four 30-second reads per copy to verify the result, and another 30 seconds for
processing. With two copies, more than 570 seconds must remain. If less time
is available, it returns the partial summary without sending or logging a new
deletion request. It does not shorten a deletion's timeout just to fit another
removal in. This leaves time unused when the next removal cannot safely fit;
unexpected delays or request failures still stop cleanup with an error.

`timeoutSeconds` must be an integer of at least 60. A short timeout can be used
for previews, but removal needs the time above plus the initial checks; keep
the default 1800 seconds unless a different limit has been tested. The Job
deadline includes the backup timeout, each enabled cleanup timeout, and 60 seconds. With both
cleanup options enabled at their defaults, that total is 14,460 seconds,
slightly longer than the four-hour schedule. Choose shorter cleanup timeouts
when combining them. An unfinished Job delays the next scheduled run because
`concurrencyPolicy: Forbid` prevents overlap. That run can still start within
the `startingDeadlineSeconds` window (900 seconds, or 15 minutes, by default).
After that, the missed run is skipped. Leave room for Job startup as well as
cleanup.

## When cleanup stays stopped

First pause the schedule with `clickhouse.backup.schedule.suspend: true` and
wait for any backup, restore, download, or deletion to finish on every copy.
Pausing the schedule does not cancel an existing Job or server operation.

- **Failed or cancelled deletion in `/backup/actions`.** Inspect the exact
  backup name in `/backup/list/local` on the affected copy and in
  `/backup/list/remote` on every copy. Confirm that its completed S3 backup
  still exists and that retained backups have their required bases. The local
  record may still exist or may already be gone after a partial attempt; do
  not resend the deletion until that state is understood. If the remote
  backup is missing or unreadable, stop: deleting local records can also
  remove native S3 data in that case. Both this cleanup and S3 cleanup
  remain stopped until the failed history entry clears. See
  [failed deletion history](backup-cleanup.md#preview-stops-that-need-investigation)
  for how entries age out or clear on a backup-container restart. A restart
  clears history; it does not repair backup files.
- **A `broken_local` record.** The report identifies its name and copy number.
  Check that exact entry in the local and remote lists. For an interrupted
  download with a healthy completed remote counterpart, follow the
  [specific-entry removal checks](clickhouse-backups.md#interrupted-base-downloads).
  If it also appears in `local_only`, that procedure does not apply: the files
  may be the remaining record of S3 data. Keep them while investigating. Do
  not remove directories by hand, use force deletion, or clear all broken
  entries at once.

After resolving the cause and any failed history, run with `dryRun: true`
first and check its report before permitting more removals. A successful
backup Job alone does not show that cleanup recovered.

## Recovery and checks

Removing local records does not shorten S3 retention. To make the backup
records available again, download them from S3 onto the recovery machine.
For an incremental backup, download the required full backup's records first,
then the incremental's records;
downloading the incremental alone does not fetch its base. The scheduler also
downloads a missing full backup's records before creating a new incremental.

If downloading records or restoring onto the same installation, pause its
schedule and wait for all backup and cleanup work to finish first. Keep it
paused until recovery and its checks are complete, so scheduled cleanup cannot
remove those records again. The download order above is preparation for
recovery, not a complete or automatic restore procedure. A separate
administrator configuration and a tested restore procedure are still needed;
see [current restore limitations](clickhouse-backups.md#current-limitations).

After enabling removal, check both the logs and database-disk usage on every
copy (`df -h`, `df -i`, and the size of
`/var/lib/clickhouse/disks/backups_s3`). The space recovered depends on how many
old records each copy held. This removes backup records, not live table data,
so database growth still needs separate capacity planning.

The disposable Docker test in
`hack/tests/runtime/test_local_backup_cleanup_runtime.py` checks removal on two
copies, unchanged S3 object contents, recovery of an older incremental after
its local records are gone, and a later incremental using downloaded records.
It uses generated data and the chart's pinned upstream images. It does not
validate a deployment's AWS permissions or network rules.
