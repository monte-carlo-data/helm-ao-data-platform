# ClickHouse backup API and startup fixes

Version `2.8.1-mc.1` builds Altinity clickhouse-backup from commit
`824f1ad5ca669fb9df7e5082f4bed6554fcf678d` with the reviewed local patch:
https://github.com/Altinity/clickhouse-backup/tree/824f1ad5ca669fb9df7e5082f4bed6554fcf678d

`api-security.patch` changes authentication logs to record only the registered
route and refusal status. Submitted usernames, passwords, query strings, and
backup names are excluded, including failed and malformed authentication.
The server refuses empty API or database credentials at startup and rejects
invalid replacement configuration while retaining its last valid configuration.
Configuration parsing errors do not disclose Secret contents in logs or errors.

The patch also accepts a positive configured ClickHouse timeout for embedded
backups. Upstream imposes a four-hour minimum; this chart's default scheduled Job
allows three hours. The connection code already uses the configured duration for
connection and read timeouts. Removing the fixed minimum lets that setting match
the Job deadline; zero and negative durations remain invalid. A timed-out client
does not prove the server operation stopped, so the scheduler still refuses to
retry an uncertain operation.

The chart starts `/usr/local/bin/start-backup.sh`, which waits without making the
database container wait. It requires these readable files:

- `/etc/clickhouse-backup/config.yml`: valid tool configuration with a database password.
- `/etc/clickhouse-backup/password`: a nonempty database password from the same Secret.
- `/etc/clickhouse-backup-api/password`: a nonempty API password.
- `/etc/clickhouse-backup-api/revision`: the exact `BACKUP_PASSWORD_REVISION` value.

Both Secret directories can be mounted read-only. The wrapper resolves the API
Secret's projected generation before reading its two keys, so a file update
cannot combine an old revision with a new password. It validates the configuration
without printing its contents, loads the API password into the child process,
and starts the server as `backup`. Missing or invalid inputs leave the container
running and waiting, with no API listener. Passwords are never written to another
file or passed in process arguments.

The API password is loaded once at process startup. Changing a Secret does not
change the running API password. Rotate it using the chart's documented pause,
revision change, and controlled restart procedure; do not assume live reload.

Build and test from the repository root:

```sh
docker build --platform=linux/amd64 \
  -t ao-clickhouse-backup:2.8.1-mc.1 images/clickhouse-backup
BACKUP_TEST_IMAGE=ao-clickhouse-backup:2.8.1-mc.1 \
  python3 images/clickhouse-backup/test_runtime.py
```

The Docker build checks the source archive, applies the patch, and runs Go tests
for header/query authentication logs, empty credentials, rejected configuration
reloads, and timeout validation. The runtime tests use dummy passwords, temporary
Docker containers, and an isolated local network. They test the real API against
a temporary ClickHouse instance and exercise startup with missing, empty,
mismatched, and projected Secret files. They use no AWS or Kubernetes access and
remove only the Docker resources they create.

Use the published image's exact digest in chart values. A different patch must
receive a new version marker; never publish different code under a used marker.
