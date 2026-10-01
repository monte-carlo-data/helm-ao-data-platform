# Check scheduled ClickHouse backups

Use a cluster where backups are installed and its credentials are available to
`kubectl`. These examples do not need Terraform. The copy-switch test creates one
extra incremental backup in S3; it leaves both ClickHouse copies and the normal
schedule running. Replace the placeholders and set the replica count to match
`clickhouse.replicasCount`:

```bash
KUBE_CONTEXT="<your-context>"
NAMESPACE="<release-namespace>"
REPLICA_COUNT=2
```

The names below use the chart's `otel` ClickHouse installation and cluster.
When upgrading from a shared backup credential, first complete the
[two paused migration steps](clickhouse-backups.md#upgrade-an-existing-shared-backup-user),
including `hack/check-backup-upgrade.py`, before resuming this verification flow.
The access checks work with any replica count. The copy-switch helper requires
**exactly two replicas** and an idle, **unsuspended** CronJob with schedule
`0 */4 * * *`, UTC timezone, and `concurrencyPolicy: Forbid`. Finish the initial
Secret, replica-health, and access checks while the schedule is paused; then
unsuspend it through your normal deployment before checking scheduled runs and
using the helper. A suspended CronJob or a custom schedule is refused.

## Check scheduled runs

```bash
kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get jobs \
  --sort-by=.metadata.creationTimestamp
kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" logs \
  -l app.kubernetes.io/component=clickhouse-backup-job \
  -c backup --prefix --tail=50
```

A successful job has `1/1` completions and logs `Verified completed full backup
... in S3.` or `Verified completed incremental backup ... in S3.` Record the job
name and backup name. A folder in S3 or a changed last-scheduled time only shows
that work started. The scheduler checks replica freshness before submission; the completed
catalog check still does not prove that restoring the data will succeed.

The first successful run each UTC day is full. Later runs that day are
incremental. A manual test does not replace checking that the clock starts a
successful incremental run.

## Check that backup controls reject other callers

Inside each ClickHouse copy, make a request without a password. Every command
must report `401 Unauthorized`; a nonzero exit code is expected. Do not test with
a real incorrect password, and use the required patched helper image.

```bash
for ((copy=0; copy<REPLICA_COUNT; copy++)); do
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec \
    "chi-otel-otel-0-${copy}-0" -c clickhouse -- \
    wget --server-response --output-document=/dev/null --timeout=5 --tries=1 \
    http://127.0.0.1:7171/backup/list/remote
done
```

Use the existing Keeper pod as a caller that is not a backup job. First check
ordinary ClickHouse access, then the backup controls. For every copy, `/ping`
must return `200 OK`, while port 7171 must time out. This shows that the network
rule blocks this caller, rather than the database simply being unavailable.

```bash
for ((copy=0; copy<REPLICA_COUNT; copy++)); do
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec \
    chk-otel-keeper-0-0-0 -- \
    wget --server-response --output-document=/dev/null --timeout=5 --tries=1 \
    "http://chi-otel-otel-0-${copy}:8123/ping"
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec \
    chk-otel-keeper-0-0-0 -- \
    wget --server-response --output-document=/dev/null --timeout=5 --tries=1 \
    "http://otel-backup-${copy}:7171/backup/list/remote"
done
```

## Test switching to the second copy

Run from this repository with Python 3 and `kubectl`. Use the name of a successful
full backup made by copy 0 **today in UTC**, taken from its job log above. The
helper checks that this is still the full backup the scheduler would choose and
that copy 1 has not already downloaded its supporting files. This lets the test
prove that copy 1 can fetch those files itself.

Allow at least 15 minutes after the last scheduled start and enough time to
finish at least 15 minutes before the next. The helper refuses to start outside
that window or while another backup job is pending or running.

```bash
BASE_FULL="replace-with-the-full-backup-name"
TEST_JOB="backup-copy1-$(date -u +%Y%m%d%H%M%S)"
EVIDENCE_DIR="$(mktemp -d /tmp/backup-test.XXXXXX)"

python3 hack/verify-backup-failover.py \
  --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" --cronjob otel-backup \
  --expected-full "$BASE_FULL" --job-name "$TEST_JOB" \
  --output-dir "$EVIDENCE_DIR"
```

Without `--run`, the helper only prepares a Job file locally. Check its target
cluster and backup name, then repeat the command with `--run` to create the test
job and collect its result:

```bash
python3 hack/verify-backup-failover.py \
  --context "$KUBE_CONTEXT" --namespace "$NAMESPACE" --cronjob otel-backup \
  --expected-full "$BASE_FULL" --job-name "$TEST_JOB" \
  --output-dir "$EVIDENCE_DIR" --run
```

The job uses the installed scheduler and password without copying the password
to your terminal. It first checks the real copies, then gives only this test run
an unreachable address for copy 0. Success requires selecting copy 1, creating
exactly one backup, and recording the expected full backup as its dependency.
The regular CronJob is unchanged. A successful result prints `"status": "passed"`
and saves the job name, backup names, timestamps, and checks in an `.evidence.json`
file inside the chosen directory.

If the test fails or stops waiting, inspect the saved result and job logs before
trying again. A backup may still be running. Keep the same job name; choosing a
new one could start another backup. The helper does not delete jobs or backup files.

Record the date, tested chart commit, job name, new backup name, full backup it
uses, and access-check results in your change record. Do not mark the checks
complete until the scheduled incremental run also succeeds. These checks do not
test retention or prove that backups can restore data; see the
[current limitations](clickhouse-backups.md#current-limitations).
