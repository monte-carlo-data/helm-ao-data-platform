# Remove older backup records from database disks

Native S3 backups leave small files on each ClickHouse database disk describing
where their data lives in S3. These files grow as backups are created or
downloaded. `clickhouse.backup.localCleanup` can remove older records after a
successful backup, while keeping the completed backups in S3.

This option is off by default. It uses the chart's existing stock Altinity
2.8.1 image and requires no new AWS permissions. Changing this option alone
changes the backup Job scripts and schedule settings, not the ClickHouse or
Keeper installation. Review any other changes included in a chart upgrade
separately.

## Preview first

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
list; `deleted` lists confirmed removals. `delete` is informational and `deleted` is empty in preview mode. Save these logs if you need a longer history than the CronJob's
three successful Jobs.

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

## Enable removal

Test cleanup and recovery with disposable data first, then review the preview
for your installation. Set `localCleanup.dryRun: false` to permit removal on
later successful backup runs. This does not enable S3 deletion; the separate
`cleanup` option remains a preview only.

Every configured database copy must be reachable and idle, and must agree on
the completed S3 backups. Before and after each removal, cleanup checks the
backup lists and their dependencies on every copy. It removes dependent
incremental records before full-backup records and never uses force deletion.
If a check fails, it stops sending deletion requests. Lines starting `Local backup cleanup: ` record `delete_requested` and
`delete_verified` for each record, preserving the result even when a later
removal fails.

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

`timeoutSeconds` must be an integer of at least 60. The Job deadline includes
the backup timeout, each enabled cleanup timeout, and 60 seconds. With both
cleanup options enabled at their defaults, that total is 14,460 seconds,
slightly longer than the four-hour schedule. Choose shorter cleanup timeouts
when combining them so a slow run does not skip the following backup.

## Recovery and checks

Removing local records does not shorten S3 retention. Recovery downloads the
records from S3 onto the recovery machine. For an incremental backup, download
the required full backup's records first, then the incremental's records;
downloading the incremental alone does not fetch its base. The scheduler also
downloads a missing full backup's records before creating a new incremental.

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
