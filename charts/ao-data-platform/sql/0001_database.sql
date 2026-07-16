-- Clustered-only DDL: every statement in sql/*.sql runs ON CLUSTER '{cluster}' so the
-- single schema Job propagates DDL to every replica via Keeper's distributed DDL queue,
-- and every storage engine is a path-less Replicated* variant that resolves its ZooKeeper
-- path through the server-level default_replica_path / default_replica_name macros (see
-- templates/clickhouse-installation.yaml). Dev and prod share this one code path — a
-- single-replica install is just clickhouse.replicasCount: 1 (+ keeper.replicasCount: 1),
-- not a different engine mode. {cluster} is an operator-injected macro (cluster "otel").
CREATE DATABASE IF NOT EXISTS otel_traces ON CLUSTER '{cluster}';
