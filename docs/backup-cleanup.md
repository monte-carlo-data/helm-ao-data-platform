# Backup cleanup previews

Optional cleanup reports run after verified scheduled backups, in the same Job.
They do not change the backup schedule or delete files. The chart does not add
an alerting system.

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

## What the report keeps

The preview keeps the newest `keepLast` individual backups, plus every older
backup they require. `keepDays` also keeps backups inside the last N days and
their bases; zero disables that age window. For a 30-day window, set
`keepDays: 30`. Extra or missed runs do not shorten that window.

For example, with an old full/incremental pair and a new full, `keepLast: 2`
keeps all three: the old incremental still needs the old full. After a new
incremental completes, the old pair becomes eligible. The report lists the old
incremental before its full backup under `delete`; it does not remove either.
Unrelated remote backups and any bases they use are always preserved.

Stock Altinity clickhouse-backup 2.8.1 leaves native `serialization.json` files
in S3 when deleting backups. This chart supports previews only:
`cleanup.dryRun: false` is rejected before a backup starts, and the preview
program cannot send write requests. Changing the image cannot enable deletion.
See [the deletion limitation](backup-cleanup-tool-bug.md). Keep the tool's
automatic deletion and S3 lifecycle expiration disabled for both backup prefixes;
the chart cannot inspect or change bucket lifecycle rules.

## Reading the result

A completed report appears in the backup Job log as `Backup cleanup: {JSON}`.
It contains `kept`, proposed `delete` names, and `deleted: []`. Both copies must
be reachable and report matching remote records. Missing bases, broken remote
metadata, busy copies, interrupted deletions, or records changing during the
check stop the preview. These cases cannot produce a trustworthy removal list.

Two kinds of local files are reported separately without stopping the preview:

- `broken_local`: entries described by the tool as `broken metadata.json not
  found` or `parse metadata.json error: ...`. A healthy remote counterpart is
  sufficient; scheduler-owned names may also be local-only. The report never
  trusts their missing dependencies or copies parser details into logs.
- `local_only`: scheduler-owned entries without a remote counterpart. These can
  remain after interrupted uploads or remote-first removal. The report does not
  treat them as remote backups or propose deleting them.

Each entry identifies the copy and backup name. A separate warning calls out
these files. Inspect them using the specific-entry checks in
[interrupted base downloads](clickhouse-backups.md#interrupted-base-downloads);
that procedure applies only when a healthy remote counterpart exists. Do not
use it for local-only entries.

Other preview failures log `Backup cleanup preview stopped: ...`. Because the
backup has already been verified, the Job still succeeds. Read that line and
the report separately from backup Job status. Do not rerun a completed backup
just because its preview failed. This chart does not send preview alerts.

The Job deadline is `schedule.timeoutSeconds + cleanup.timeoutSeconds + 60`.
Keep it below the scheduled interval, with time for startup and scheduling
delays. Otherwise an unfinished Job can cause the next scheduled run to be
skipped. Defaults allow 10800 seconds for backup and 1800 for preview, with a
60-second allowance, on a four-hour schedule.

## Local disk growth and future deletion

ClickHouse keeps files describing S3 backups on its own data volume, under
`/var/lib/clickhouse/disks/backups_s3/`. They accumulate on every copy that creates
or downloads backup metadata. This preview does not limit their growth. Check
free space and available file entries on every copy using the commands in
[the current limitations](clickhouse-backups.md#current-limitations).

Deletion remains separate work: remove selected remote backups first, then
prune their local pointer directories on every copy. Remove dependents before
bases and preserve every backup still needed by a retained backup. Verify that
remote objects are gone and that a retained backup can still be restored.
Reporting local-only files supports the intermediate state between those steps;
it does not implement either step.

## Checks after installation

1. After a scheduled backup, read its cleanup report. Compare `kept`, `delete`,
   and required bases against the remote catalog and S3.
2. Confirm that no backup files were removed. A missing catalog entry alone does
   not prove all of its S3 files are gone.
3. Review `broken_local`, `local_only`, and any `Backup cleanup preview stopped:`
   line. A successful Job proves the backup, not successful cleanup reporting.
