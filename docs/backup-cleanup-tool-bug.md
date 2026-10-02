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

The effect is leftover S3 objects and growing storage use, not corruption of a
retained backup. The tool's manual `delete remote` command has the same problem:
each removal can leave files behind, even when its catalog entry disappears.
Keep this in mind when following the manual-pruning guidance in
[the backup limitations](clickhouse-backups.md#current-limitations).

As of October 2, 2026, 2.8.1 is the latest Altinity release and its `master`
branch still contains the JSON suffix filter. A proposed upstream fix is open:
https://github.com/Altinity/clickhouse-backup/pull/1590
It has not been merged or released. Selecting a different current image does
not resolve this limitation.

The preview covers remote backups only. Local pointer files keep growing on
every copy; version 5.3.0 does not bound that growth. Remote-first deletion,
followed by pruning local pointers on all copies while preserving retained
backup dependencies, still needs to be implemented and tested.
