# Backup cleanup

Optional cleanup runs after verified scheduled backups, in the same Job.
It is disabled by default and starts in preview mode. It does not change the backup schedule. The chart does not add
an alerting system. Cleanup checks every configured ClickHouse copy and supports
one or more copies.

This page covers `clickhouse.backup.cleanup`, the S3 preview and whole-set deletion option. The
separate [database-disk record cleanup](backup-local-cleanup.md) option can
remove older local records while preserving their completed S3 backups.

`daily-weekly` is the default policy. Preview and deletion use the same list
of complete sets. Review that preview first, then change only `dryRun` to
`false` to permit deletion with the verified Altinity 2.8.2 image. Preview is
recommended, not an enforced approval step.

This replaces the earlier plan to execute the count-and-age preview's `delete`
list. Count-and-age is still available only as an explicit preview setting;
its report does **not** show what daily-weekly deletion will remove. Existing
installs without an explicit policy will see a different preview after this
upgrade, but no deletion is enabled automatically. If an installation tool
supplies `count-and-age` explicitly, first change it to `daily-weekly` while
keeping `dryRun: true`, review the new report, then enable deletion separately.

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
  same-day incrementals, and no extra protected older sets, the 7/30 settings
  retain 11 full sets, or 12 when cleanup runs on Monday. Missed days and protected
  dependencies can change that count. Older recovery points are spaced farther
  apart than recent daily history.

These rules do not change daily full backups or four-hour incrementals. Broken
local records stop this policy because their missing metadata could hide a
needed base. An unavailable copy also stops cleanup.

## Existing count-and-age preview

Set `retentionPolicy: count-and-age` explicitly to keep the previous preview.
It keeps the newest `keepLast` individual backups and their required bases.
`keepDays` also keeps every backup inside the last N days and its bases; zero
disables that age window. For example, `keepDays: 30` previews keeping every
backup for that entire window, unlike daily-weekly's fewer older recovery
points. The old defaults (`keepLast: 2`, `keepDays: 0`) keep two backups and their
bases, not two days.

This policy cannot delete files. To prepare deletion, switch to a daily-weekly
preview and inspect that report. Do not use a count-and-age report to approve
a daily-weekly deletion.

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
   otherwise leave native data behind, even with the fixed software. If the
   run stops after removing local records, the completed S3 backup remains
   intact for recovery and the next cleanup run. Removing S3 first would
   instead leave local-only records that keep their base set and need manual
   cleanup. This is why the local-first order is intentional.
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
example, this count-and-age report keeps a full backup and its incremental, and proposes
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

`delete` entries list the original candidates in removal order, and `delete_count`
counts those candidates. In deletion mode, `deleted` and `deleted_count` record
only S3 removals whose follow-up checks completed. `deleted_count` is zero in
preview mode. Per-operation events record local and remote deletion attempts
and verified removals. If a password check or connection failure proves a request
was never sent, `delete_not_sent` records that outcome; it is not listed as an
unverified deletion. `delete_requested` is written only after the time check
allows a request to be sent.

If a later deletion or check fails, the report keeps every earlier verified S3
removal and reports `completed: false` with `stop_reason: error`. The stop line
includes the reason and the number of verified S3 removals. A clean pause before
another deletion reports `stop_reason: time_limit` and also keeps that progress.
Check these fields even when `deleted_count` is greater than zero.

When an attempted deletion has not been verified, the report also includes
`unverified_deletions` entries and `unverified_deletions_count`. Each entry has
`copy`, `location`, `name`, and `response_status`:

- `response_status: success` means the server returned the expected success
  response, but the follow-up checks did not finish successfully.
- `response_status: unknown` means the expected success response was not
  received. The deletion may still be running.
- `location: local` concerns the named copy's database-disk records. It does
  not mean the S3 backup was removed. `location: remote` concerns an attempted
  S3 removal.

These entries need investigation and never increase `deleted_count`. Use
`location` and `copy` to inspect the affected records and remote backup before
more manual work. Cleanup sends no further deletion after an unverified result
and never retries that request within the run.

Every name is written separately, so the growing candidate list does not make
one increasingly long JSON line. Keep the summary and all entry lines to retain
the full report. Where an entry includes `copy`, `0` is the first ClickHouse
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

Before each local or S3 deletion under `daily-weekly`, cleanup reserves a fixed
300-second delete timeout, five 30-second follow-up reads per copy, and another
30 seconds for processing. These requests need
`300 + (5 * copies + 1) * 30` seconds, plus time for the password revision checks
that run before each request. With two copies, those checks add 220 seconds to
the 630-second request allowance, so more than 850 seconds must remain before
starting a deletion. Earlier checks also use the cleanup budget, so configure
more time than this reserve; the default is 1800 seconds.

| ClickHouse copies | Seconds that must remain before each deletion |
| --- | ---: |
| 1 | more than 540 |
| 2 | more than 850 |
| 3 | more than 1260 |
| 4 | more than 1770 |
| 5 | more than 2380 |

The formula including password checks is `300 + (5*N + 1)*30 + (5*N + 1)*10*N`,
where `N` is the copy count. With four copies, 1800 seconds leaves little time
for the earlier checks; with five or more it cannot start any deletion. Increase
`cleanup.timeoutSeconds` above the reserve plus the time observed for those
checks. Otherwise repeated green Jobs can leave the entire backlog untouched.

If too little time remains to start another deletion, cleanup sends no further
deletion and reports `completed: false` with `stop_reason: time_limit`. A large
backlog can take several runs, but repeated runs with zero verified removals
need a longer budget, not just more waiting. The first daily-weekly preview
read needs more than `30 + 10*N` seconds; a 60-second budget is usable only with
one or two copies and is not enough to guarantee a full preview. No deletion
is attempted in a preview. A request timeout or unfinished checks after deletion
still need investigation. If initial reads fail before a plan exists, only the
stop message is available and no deletion has been sent.

The Job deadline is `schedule.timeoutSeconds + cleanup.timeoutSeconds + 60`,
plus `localCleanup.timeoutSeconds` when database-disk cleanup is also enabled.
Keep it below the scheduled interval, with time for startup and scheduling
delays. Otherwise an unfinished Job delays the next scheduled run. It can
still start within the configured start deadline (900 seconds, or 15 minutes,
by default); after that the missed run is skipped. Defaults allow 10800 seconds
for backup and 1800 for S3 cleanup, with a 60-second allowance, on a four-hour
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
  See the interrupted-upload procedure below before deciding whether to keep
  or remove a readable entry.

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
  `metadata.json`, so an interrupted upload can leave `broken (can't stat
  metadata.json)`. Confirm this is incomplete metadata rather than failed S3
  access. Keep the complete local record on the copy that created the backup.
  See [recover an interrupted upload](#recover-an-interrupted-upload) for
  resuming it or deliberately abandoning it. Both local and S3 cleanup stop
  until the incomplete remote entry is repaired or removed.
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

## Recover an interrupted upload

Pause the schedule and wait for operations to finish on every copy first. Check
that the incomplete remote entry belongs to the failed upload and that the copy
which created it still has a complete, readable local record. Do not clear that
record: it describes the native objects and, for an incremental, its required
base. Check that the base is complete and retained before resuming.

Use the authenticated backup API on the creating copy to resume the upload:

```text
POST /backup/upload/<backup-name>?resume=true
```

The equivalent command with a working CLI configuration is:

```sh
clickhouse-backup --config <configuration-file> upload --resume <backup-name>
```

A plain `upload` refuses an existing remote entry. In Altinity 2.8.2 the per-request
resume flag works for this upload even with `use_resumable_state: false`. The CLI
help says resume is ignored for embedded backups, but that statement does not
match this upload path. The upload uses the local record's base, and finishes
the remote `.backup` and `metadata.json` files. Wait for the operation to succeed,
then check every copy's remote list: the entry must be complete, and an
incremental must still name the same required backup. Run a new preview before
allowing automatic removal again.

If the backup must instead be abandoned, first confirm that no retained backup
needs it and record its exact name and creating copy. With its complete local
record still present, use that copy's stock `delete remote <backup-name>` command
(or `POST /backup/delete/remote/<backup-name>`). For an incomplete remote entry,
this removes the broken catalog without removing native data. Confirm the
remote entry is absent. The local record is now `local_only`: it consumes disk
space and, for an incremental, keeps its base's entire set out of automatic
cleanup until the record is removed.

To finish that deliberate abandonment, use `delete local <backup-name>` on the
creating copy (or `POST /backup/delete/local/<backup-name>`). That record's
pointers allow the stock tool to remove the abandoned native objects once the
remote entry is gone. Check both lists and the expected S3 files afterward.
Do not use force deletion or remove directories by hand. If the creating copy's
local record is missing or broken, stop and investigate instead: deleting the
remote catalog can then leave native files without the records needed to
remove them. A failed deletion history entry must also be investigated and
cleared as described above before cleanup can continue.

The disposable Docker tests remove the final catalog files to reproduce an
incomplete upload. With stock 2.8.2, resuming full and incremental uploads
recreates those files, preserves the required base and native objects, and
allows both backups to be restored on a separate copy. The abandonment test
also checks that remote deletion leaves native data alone, then local deletion
removes the abandoned incremental's objects without changing its full base.

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
