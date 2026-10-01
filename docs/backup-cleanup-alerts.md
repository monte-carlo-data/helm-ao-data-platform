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

Both copies must report the fixed version before cleanup can delete anything;
the script checks again before each deletion. See the fix and test evidence in
[backup-cleanup-tool-bug.md](backup-cleanup-tool-bug.md). The tool's own automatic
deletion remains off. Keep S3 lifecycle expiration disabled for both backup
prefixes; the chart cannot inspect or change bucket lifecycle rules.

Both copies must report matching, healthy backup records. Missing bases, busy
copies, or interrupted prior cleanup stop the report and fail the scheduled
job, so the same alert catches cleanup problems after a successful upload.

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
