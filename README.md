# ao-data-platform

Helm chart for the Monte Carlo data plane for Agent Observability.

## Chart

### ao-data-platform

Deploys the observability data plane:
- Altinity ClickHouse Operator + ClickHouse instance
- OpenTelemetry Collector (traces pipeline)
- Schema migration Job (a plain `Job`, recreated per release revision, that runs on every install and upgrade)

The ClickHouse instance ships with production hardening: a capped memory ceiling (80% of the cgroup limit), `notice`-level logging, 7-day TTLs on system log tables, a startup probe with a 5-minute warmup window, and a `PodDisruptionBudget` with `minAvailable: 1`. The PDB is a circuit breaker for cluster automation, not an HA mechanism — voluntary evictions (node drains, EKS upgrades, managed node group AMI bumps) return 429 and the initiator has to handle the failure explicitly.

> **Upgrading an existing cluster:** the system-log TTLs only take effect when ClickHouse first *creates* each `system.*_log` table. If those tables already exist (any cluster that was running before this chart version), a restart will not apply the TTLs retroactively — `SHOW CREATE TABLE system.query_log` will show no TTL, which is expected, not a failure. To apply them on an existing cluster, run a one-time `ALTER TABLE system.<log> MODIFY TTL event_date + INTERVAL 7 DAY` per log table (or drop the tables and let ClickHouse recreate them on next flush).

> **Telemetry retention** is controlled by `clickhouse.ttlDays` (default 30 days), covering the raw traces, the trace-id timestamp index, and the normalized spans. Unlike the system-log TTLs above, the schema migration Job re-applies this on every install and upgrade (`ALTER TABLE … MODIFY TTL`), so changing the value updates existing tables — no manual ALTER needed. The Job sets `materialize_ttl_after_modify = 0`, so the change is metadata-only: *raising* the TTL takes effect immediately, while *lowering* it purges newly-expired rows lazily on the next background merge rather than at once. To force an immediate purge after lowering, run `ALTER TABLE otel_traces.<table> MATERIALIZE TTL` per affected table. The `llm_*` worker queue tables (`llm_inputs`, `llm_results`, `llm_batches`) are LLM-pipeline state rather than telemetry and are **not** governed by this value — they keep a fixed 30-day TTL defined in their SQL.

## Prerequisites

- Helm 3
- A Kubernetes cluster (k3s for local dev, EKS for AWS)
- [cert-manager](https://cert-manager.io/) installed in the cluster (for TLS, enabled by default)
- [External Secrets Operator](https://external-secrets.io/) installed in the cluster
- A `SecretStore` or `ClusterSecretStore` configured to access your secrets backend (AWS Secrets Manager, Fake provider for local dev, etc.)

The chart does not ship a default `llmWorker.image` — supply your own (`llmWorker.image.repository` / `llmWorker.image.tag`) or the `llm-worker` Deployment will not start. The public worker image is published as `montecarlodata/ao-llm-worker`.

## Local Development (k3s)

### 1. Create a k3d cluster

```bash
k3d cluster create ao-playground
```

### 2. Install cert-manager

Internal TLS (collector↔ClickHouse) is always enabled and requires cert-manager:

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set crds.enabled=true \
  --wait
```

### 3. Install External Secrets Operator

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --set installCRDs=true \
  --wait
```

### 4. Create a Fake ClusterSecretStore

For local development, use the ESO Fake provider. The chart always provisions the `otel`,
`schema_owner`, `llm_worker`, and `monte_carlo` users, so seed a key for each (one shared
dev password is fine here):

```bash
CLICKHOUSE_PASSWORD="$(openssl rand -base64 16)"

kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: fake-secret-store
spec:
  provider:
    fake:
      data:
        - key: clickhouse-otel-password
          value: "${CLICKHOUSE_PASSWORD}"
          version: "v1"
        - key: clickhouse-schema-owner-password
          value: "${CLICKHOUSE_PASSWORD}"
          version: "v1"
        - key: clickhouse-llm-worker-password
          value: "${CLICKHOUSE_PASSWORD}"
          version: "v1"
        - key: clickhouse-monte-carlo-password
          value: "${CLICKHOUSE_PASSWORD}"
          version: "v1"
EOF
```

### 5. Build chart dependencies

```bash
helm dependency build charts/ao-data-platform/
```

### 6. Install the chart

Wire an `ExternalSecret` for each always-provisioned user at the Fake store, and point the
`llm-worker` at a worker image. (`readonly_user` and `admin` are off by default; enable and
wire them the same way under `clickhouse.readonlyUser` / `clickhouse.adminUser` if you need
them.)

```bash
helm upgrade --install ao-data-platform charts/ao-data-platform/ -n montecarlo --create-namespace \
  --set clickhouse.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.externalSecret.remoteRef.key=clickhouse-otel-password \
  --set clickhouse.externalSecret.remoteRef.version=v1 \
  --set clickhouse.schemaOwner.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.schemaOwner.externalSecret.remoteRef.key=clickhouse-schema-owner-password \
  --set clickhouse.schemaOwner.externalSecret.remoteRef.version=v1 \
  --set clickhouse.llmWorkerUser.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.llmWorkerUser.externalSecret.remoteRef.key=clickhouse-llm-worker-password \
  --set clickhouse.llmWorkerUser.externalSecret.remoteRef.version=v1 \
  --set clickhouse.monteCarloUser.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.monteCarloUser.externalSecret.remoteRef.key=clickhouse-monte-carlo-password \
  --set clickhouse.monteCarloUser.externalSecret.remoteRef.version=v1 \
  --set llmWorker.image.repository=montecarlodata/ao-llm-worker \
  --set llmWorker.image.tag=latest
```

### 7. Verify

```bash
# ClickHouse operator
kubectl get pods -n montecarlo -l app.kubernetes.io/name=altinity-clickhouse-operator

# ClickHouse instance
kubectl get chi -n montecarlo

# Schema migration job
kubectl get jobs -n montecarlo

# OTel collector
kubectl get pods -n montecarlo -l app.kubernetes.io/name=opentelemetry-collector

# TLS certificates
kubectl get certificates -n montecarlo

# ExternalSecret status
kubectl get externalsecret -n montecarlo
```

## Deploying to AWS EKS

### Prerequisites

- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) installed in the cluster
- Private subnets tagged with `kubernetes.io/role/internal-elb: 1`
- ACM certificates for the OTel Collector and ClickHouse DNS names
- [External Secrets Operator](https://external-secrets.io/) installed in the cluster
- A `ClusterSecretStore` configured for AWS Secrets Manager
- Secrets in AWS Secrets Manager for the ClickHouse user passwords — `otel`, `schema_owner`, `llm_worker`, `monte_carlo` (always), plus `readonly_user` / `admin` when enabled

Supply environment-specific configuration in your own values file (referred to below as
`my-values.yaml`) and pass it with `-f`. The chart ships only `values.yaml` (defaults); it
does not bundle environment overlays.

### 1. Configure the ExternalSecrets

Point an `ExternalSecret` for each always-provisioned user (`otel`, `schema_owner`,
`llm_worker`, `monte_carlo`) at your AWS Secrets Manager `ClusterSecretStore`. Each user's
`secretStoreRef.name` and `remoteRef.key` are required. (`readonly_user` and `admin` are off
by default — enable and wire them the same way under `clickhouse.readonlyUser` /
`clickhouse.adminUser` if needed.)

```yaml
clickhouse:
  externalSecret:                                  # otel
    secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
    remoteRef: {key: ao/clickhouse-otel-password}  # AWS Secrets Manager secret name
  schemaOwner:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-schema-owner-password}
  llmWorkerUser:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-llm-worker-password}
  monteCarloUser:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-monte-carlo-password}
```

### 2. Configure ACM certificate ARNs

Set your ACM certificate ARNs for the OTel Collector and ClickHouse Services via the
`service.beta.kubernetes.io/aws-load-balancer-ssl-cert` annotation on each Service.

### 3. Configure DNS records

The chart uses [ExternalDNS](https://kubernetes-sigs.github.io/external-dns/) to create DNS
records for the NLBs. Set `clickhouse.hostname` and the
`external-dns.alpha.kubernetes.io/hostname` annotation on the OTel collector Service to your
desired DNS names.

### 4. Install the chart

```bash
helm dependency build charts/ao-data-platform/
helm upgrade --install ao-data-platform charts/ao-data-platform/ -n montecarlo --create-namespace \
  -f my-values.yaml
```

## CI / CD

CircleCI runs on every push:

- **Lint** — `helm lint charts/ao-data-platform` on every branch and on `v*` tag pushes.
- **Publish (dev)** — `dev` branch pushes publish two pre-release artifacts to Docker Hub: `0.0.0-latest` (floating, overwritten every push) and `0.0.0-dev.g<short-sha>` (immutable, one per commit).
- **Publish (release)** — `v*` git tag pushes on `main`-ancestor commits publish the numbered version to Docker Hub.

### Versioning

Two flows, by branch/tag:

- **Dev (continuous):** every push to the `dev` branch publishes a `0.0.0-latest` floating tag and a `0.0.0-dev.g<short-sha>` immutable per-commit tag as pre-releases. The floating tag is for consumers that always want the tip of dev; the per-commit tag preserves history so you can pin or roll back. `0.0.0-` pre-releases are excluded from normal semver version constraints.
- **Release (tag-driven):** to cut a release, push a `v<semver>` git tag (e.g. `v1.5.0`). CI strips the leading `v` and publishes that version. Tags on commits that are not ancestors of `origin/main` are refused at the start of the publish job. `main` branch pushes alone (without a tag) do not publish anything.

The `version:` field in `Chart.yaml` is overridden by CI for dev publishes. For tagged releases, CI enforces that `Chart.yaml` `version:` matches the tag (minus the leading `v`) — bump `Chart.yaml` and merge to `main` before pushing the `v<semver>` tag, or the publish job will refuse.

### Publishing

The chart is published as an OCI artifact to Docker Hub:

```
oci://registry-1.docker.io/montecarlodata/ao-data-platform
```

CI authenticates to Docker Hub with a scoped access token (`DOCKER_LOGIN` / `DOCKER_PASSWORD`) supplied by a publish-only CircleCI context that is not exposed to forked-PR builds.

Pull a published version directly:

```bash
helm pull oci://registry-1.docker.io/montecarlodata/ao-data-platform --version 1.5.0
```

## ClickHouse user model

The chart provisions a least-privilege ClickHouse user per access path. Privileges are enforced
declaratively via config-level grants in the `ClickHouseInstallation` (no SQL-RBAC bootstrap). The
materialized views run under `schema_owner` as their `DEFINER`, so the ingest user needs no access
to the normalized target tables. The stock `default` superuser is removed.

| User | Reads | Writes | Used by | Provisioned |
|------|-------|--------|---------|-------------|
| `schema_owner` | `otel_traces.*` + `system` | full DDL on `otel_traces.*`, `ALTER system.*`, `SYSTEM FLUSH LOGS` | schema-migration Job; the MV `DEFINER` | always |
| `otel` | — | `INSERT` on the telemetry source tables when `clickhouse.otel.restrictGrants=true` (otherwise unrestricted) | OTel collector | always |
| `llm_worker` | `llm_batches`/`llm_inputs`/`llm_results` | `INSERT` on `llm_batches`/`llm_results` | llm-worker Deployment | always |
| `monte_carlo` | reader bundle¹ | `INSERT` on `llm_inputs`/`llm_batches`/`conversation_eval_scores` | Monte Carlo (data-source monitoring + agent observability) | always |
| `readonly_user` | reader bundle¹ | — (`readonly=2`, so JDBC `SET` works) | humans / MCP / JDBC clients | `clickhouse.readonlyUser.enabled=true` |
| `admin` | all | all + user management + `SYSTEM` | break-glass DBA (not service-to-service; loopback-only by default) | `clickhouse.adminUser.enabled=true` |

¹ **reader bundle** = `SELECT` on `otel_traces.*`, `system.tables/parts/query_log`, and
`information_schema.*` — the metadata reads JDBC/MCP clients and Monte Carlo monitoring need. Shared
by `monte_carlo` and `readonly_user`.

Each password-backed user has an ExternalSecret sourcing its password from your secret store (see the
per-user `*.externalSecret` values below). Network *reachability* is typically restricted one layer
up at the load balancer; per-caller CH-user-level network scoping is handled separately.

`hack/verify-deployment.sh` runs its ClickHouse data checks as `readonly_user`, so set
`clickhouse.readonlyUser.enabled=true` to use the script.

## Configuration

| Value | Default | Description |
|-------|---------|-------------|
| `clickhouse.storageSize` | `100Gi` | PVC size for ClickHouse data. |
| `clickhouse.ttlDays` | `30` | Retention in days for the telemetry tables (raw traces, trace-id index, normalized spans). Re-applied on every install/upgrade via `ALTER TABLE … MODIFY TTL`. Does **not** govern the `llm_*` worker queue tables (they keep a fixed TTL). See the telemetry-retention note above. |
| `clickhouse.nodeSelector` | `{}` | Node selector for the ClickHouse pod (wired into the CHI's `podTemplate`) |
| `clickhouse.tolerations` | `[]` | Tolerations for the ClickHouse pod (wired into the CHI's `podTemplate`) |
| `clickhouse.otelSecret` | `ao-clickhouse-otel-credentials` | Name of the K8s Secret (created by ESO) with a `password` key |
| `clickhouse.otelNetworksIp` | `["0.0.0.0/0"]` | CIDR list allowed to authenticate as the `otel` user (default open). Reachability is typically restricted at the load balancer; per-caller CH-level scoping is handled separately. |
| `clickhouse.otel.restrictGrants` | `false` | When `true`, restrict `otel` to `INSERT` on the telemetry source tables (least-privilege ingest). Leave `false` until external readers have been switched to `monte_carlo`. |
| `clickhouse.externalSecret.secretStoreRef.name` | `""` | Name of the `SecretStore` or `ClusterSecretStore` to use (for the `otel` password) |
| `clickhouse.externalSecret.secretStoreRef.kind` | `ClusterSecretStore` | Kind of the secret store reference |
| `clickhouse.externalSecret.remoteRef.key` | `""` | Key in the external secrets backend |
| `clickhouse.externalSecret.remoteRef.property` | `""` | Property within a JSON secret (optional) |
| `clickhouse.externalSecret.remoteRef.version` | `""` | Version of the secret (required for Fake provider) |
| `clickhouse.externalSecret.refreshInterval` | `1h` | How often ESO syncs the secret |
| `clickhouse.schemaOwner.secret` | `ao-clickhouse-schema-owner-credentials` | K8s Secret (ESO) for the always-provisioned `schema_owner` user. |
| `clickhouse.schemaOwner.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `schema_owner`. |
| `clickhouse.schemaOwner.externalSecret.*` | — | ExternalSecret config for `schema_owner` (same shape as `clickhouse.externalSecret.*`). |
| `clickhouse.llmWorkerUser.secret` | `ao-clickhouse-llm-worker-credentials` | K8s Secret (ESO) for the always-provisioned `llm_worker` user. |
| `clickhouse.llmWorkerUser.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `llm_worker`. |
| `clickhouse.llmWorkerUser.externalSecret.*` | — | ExternalSecret config for `llm_worker` (same shape as `clickhouse.externalSecret.*`). |
| `clickhouse.monteCarloUser.secret` | `ao-clickhouse-monte-carlo-credentials` | K8s Secret (ESO) for the always-provisioned `monte_carlo` user. |
| `clickhouse.monteCarloUser.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `monte_carlo`. |
| `clickhouse.monteCarloUser.externalSecret.*` | — | ExternalSecret config for `monte_carlo` (same shape as `clickhouse.externalSecret.*`). |
| `clickhouse.adminUser.enabled` | `false` | When `true`, provision the gated `admin` break-glass superuser (full access + user management + `SYSTEM`) and its ExternalSecret. |
| `clickhouse.adminUser.secret` | `ao-clickhouse-admin-credentials` | K8s Secret (ESO) for `admin` (used when enabled). |
| `clickhouse.adminUser.networksIp` | `["127.0.0.1","::1"]` | CIDRs allowed to authenticate as `admin`. Defaults to loopback only, so `admin` is reachable only by exec-ing into the ClickHouse pod; override only if you need remote admin. |
| `clickhouse.adminUser.externalSecret.*` | — | ExternalSecret config for `admin` (same shape as `clickhouse.externalSecret.*`). |
| `clickhouse.readonlyUser.enabled` | `false` | When `true`, the chart provisions a second SELECT-only ClickHouse user (`readonly_user`) with `readonly = 2` so standard JDBC clients (DataGrip etc.) can complete their handshake, the K8s Secret named by `clickhouse.readonlyUser.secret`, and a second ExternalSecret sourcing its password. |
| `clickhouse.readonlyUser.secret` | `ao-clickhouse-readonly-user-credentials` | Name of the K8s Secret (created by ESO) holding the readonly_user password under the `password` key. |
| `clickhouse.readonlyUser.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `readonly_user`. |
| `clickhouse.readonlyUser.externalSecret.secretStoreRef.name` | `""` | Name of the `SecretStore` or `ClusterSecretStore` for the readonly_user password (required when `readonlyUser.enabled = true`) |
| `clickhouse.readonlyUser.externalSecret.secretStoreRef.kind` | `ClusterSecretStore` | Kind of the readonly_user secret store reference |
| `clickhouse.readonlyUser.externalSecret.remoteRef.key` | `""` | Key in the external secrets backend holding the readonly_user password (required when `readonlyUser.enabled = true`) |
| `clickhouse.readonlyUser.externalSecret.remoteRef.property` | `""` | Property within a JSON secret (optional) |
| `clickhouse.readonlyUser.externalSecret.remoteRef.version` | `""` | Version of the readonly_user secret (required for Fake provider) |
| `clickhouse.readonlyUser.externalSecret.refreshInterval` | `1h` | How often ESO syncs the readonly_user secret |
| `clickhouse.hostname` | `""` | If set, adds `external-dns.alpha.kubernetes.io/hostname` annotation to the ClickHouse Service |
| `clickhouse.service.type` | `ClusterIP` | ClickHouse Service type (`ClusterIP`, `LoadBalancer`) |
| `clickhouse.service.annotations` | `{}` | Annotations on the ClickHouse Service (e.g. AWS NLB annotations) |
| `llmWorker.image.repository` | `""` | Image repository for the `llm-worker` (required — e.g. `montecarlodata/ao-llm-worker`) |
| `llmWorker.image.tag` | `""` | Image tag for the `llm-worker` |
| `llmWorker.aws.region` | `us-east-1` | AWS region passed to the `llm-worker` |
| `opentelemetry-collector.service.type` | `ClusterIP` | OTel Collector Service type (`ClusterIP`, `LoadBalancer`) |
| `opentelemetry-collector.service.annotations` | `{}` | Annotations on the OTel Collector Service (e.g. AWS NLB, external-dns) |
| `tls.enabled` | `true` | Enable TLS between services (requires cert-manager) |
| `tls.certManager.createCA` | `true` | Create a self-signed CA; set to `false` if you have your own issuer |
| `tls.certManager.existingIssuerRef` | `{}` | Use an existing issuer instead of the generated CA (e.g. `{name: my-issuer, kind: ClusterIssuer}`) |

### Node scheduling and workload isolation

ClickHouse and the OTel collector should **not** run on the same node. Both carry
multi-GiB memory limits that together can exceed a single node's capacity, and the
collector sizes its `memory_limiter` against a fixed reference rather than the
node — co-scheduling them risks node-level OOM and correlated failure of both
workloads.

Through chart `1.2.x` the chart enforced this itself with a default
`requiredDuringSchedulingIgnoredDuringExecution` pod anti-affinity on the
collector. That hard rule was removed in `1.3.0`: on the common deployment shape
for this chart — a small cluster where ClickHouse's EBS PV is locked to a single
Availability Zone — the rule could become unsatisfiable after a node-group roll
and leave ClickHouse stuck `Pending` (its PV can't follow it to another AZ, and
the collector may be occupying the only node in CH's AZ).

**The chart no longer enforces separation by default.** Isolation is now expected
to come from node-group partitioning, which you wire via `clickhouse.nodeSelector`
and `clickhouse.tolerations`. The recommended pattern is a dedicated, tainted node
group for ClickHouse, with the collector left on the general pool:

```yaml
clickhouse:
  # Pin ClickHouse to its dedicated node group.
  nodeSelector:
    dedicated: clickhouse
  tolerations:
    - key: dedicated
      operator: Equal
      value: clickhouse
      effect: NoSchedule
```

With ClickHouse pinned (and tolerating) a dedicated node group and the collector
scheduling only on the general pool, the two workloads physically cannot land on
the same node, so no anti-affinity rule is needed.

If you are not partitioning nodes, either set `clickhouse.nodeSelector` +
`clickhouse.tolerations` to target your own dedicated node group, or restore
collector-side separation by overriding `opentelemetry-collector.affinity` with a
pod anti-affinity rule of your own (`preferred…` avoids the single-AZ deadlock that
motivated removing the default):

```yaml
opentelemetry-collector:
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          podAffinityTerm:
            labelSelector:
              matchLabels:
                clickhouse.altinity.com/app: chop
            topologyKey: kubernetes.io/hostname
```
