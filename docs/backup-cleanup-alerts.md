# Backup cleanup checks and email alerts

Optional cleanup reports run after successful scheduled backups, and a separate
job checks backup health every five minutes. These features do not change the
configured backup schedule.

## Cleanup

```yaml
clickhouse:
  backup:
    cleanup:
      enabled: true
      dryRun: true
      keepLast: 2
      keepDays: 0
```

This reports the backups that would be removed. It keeps the newest two
individual backups, plus any older full backups they require. The report can
therefore keep more than two entries. For real-data deployments, `keepDays: 30`
also keeps every backup inside the last 30 days and its required bases. This
uses dates, so extra or missed runs do not silently shorten the time window.

Stock Altinity clickhouse-backup 2.8.1 leaves native `serialization.json` files
in S3 when deleting backups. This chart therefore supports previews only:
`cleanup.dryRun: false` is rejected, and the Python entry point also rejects
execution before sending a request. Changing the image cannot enable deletion.
See [the stock deletion limitation](backup-cleanup-tool-bug.md).
The tool's own automatic deletion remains off. Keep S3 lifecycle expiration
disabled for both backup prefixes; the chart cannot inspect or change bucket
lifecycle rules.

Both copies must report matching, healthy backup records. Missing bases, busy
copies, or interrupted prior cleanup stop the report and fail the scheduled
job, so the same alert catches cleanup problems after a successful upload.

There is one reported exception: a local entry with `broken metadata.json not found`
or `parse metadata.json error: ...` can be left by an interrupted download. When
the same name has a healthy remote backup, the preview still completes and lists
the affected name and copy under `broken_local`. It does not delete anything or
trust the incomplete entry's dependency. The scheduled Job then fails with a
message distinguishing the completed backup from the local files needing
attention, so the existing failure alert reports it. Until a scheduled Job
finishes without this warning, the monitor also retains the previous successful
Job time. Follow the specific-entry recovery steps in
[the backup instructions](clickhouse-backups.md#interrupted-base-downloads).
An unknown error description or a missing remote counterpart still stops the
preview. Parser messages are never copied into the report.

## Local disk growth and deletion requirements

ClickHouse keeps small files describing the S3 backups on its own data volume,
under `/var/lib/clickhouse/disks/backups_s3/`. A preview does not remove these
files or limit their growth. Check both free space and available file entries
on every copy, using the commands in
[the current limitations](clickhouse-backups.md#current-limitations).

Before enabling backups for production data, retention must be able to remove
selected remote backups and then their local pointer directories on every copy.
Delete dependent backups before their bases, and preserve every base still used
by a kept backup. Verify that remote objects are actually gone and that a kept
backup can still be restored. Keep local deletion in that deliberate cleanup
step, not in the scheduled backup operation. This PR's preview does not meet
that deletion requirement by itself.

## Alerts

Enable `clickhouse_backup_monitoring` in the Terraform module with an
`alert_email`. It supplies the chart's monitoring values and creates the AWS
permissions, CloudWatch alarms, and SNS email subscription. Confirm the AWS
subscription email after applying; notifications cannot arrive before that.
https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html

The monitor can read the backup schedule and job results, and send three
CloudWatch metrics. It has no backup password, S3 access, or permission to change
Kubernetes objects. Its reports contain no table data.
The name `clickhouse-backup-monitor` belongs to this monitor when it is enabled;
the backup software must use a different service account name to keep their
permissions separate.

The last success comes from completed scheduled Jobs kept by Kubernetes, so it
survives monitor and ClickHouse pod restarts. Manual Jobs are excluded even when
created with `kubectl create job --from=cronjob/otel-backup`. If that history is
removed, the monitor cannot prove a recent scheduled success and reports it as
overdue after the initial grace period.

* `BackupJobFailed`: a scheduled backup or cleanup failed. A later successful
  scheduled job clears it.
* `BackupOverdue`: no successful scheduled job for four hours plus 15 minutes,
  or the schedule is paused. Manual test backups do not reset this check. Before
  the first success, the timer starts when the schedule was created.
* `MonitorHealthy`: the monitor can read valid status. CloudWatch also treats
  missing reports as a failure, so losing the monitor or cluster still alerts.
  The alarm evaluates three five-minute periods; AWS's handling of missing
  points affects the exact delivery time.

If the monitor cannot read valid status, it sends only `MonitorHealthy: 0`.
It does not report that backups recovered. The Terraform module sets the two
backup alarms to `treat_missing_data = "ignore"`, keeping their previous state
until valid backup status is available again. Update the module and chart
together; otherwise missing reports can still clear a backup alarm.

The default completion allowance is 15 minutes beyond the scheduled interval.
It is separate from the backup's longer hard timeout. Set it based on measured
backup duration. AWS sends alarm and recovery messages to the configured email.

## Checks after installation

1. Confirm the email subscription. Read the `otel-backup-monitor` job logs and
   verify all three metrics in CloudWatch.
2. After the next scheduled backup, read its cleanup report and compare the
   kept names and required full backups with S3. Dry run must change no files.
3. Test failed and missed scheduled jobs using an isolated test schedule and
   shorter thresholds; verify email delivery and recovery. Unit tests cover
   these decisions, but do not prove AWS email delivery.

With an old full/incremental pair and a new full, `keepLast: 2` keeps all three:
the old incremental still needs the old full. The old pair becomes eligible
after a new incremental completes. The report lists the old incremental before
its full backup; it does not remove either entry. A missing entry in the backup list alone does not prove all
of its S3 files were removed.

Use a separate alert-test schedule and metric dimensions for deliberate
failures; keep the normal backup schedule running. Complete these checks in
each installation before relying on email alerts. Backup deletion remains unavailable.
