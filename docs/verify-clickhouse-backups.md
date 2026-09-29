# Check backups on the dev cluster

These examples target the separate `ao1297-zhongying` test cluster. They do not
need Terraform. The copy-switch test creates one extra incremental backup in S3;
it leaves both ClickHouse copies and the normal schedule running.

```bash
export AWS_PROFILE=ao1297
export AWS_REGION=us-west-1
export KUBECONFIG="$HOME/.kube/ao1297-config"
```

## Check scheduled runs

```bash
kubectl --context ao1297-zhongying -n montecarlo get jobs \
  --sort-by=.metadata.creationTimestamp
kubectl --context ao1297-zhongying -n montecarlo logs \
  -l app.kubernetes.io/component=clickhouse-backup-job \
  -c backup --prefix --tail=50
```

A successful job has `1/1` completions and logs `Verified completed full backup
... in S3.` or `Verified completed incremental backup ... in S3.` Record the job
name and backup name. A folder in S3 or a changed last-scheduled time only shows
that work started.

The first successful run each UTC day is full. Later runs that day are
incremental. A manual test does not replace checking that the clock starts a
successful incremental run.

## Check that backup controls reject other callers

Inside each ClickHouse copy, make a request without a password. Both commands
must report `401 Unauthorized`; a nonzero exit code is expected.

```bash
for copy in 0 1; do
  kubectl --context ao1297-zhongying -n montecarlo exec \
    "chi-otel-otel-0-${copy}-0" -c clickhouse -- \
    wget --server-response --output-document=/dev/null --timeout=5 --tries=1 \
    http://127.0.0.1:7171/backup/list/remote
done
```

Use the existing Keeper pod as a caller that is not a backup job. First check
ordinary ClickHouse access, then the backup controls. For both copies, `/ping`
must return `200 OK`, while port 7171 must time out. This shows that the network
rule blocks this caller, rather than the database simply being unavailable.

```bash
for copy in 0 1; do
  kubectl --context ao1297-zhongying -n montecarlo exec \
    chk-otel-keeper-0-0-0 -- \
    wget --server-response --output-document=/dev/null --timeout=5 --tries=1 \
    "http://chi-otel-otel-0-${copy}:8123/ping"
  kubectl --context ao1297-zhongying -n montecarlo exec \
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
AO1299_BASE="replace-with-the-full-backup-name"
AO1299_TEST_JOB="ao1299-copy1-$(date -u +%Y%m%d%H%M%S)"
AO1299_EVIDENCE="$(mktemp -d /tmp/ao1299-backup-test.XXXXXX)"

python3 hack/verify-backup-failover.py \
  --context ao1297-zhongying --namespace montecarlo --cronjob otel-backup \
  --expected-full "$AO1299_BASE" --job-name "$AO1299_TEST_JOB" \
  --output-dir "$AO1299_EVIDENCE"
```

Without `--run`, the helper only prepares a Job file locally. Check its target
cluster and backup name, then repeat the command with `--run` to create the test
job and collect its result:

```bash
python3 hack/verify-backup-failover.py \
  --context ao1297-zhongying --namespace montecarlo --cronjob otel-backup \
  --expected-full "$AO1299_BASE" --job-name "$AO1299_TEST_JOB" \
  --output-dir "$AO1299_EVIDENCE" --run
```

The job uses the installed scheduler and password without copying the password
to your terminal. It first checks the real copies, then gives only this test run
an unreachable address for copy 0. Success requires selecting copy 1, creating
exactly one backup, and recording the expected full backup as its dependency.
The regular CronJob is unchanged. A successful result prints `"status": "passed"`
and saves the job name, backup names, timestamps, and checks in an `.evidence.json`
file inside the chosen directory.

If the test fails or stops waiting, inspect the saved result and job logs before trying
again. A backup may still be running. Keep the same job name; choosing a new one
could start another backup. The helper does not delete jobs or backup files.

Record the date, tested chart commit, job name, new backup name, full backup it
uses, and access-check results in AO-1299. Keep the issue open until the scheduled
incremental run also succeeds. Retention tests belong to AO-1300; proving that
backups can restore data belongs to AO-1301.
