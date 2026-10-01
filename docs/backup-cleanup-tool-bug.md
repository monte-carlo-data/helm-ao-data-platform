# Why cleanup only previews removals

Altinity clickhouse-backup 2.8.1 skips native `.json` files during S3 deletion,
including ClickHouse `serialization.json` objects, then removes their local
pointers. A removed backup entry therefore does not prove its native files are
gone. The source is:
https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/delete.go

This chart uses the standard image and does not maintain a custom deletion
patch. Retention previews still protect the configured number of backups,
the age window, and every required base. They send only reads and report
`deleted: []`.

The chart rejects enabled cleanup with `dryRun: false`. The Python scheduler
rejects that setting before starting a backup, and `Cleaner.run(execute=True)`
refuses before contacting either API. No deletion implementation is included.
Keep automatic tool retention and S3 lifecycle expiration disabled for the
backup prefixes; neither is a substitute for dependency-aware cleanup.
