# Backup cleanup previews

Optional cleanup reports run after verified scheduled backups, in the same Job.
They do not change the backup schedule or delete files. The chart does not add
an alerting system. Cleanup requires exactly two ClickHouse copies.

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

The defaults (`keepLast: 2`, `keepDays: 0`) keep two individual backups and their
required bases, not two days. From the second successful run of a new UTC day,
the previous day's chain can appear under `delete` unless another kept backup
still needs it. Use `keepDays: 30` for a 30-day production window.

When cleanup is enabled, `cleanup.dryRun: false` is rejected before a backup
starts. Changing the image cannot enable deletion. See
[why deletion is unavailable](#why-deletion-is-unavailable).

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
Backup cleanup: {"mode":"dry-run","keep_last":2,"keep_days":0,"kept_count":2,"delete_count":1,"deleted_count":0,"broken_local_count":0,"local_only_count":0}
Backup cleanup entry: {"list":"kept","name":"ao-otel-full-20260102T000000Z-4567abcd"}
Backup cleanup entry: {"list":"kept","name":"ao-otel-incremental-20260102T040000Z-89abcdef"}
Backup cleanup entry: {"list":"delete","name":"ao-otel-full-20260101T000000Z-0123abcd"}
```

`delete` entries are proposals in removal order; `deleted_count` is always zero.
Every name is written separately, so the growing candidate list does not make
one increasingly long JSON line. Keep the summary and all entry lines to retain
the full report. Local entries also include `copy`: `0` is the first ClickHouse
replica and `1` is the second.

Both copies must be reachable and report matching remote records. Missing
bases, broken remote metadata, busy copies, interrupted deletions, or records
changing during the check stop the preview. These cases cannot produce a
trustworthy removal list.

Preview failures log `Backup cleanup preview stopped: ...`. Because the backup
has already been verified, the Job still succeeds. Read that line and the
report separately from backup Job status. Do not rerun a completed backup just
because its preview failed. This chart does not send preview alerts.

The Job deadline is `schedule.timeoutSeconds + cleanup.timeoutSeconds + 60`.
Keep it below the scheduled interval, with time for startup and scheduling
delays. Otherwise an unfinished Job can cause the next scheduled run to be
skipped. Defaults allow 10800 seconds for backup and 1800 for preview, with a
60-second allowance, on a four-hour schedule.

## Local files in the report

Two kinds of local files are reported without stopping the preview:

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
  repairing the upload can restore a valid remote entry. If abandoning that
  incomplete upload, first verify its exact name, a matching complete local
  entry on the chosen copy, and that no retained backup requires it. The tool's
  `delete remote <name>` on that copy
  removes the broken catalog prefix while leaving native objects and local
  pointers; see [the deletion behavior below](#why-deletion-is-unavailable).
  A remaining scheduler-created entry then appears as `local_only`; an unrelated
  name still stops the preview, as described below. This removes an incomplete
  catalog entry, not the backup's S3 data. Do not apply this procedure to an
  unverified or healthy remote backup, or force past a dependency error.
- **A failed or cancelled deletion in API history.** The preview stops while
  either copy's history contains a failed or cancelled `delete` command, even
  if a later attempt succeeded. Inspect the affected backup and any partial
  removal first. Inspection alone does not clear the history. The tool keeps
  up to 1,000 finished actions by default (`general.status_history_size`);
  the old entry disappears when it ages out or the backup container restarts.
  Restarting clears in-memory history but does not repair backup files.
- **An unrelated local backup with no remote entry.** A manually created local
  backup outside this scheduler's names stops every preview until it has a
  valid remote counterpart or is deliberately removed. Decide whether that
  backup should be uploaded or retained. Do not delete it merely to silence
  the preview; local deletion can also remove native S3 objects.

After addressing the cause, check the next preview's logs. Job success alone
does not show that the preview recovered.

## Why deletion is unavailable

This chart does not delete backups. It rejects enabled cleanup with
`dryRun: false`, and the preview program refuses execution before contacting
either backup API. Keep the tool's automatic retention disabled and S3
lifecycle expiration disabled for both backup prefixes. The chart cannot
inspect or change bucket lifecycle rules.

The pinned Altinity clickhouse-backup 2.8.1 image has two deletion behaviors that
matter for this chart:

- On a copy holding a matching local backup, `delete remote` skips native S3
  object cleanup and removes only the remote catalog prefix. This includes the
  creating copy and a copy that downloaded the backup's pointers. Removing the
  catalog entry therefore does not show that native data has been removed.
- Once the remote entry is gone, `delete local` uses that copy's pointer files
  to delete native S3 objects. Version 2.8.1 skips pointers whose names end in
  `.json`, including native `serialization.json` files, and then removes their
  local directories. The native JSON objects are left behind. A remote delete
  from a copy without a matching local entry attempts native cleanup itself,
  but has the same JSON-suffix exclusion.

The effect is leftover S3 objects and growing storage use. This bug does not
corrupt retained backups when deletion stays off. Source and upstream fix:

https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/delete.go

https://github.com/Altinity/clickhouse-backup/pull/1590

Deletion remains unavailable until this chart adopts a tested upstream release
with the fix and implements cleanup. Merging the upstream change alone does
not update this chart's pinned image or remove its deletion guards.

The tool's **remote** retention already preserves required full backups by
following `RequiredBackup` links. It still differs from this preview: it counts
all backups under its configured remote path, including manual backups, and
has no age window. It runs during upload, so some retention errors can fail an
otherwise completed upload; some catalog-removal errors are only logged.
It also uses the deletion paths described above. Its upload dry run returns
before retention and does not preview that retention plan. S3 lifecycle expiry
does not inspect backup dependencies at all. Sources:

https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/storage/compression.go

https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/upload.go

## Local disk growth and future deletion

ClickHouse keeps small files describing S3 object locations on its own data
volume, under `/var/lib/clickhouse/disks/backups_s3/`. These pointers accumulate
on every copy that creates or downloads backup metadata. This preview does not
limit their growth. Check free space and available file entries on every copy
using the commands in [the current limitations](clickhouse-backups.md#current-limitations).

Future automatic deletion must execute this preview's ordered `delete` list
through per-backup delete calls, preserving the same age window, manual backups,
and required bases. Turning on the tool's count-based retention would choose a
different set. Remove dependent backups before their bases.

That work must account for both remote catalog removal and local pointers on
every copy. As described above, the copy issuing `delete remote` determines
whether that step attempts native-object cleanup. If it has a matching local
entry, native cleanup waits for local deletion; skipping that step leaves S3
data without a remote catalog entry. Cleanup must handle an unavailable copy
and interrupted steps, verify native objects are gone, and verify a retained
backup can still be restored. The preview implements none of those deletions.

## Checks after installation

1. After a scheduled backup, read its summary and all entry lines. Compare
   `kept`, ordered `delete`, and required bases against the remote catalog and S3.
2. Confirm that no backup files were removed. A missing catalog entry alone does
   not prove all of its S3 files are gone.
3. Review `broken_local`, `local_only`, and any `Backup cleanup preview stopped:`
   line. A successful Job proves the backup, not successful cleanup reporting.
