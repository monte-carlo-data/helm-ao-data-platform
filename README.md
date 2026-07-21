# ao-data-platform

Helm chart for the Monte Carlo data plane for Agent Observability.

## Chart

### ao-data-platform

Deploys the observability data plane:
- Altinity ClickHouse Operator + ClickHouse installation (single shard, `clickhouse.replicasCount` replicas)
- ClickHouse Keeper ensemble (Raft coordination for replicated tables — 3 voters by default, `keeper.replicasCount: 1` for dev; see [ClickHouse replication and Keeper](#clickhouse-replication-and-keeper))
- OpenTelemetry Collector (traces pipeline)
- Schema migration Job (a plain `Job`, recreated per release revision, that runs on every install and upgrade)

The ClickHouse instance ships with production hardening: a capped memory ceiling (80% of the cgroup limit), `notice`-level logging, 7-day TTLs on system log tables, a startup probe with a 5-minute warmup window, and a writer-safe readiness probe (see [Writer-safe readiness](#writer-safe-readiness-ready)). Disruption protection comes from the ClickHouse operator, which creates a `PodDisruptionBudget` (`maxUnavailable: 1`) for the pods it manages — a circuit breaker for cluster automation (node drains, Kubernetes upgrades, node AMI rolls), not an HA mechanism. Do **not** add your own PDB selecting these pods: a pod matched by more than one PDB cannot be evicted at all (the Kubernetes eviction API rejects multi-PDB pods), which blocks node drains outright instead of rate-limiting them.

> **Upgrading an existing cluster:** the system-log TTLs only take effect when ClickHouse first *creates* each `system.*_log` table. If those tables already exist (any cluster that was running before this chart version), a restart will not apply the TTLs retroactively — `SHOW CREATE TABLE system.query_log` will show no TTL, which is expected, not a failure. To apply them on an existing cluster, run a one-time `ALTER TABLE system.<log> MODIFY TTL event_date + INTERVAL 7 DAY` per log table (or drop the tables and let ClickHouse recreate them on next flush).

> **Chart-version bumps no longer recreate ClickHouse:** the ClickHouse operator propagates only a fixed allowlist of stable labels onto the resources it generates. A chart-version bump changes the volatile `helm.sh/chart` label, but that label is no longer stamped onto the StatefulSet's immutable `volumeClaimTemplates`, so the bump no longer forces a delete/recreate of the ClickHouse StatefulSet.

> **Upgrading to 3.0.0:** a breaking major release — the default install shape changes. The schema SQL is now clustered-only — every table is a path-less `Replicated*` engine, all DDL (including the schema-Job TTL `ALTER`s) runs `ON CLUSTER '{cluster}'`, and `clickhouse.replicasCount` defaults to **2**. A bare install now deploys the prod HA shape (2 ClickHouse replicas + 3 Keeper voters, both hard-spread across zones); dev/single-AZ installs set both counts to `1`. **Installs upgrading with existing pre-3.0.0 (plain `MergeTree`) data must pin `clickhouse.replicasCount: 1` until the tables have been converted in place** — see [the migration ordering](#high-availability-and-the-migration-ordering). Readiness also switches from the operator-injected `/ping` to the writer-safe `/ready` handler: a replica partitioned from Keeper drops out of the Service until it rejoins, and a joining replica stays not-Ready through its registration/metadata phase — though it can turn Ready before its historical part fetches finish, so gate operational waits on `system.replicas`, not pod Ready (see [Writer-safe readiness](#writer-safe-readiness-ready)).
>
> **Upgrading to 2.3.0:** every install now renders a `ClickHouseKeeperInstallation` alongside the ClickHouse server — 3 Keeper voters with a hard one-voter-per-AZ topology spread by default. On clusters that can't schedule across three zones (local dev, single-AZ), the extra voters stay `Pending`; set `keeper.replicasCount: 1` there. The ClickHouse server is wired to Keeper from this version, but the tables are still plain `MergeTree` and don't use it yet — runtime behavior is otherwise unchanged. The Keeper client port ships network-isolated by a `NetworkPolicy` (inert on clusters without a NetworkPolicy engine) with a trimmed four-letter-word allowlist — see [Keeper network exposure and hardening](#keeper-network-exposure-and-hardening).

> **Telemetry retention** is controlled by `clickhouse.ttlDays` (default 30 days), covering the raw traces, the trace-id timestamp index, the normalized spans, and their conversation-derived annotations (`conversation_eval_scores`, `conversation_cluster_assignments`). Unlike the system-log TTLs above, the schema migration Job re-applies this on every install and upgrade (`ALTER TABLE … MODIFY TTL`), so changing the value updates existing tables — no manual ALTER needed. The Job sets `materialize_ttl_after_modify = 0`, so the change is metadata-only: *raising* the TTL takes effect immediately, while *lowering* it purges newly-expired rows lazily on the next background merge rather than at once. To force an immediate purge after lowering, run `ALTER TABLE otel_traces.<table> MATERIALIZE TTL` per affected table. The `llm_*` worker queue tables (`llm_inputs`, `llm_results`, `llm_batches`) are LLM-pipeline state rather than telemetry and are **not** governed by this value — they keep a fixed 30-day TTL defined in their SQL.

## ClickHouse replication and Keeper

The chart deploys a single-shard ClickHouse cluster whose replica count is set by
`clickhouse.replicasCount` (default `2`), coordinated by a ClickHouse Keeper ensemble
(`keeper.replicasCount` voters, default `3`). Keeper is intrinsic to the clustered design —
it renders on every install, there is no keeper-less mode; a dev or small deployment just
sizes it down to one voter. The ClickHouse server is wired to Keeper via the CHI's
`zookeeper` configuration, and replicated tables register under a macro-based ZooKeeper
path (`default_replica_path: /clickhouse/tables/{cluster}/{shard}/{database}/{table}`)
rather than ClickHouse's built-in `{uuid}` default.

### Keeper sizing and scheduling

- **Voter count should be odd** (Raft quorum). 3 voters tolerate losing one; use `1` for dev.
- **Hard per-AZ spread:** the CHK pod template applies a `DoNotSchedule` topology spread on
  `topology.kubernetes.io/zone`, so a 3-voter ensemble demands schedulable nodes in three
  zones. A voter with no valid zone stays `Pending` — expected on single-AZ or local
  clusters; set `keeper.replicasCount: 1` there. The spread is deliberately hard: packing two
  voters into one AZ would silently forfeit the ensemble's single-AZ-failure tolerance.
- Keeper persists only coordination metadata (Raft log + snapshots), so its PVCs are small
  (`keeper.storageSize`, default `10Gi`) and a replaced voter re-syncs from the quorum.
- Pin voters to dedicated nodes with `keeper.nodeSelector` / `keeper.tolerations`, same
  pattern as ClickHouse (see [Node scheduling and workload isolation](#node-scheduling-and-workload-isolation)).

### Keeper network exposure and hardening

Keeper speaks the ZooKeeper protocol, which is **unauthenticated as deployed here**: any
pod that can reach the client port (2181) can read and write the entire coordination tree.
That is the standard production posture for Keeper/ZooKeeper ensembles — the ecosystem
convention (including the operator's own hardening guidance) is to secure Keeper by
network isolation rather than protocol-level auth — so the chart hardens the network
layer:

- **NetworkPolicy** (`keeper.networkPolicy.enabled`, default `true`): restricts Keeper
  ingress to the ClickHouse server pods and the operator on the client port, and to the
  Keeper voters themselves on the Raft port (9444). This is the chart's first
  `NetworkPolicy` object — note that policies only take effect on clusters with a
  NetworkPolicy-capable CNI (on EKS, the VPC CNI's network policy agent must be enabled);
  without one the object is inert, which is also why it's safe to ship enabled by default.
  Because an inert policy looks healthy in the API, `hack/verify-deployment-aws.sh` proves the
  deny with a negative probe: it connects to the Keeper client port from an unauthorized
  pod and requires the connection to time out.
- **Four-letter-word allowlist** (`keeper.fourLetterWordAllowList`, default
  `ruok,mntr,srvr,stat,conf`): Keeper's built-in default additionally serves
  cluster-affecting commands (e.g. `rcvr` force-recovery, `rqld` request-leadership) on
  that same unauthenticated port; the chart trims the list to the liveness probe's `ruok`
  plus read-only introspection. Keep `ruok` in any custom list — the operator's injected
  liveness probe depends on it.

Protocol-level auth (ZooKeeper digest ACLs) is deliberately not used: it is uncommon in
production Keeper deployments, has known friction with the operator's tooling, and the
credential would travel in cleartext anyway. If a future deployment needs
defense-in-depth on this port, the natural upgrade is Keeper TLS (`tcp_port_secure`),
which unlike digest ACLs has no retrofit penalty on existing znodes.

### High availability and the migration ordering

From chart `3.0.0` the default install is HA: the `sql/*.sql` table definitions are
path-less `Replicated*` engines, every DDL statement runs `ON CLUSTER '{cluster}'` (so the
single schema Job propagates schema to every replica via Keeper's distributed DDL queue),
and the ClickHouse pods carry the same hard per-zone topology spread as the Keeper voters —
`DoNotSchedule`, so the second replica needs a schedulable node in a second zone or it
stays `Pending`.

Fresh installs need no ceremony: both replicas create their tables at the same macro-based
ZooKeeper path and replicate from the first insert. Dev/small installs set
`clickhouse.replicasCount: 1` + `keeper.replicasCount: 1` — same engines, same DDL, sized
down; there is no single-instance engine mode.

Moving an existing single-replica install with pre-`3.0.0` (plain `MergeTree`) data to HA
is a two-apply sequence with a manual conversion in between. **Do not raise
`clickhouse.replicasCount` above 1 — and do not take the `3.0.0` upgrade without pinning it
at 1 — until the conversion has verified.** A second replica created against non-replicated
tables does not clone the existing data; it starts an independent, empty table lineage.

1. **Apply #1** (chart `2.3.x`): Keeper + the macro-based `default_replica_path` ship
   while `replicasCount` stays `1`. Both are inert for plain `MergeTree` tables — this
   apply only prepares the coordination layer the conversion assumes.
2. **Convert the existing tables in place** using ClickHouse's
   [`convert_to_replicated` flag-file mechanism](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replication#converting-from-mergetree-to-replicatedmergetree)
   (requires ClickHouse ≥ 23.11 and an Atomic database; take a volume snapshot first).
   Verify every table reports `Replicated*` engines, `is_readonly = 0`, and the macro-based
   `zookeeper_path` in `system.replicas` before proceeding.
3. **Apply #2** (chart ≥ `3.0.0`, `replicasCount: 2`): the operator adds the second
   replica. Because the DDL is path-less and `default_replica_path` is macro-based, its
   `ON CLUSTER` `CREATE … IF NOT EXISTS` statements no-op on the converted replica and
   resolve to the same ZooKeeper path on the new one — it registers as a second replica
   and clones every part over the interserver port. The new pod reports not-Ready (the
   `/ready` probe below) only while its tables are readonly — the registration and
   metadata-attach phase of the clone. `is_readonly` clears once the fetch queue is
   populated, *before* the queued part fetches complete, so the pod can turn Ready while
   the historical backfill is still in flight; gate operational waits on
   `system.replicas` (`active_replicas = 2`, `queue_size` draining to 0), not on pod
   Ready.

The macro-based replica path is what makes step 3 safe: a converted table keeps its old
UUID while a freshly created replica gets a new one, so ClickHouse's default
`{uuid}`-based path would register them as two unrelated replicas that never sync.

### Writer-safe readiness (`/ready`)

A replica that loses its Keeper session keeps answering `/ping` with 200 while its
`Replicated*` tables are readonly (`/replicas_status` also stays 200 in ClickHouse 26.4) —
and INSERTs routed to it don't fail fast, they hang in `WaitForAsyncInsert` until timeout.
The chart therefore replaces the pod readiness probe with a custom `http_handlers`
endpoint, `/ready`, running `SELECT throwIf(count() > 0) FROM system.replicas WHERE
is_readonly`. It executes as a dedicated passwordless `probe` user — readonly profile,
config grants allowing `SELECT` on `system.replicas` plus `SHOW TABLES` on `otel_traces.*`
(row *visibility*: `system.replicas` only shows tables the querying user can see, so
without it the probe reads an empty table and can never fail) and nothing else —
unauthenticated to callers, like `/ping`. A readonly replica drops out of the Service
until it rejoins Keeper.

The probe is writer-safe, not read-complete. A replica joining the cluster is readonly —
and therefore not-Ready — only through its registration/metadata phase: `is_readonly`
clears once the replication queue is populated, *before* the queued part fetches run, so
a new replica can turn Ready (and serve reads) while its historical backfill is still in
flight. That is correct for writers — a queue-populated replica accepts INSERTs and
replicates them — but reads can hit a still-backfilling replica, so gate operational
waits on `system.replicas` (`queue_size` draining to 0), never on pod Ready.

Three consequences to be aware of: a not-Ready replica already counts as disrupted for
the operator's `maxUnavailable: 1` PDB, so a drain of the *healthy* replica's node is
refused while the other is readonly (correct, but it changes node-roll behavior);
`helm --wait` / `kubectl wait --for=condition=Ready` won't return while a pod is still
readonly; and if *every* replica is readonly at once — Keeper quorum loss, or in the
single-voter dev shape a Keeper outage outlasting the probe window (~30 s:
`failureThreshold: 3` × `periodSeconds: 10`) — the Service empties and *reads* are
blocked too, even though each replica's data is still locally SELECT-able. Pre-3.0.0
`/ping` readiness kept reads flowing in that state; trading read availability for
writer safety here is deliberate.

## Prerequisites

- Helm 3
- A Kubernetes cluster (k3s for local dev, EKS for AWS, AKS for Azure)
- [cert-manager](https://cert-manager.io/) installed in the cluster (for TLS, enabled by default)
- [External Secrets Operator](https://external-secrets.io/) installed in the cluster
- A `SecretStore` or `ClusterSecretStore` configured to access your secrets backend (AWS Secrets Manager, Azure Key Vault, Fake provider for local dev, etc.)
- [trust-manager](https://cert-manager.io/docs/trust/trust-manager/) — for the Gateway path (`gateway.enabled`, azure or gke), which always renders a trust-manager `Bundle` for the Gateway→backend re-encrypt. The per-cloud Terraform module ([Azure](https://github.com/monte-carlo-data/terraform-azurerm-ao-data-platform) / [GCP](https://github.com/monte-carlo-data/terraform-google-ao-data-platform)) installs it; without it, apply fails with a CRD-not-found error.

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
wire them the same way under `clickhouse.readonlyUser` / `clickhouse.admin` if you need
them.) `clickhouse.replicasCount=1` + `keeper.replicasCount=1` size the cluster down to a
single replica and a single voter — the default 2 replicas + 3 voters carry hard per-zone
spreads that a local k3d cluster can't satisfy (the extra pods would sit `Pending`). Note
that with a single voter there is no Keeper quorum to fail over to: a Keeper pod outage
outlasting the readiness window (~30 s) turns the ClickHouse replica readonly and empties
the ClickHouse Service — reads included — until Keeper returns (see
[Writer-safe readiness](#writer-safe-readiness-ready)).

```bash
helm upgrade --install ao-data-platform charts/ao-data-platform/ -n montecarlo --create-namespace \
  --set clickhouse.replicasCount=1 \
  --set keeper.replicasCount=1 \
  --set clickhouse.otel.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.otel.externalSecret.remoteRef.key=clickhouse-otel-password \
  --set clickhouse.otel.externalSecret.remoteRef.version=v1 \
  --set clickhouse.schemaOwner.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.schemaOwner.externalSecret.remoteRef.key=clickhouse-schema-owner-password \
  --set clickhouse.schemaOwner.externalSecret.remoteRef.version=v1 \
  --set clickhouse.llmWorker.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.llmWorker.externalSecret.remoteRef.key=clickhouse-llm-worker-password \
  --set clickhouse.llmWorker.externalSecret.remoteRef.version=v1 \
  --set clickhouse.monteCarlo.externalSecret.secretStoreRef.name=fake-secret-store \
  --set clickhouse.monteCarlo.externalSecret.remoteRef.key=clickhouse-monte-carlo-password \
  --set clickhouse.monteCarlo.externalSecret.remoteRef.version=v1 \
  --set llmWorker.image.repository=montecarlodata/ao-llm-worker \
  --set llmWorker.image.tag=latest
```

### 7. Verify

```bash
# ClickHouse operator
kubectl get pods -n montecarlo -l app.kubernetes.io/name=altinity-clickhouse-operator

# ClickHouse instance
kubectl get chi -n montecarlo

# ClickHouse Keeper ensemble (all voters should be Running; a Pending voter usually
# means the per-AZ topology spread can't be satisfied — see the Keeper section above)
kubectl get chk -n montecarlo
kubectl get pods -n montecarlo -l clickhouse-keeper.altinity.com/chk

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
`clickhouse.admin` if needed.)

```yaml
clickhouse:
  otel:
    externalSecret:                                  # otel
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-otel-password}  # AWS Secrets Manager secret name
  schemaOwner:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-schema-owner-password}
  llmWorker:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-llm-worker-password}
  monteCarlo:
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

For a full post-deploy check, run `hack/verify-deployment-aws.sh -n <namespace> -r <region>` —
it verifies pods, ClickHouse, the OTel collector, certs, and the NLB/DNS endpoints.

## Deploying to Azure (AKS)

On Azure the chart exposes the collector and ClickHouse through the **managed AKS Gateway API**
(the application-routing add-on), terminating TLS at per-hostname HTTPS listeners and re-encrypting
to the in-cluster backends. This path is **opt-in** (`gateway.enabled=true`) and **disabled by
default**, so AWS and local installs are unaffected.

The Gateway's load balancer is **always internal** (private IP only) — the collector and ClickHouse
endpoints are never publicly reachable and are reached over private connectivity.

Because the Gateway is L7 HTTP(S), clients reach the backends over their HTTP interfaces only: the
collector takes **OTLP/HTTP on 4318** (no gRPC), and ClickHouse is reached over its **HTTPS interface
on port 8443** (not 443, and not the native TCP protocol on 9440) — so clients and DSNs should target
`https://<clickhouse-host>:8443`.

### Prerequisites

- An AKS cluster with the **application-routing add-on** enabled, providing the Gateway API CRDs
  and the `approuting-istio` GatewayClass (`gateway.className`)
- [cert-manager](https://cert-manager.io/) and [trust-manager](https://cert-manager.io/docs/trust/trust-manager/),
  with trust-manager's trust namespace set to the release namespace
- [External Secrets Operator](https://external-secrets.io/) with a `ClusterSecretStore` for Azure
  Key Vault (the ClickHouse user passwords)
- A Workload Identity for the cert-manager DNS-01 solver, with access to the DNS zone
- A DNS zone for the listener hostnames — Let's Encrypt validates via DNS-01 (DNS records, not
  endpoint reachability), so it issues publicly-trusted certs even though the endpoints are private.
  Point `gateway.otelHostname` / `gateway.clickhouseHostname` at the Gateway's private LB IP.

The companion [Azure Terraform module](https://github.com/monte-carlo-data/terraform-azurerm-ao-data-platform)
provisions all of the above (add-on, cert-manager, trust-manager,
ESO, Key Vault, Workload Identities) and renders the matching values; deploying the chart standalone
means reproducing those prerequisites yourself.

### Enabling the gateway

Supply environment-specific configuration in your own values file and pass it with `-f`:

```yaml
gateway:
  enabled: true
  provider: azure                         # required when gateway.enabled — "azure" or "gke"
  otelHostname: otel.<your-zone>          # resolves to the Gateway's private LB IP
  clickhouseHostname: clickhouse.<your-zone>
  tls:
    source: letsencrypt                   # only supported source
    letsencrypt:
      email: ""                           # optional ACME contact
      azureDNS:
        hostedZoneName: <your-zone>
        resourceGroupName: <dns-rg>
        subscriptionID: <subscription-id>
        managedIdentityClientID: <cert-manager-workload-identity-client-id>
```

```bash
helm dependency build charts/ao-data-platform/
helm upgrade --install ao-data-platform charts/ao-data-platform/ -n montecarlo --create-namespace \
  -f my-values.yaml
```

The Gateway always re-encrypts the Gateway→backend hop and **requires `tls.enabled=true`** — the
ClickHouse `https`/8443 listener only exists when TLS is enabled. This is enforced at render time, so
enabling the gateway with `tls.enabled=false` fails the render rather than breaking silently. Verify
with `kubectl get gateway,httproute,certificate -n montecarlo`.

For a full post-deploy check, run `hack/verify-deployment-azure.sh -n <namespace>` — it
auto-detects gateway vs internal-LB mode and verifies pods, ClickHouse, the OTel collector,
certs, and (in gateway mode) the Gateway/HTTPRoute/BackendTLSPolicy resources.

## Deploying to GCP (GKE)

On GKE the chart exposes the collector and ClickHouse through the **GKE Gateway API** using the
internal regional GatewayClass (`gke-l7-rilb`, set via `gateway.className`), terminating TLS at
per-hostname HTTPS listeners and re-encrypting to the in-cluster backends. Like the Azure path it
is **opt-in** (`gateway.enabled=true`, `gateway.provider=gke`) and **disabled by default**, so AWS
and local installs are unaffected.

The Gateway's load balancer is **always internal** (private IP only) — the collector and ClickHouse
endpoints are never publicly reachable and are reached over private connectivity.

As with Azure, clients reach the backends over their HTTP interfaces only: the collector takes
**OTLP/HTTP on 4318**, and ClickHouse over its **HTTPS interface on port 8443** — target
`https://<clickhouse-host>:8443`. Two GKE-specific pieces differ from Azure:

- **Source-range restriction** is a regional **Cloud Armor** policy attached to each backend via a
  `GCPBackendPolicy`, named by `gateway.gcpBackendSecurityPolicy` (not the Azure annotation). Leave
  it empty for no restriction. `gateway.allowedSourceRanges` is the Azure-only path and must stay
  empty on gke (enforced at render time).
- **Backend health checks** use a `HealthCheckPolicy` pointing at the collector's `:13133` health
  endpoint — otherwise the `appProtocol: HTTPS` backend marking makes GKE default to an
  HTTPS-on-serving-port probe that 404s the OTLP receiver.

### Prerequisites

- A GKE cluster with the **Gateway API** enabled and the internal regional GatewayClass
  `gke-l7-rilb` (`gateway.className`)
- [cert-manager](https://cert-manager.io/) and [trust-manager](https://cert-manager.io/docs/trust/trust-manager/),
  with trust-manager's trust namespace set to the release namespace
- [External Secrets Operator](https://external-secrets.io/) with a `ClusterSecretStore` for GCP
  Secret Manager (the ClickHouse user passwords)
- **Workload Identity Federation** for the cert-manager DNS-01 solver, bound to the Cloud DNS zone
  (cert-manager uses ambient GKE Workload Identity — no service-account key)
- A **Cloud DNS** zone for the listener hostnames — Let's Encrypt validates via DNS-01 (DNS records,
  not endpoint reachability), so it issues publicly-trusted certs even though the endpoints are
  private. Point `gateway.otelHostname` / `gateway.clickhouseHostname` at the Gateway's private LB IP.

The companion [GCP Terraform module](https://github.com/monte-carlo-data/terraform-google-ao-data-platform)
provisions all of the above (Gateway API, cert-manager, trust-manager, ESO, Secret Manager, Cloud
Armor policy, Workload Identity Federation) and renders the matching values; deploying the chart
standalone means reproducing those prerequisites yourself.

### Enabling the gateway

Supply environment-specific configuration in your own values file and pass it with `-f`:

```yaml
gateway:
  enabled: true
  provider: gke                           # className defaults to gke-l7-rilb for gke
  otelHostname: otel.<your-zone>          # resolves to the Gateway's private LB IP
  clickhouseHostname: clickhouse.<your-zone>
  gcpBackendSecurityPolicy: <cloud-armor-policy-name>   # optional; empty = no source restriction
  tls:
    source: letsencrypt                   # only supported source
    letsencrypt:
      email: ""                           # optional ACME contact
      cloudDNS:
        project: <dns-project-id>         # project hosting the Cloud DNS zone
```

```bash
helm dependency build charts/ao-data-platform/
helm upgrade --install ao-data-platform charts/ao-data-platform/ -n montecarlo --create-namespace \
  -f my-values.yaml
```

As on Azure, the Gateway re-encrypts the Gateway→backend hop and **requires `tls.enabled=true`**;
enabling the gateway with `tls.enabled=false` fails the render. Verify with
`kubectl get gateway,httproute,certificate -n montecarlo`.

For a full post-deploy check, run `hack/verify-deployment-gcp.sh -n <namespace>` — it verifies
pods, ClickHouse, the OTel collector, certs, StorageClass, and the Gateway/HTTPRoute/BackendTLSPolicy
+ GCPBackendPolicy/HealthCheckPolicy resources.

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
helm pull oci://registry-1.docker.io/montecarlodata/ao-data-platform --version 2.1.0
```

## ClickHouse user model

The chart provisions a least-privilege ClickHouse user per access path. Privileges are enforced
declaratively via config-level grants in the `ClickHouseInstallation` (no SQL-RBAC bootstrap). The
materialized views run under `schema_owner` as their `DEFINER`, so the ingest user needs no access
to the normalized target tables. The stock `default` superuser is removed.

| User | Reads | Writes | Used by | Provisioned |
|------|-------|--------|---------|-------------|
| `schema_owner` | `otel_traces.*` | full DDL on `otel_traces.*`, `ALTER` on 7 system-log tables², `SYSTEM FLUSH LOGS`, `CLUSTER` on `*.*`³ | schema-migration Job; the MV `DEFINER` | always |
| `otel` | full read (`restrictGrants=false`, default); `—` when `restrictGrants=true` | `INSERT` on `otel_traces.otel_traces` when `clickhouse.otel.restrictGrants=true` (otherwise unrestricted) | OTel collector | always |
| `llm_worker` | `llm_batches`/`llm_inputs`/`llm_results` | `INSERT` on `llm_batches`/`llm_results` | llm-worker Deployment | always |
| `monte_carlo` | reader bundle¹ | `INSERT` on `llm_inputs`/`llm_batches`/`conversation_eval_scores`/`conversation_cluster_assignments` | Monte Carlo (data-source monitoring + agent observability) | always |
| `probe` | `system.replicas` + table visibility (`SHOW TABLES` on `otel_traces.*`; no data reads) | — (`readonly=2` profile) | the `/ready` readiness handler (see [Writer-safe readiness](#writer-safe-readiness-ready)) | always (passwordless; no ExternalSecret) |
| `readonly_user` | reader bundle¹ | — (`readonly=2`, so JDBC `SET` works) | humans / MCP / JDBC clients | `clickhouse.readonlyUser.enabled=true` |
| `admin` | all | all + user management + `SYSTEM` | break-glass DBA (not service-to-service; loopback-only by default) | `clickhouse.admin.enabled=true` |

¹ **reader bundle** = `SELECT` on `otel_traces.*`, `system.tables/parts/query_log`,
`system.numbers`, and `information_schema.*` — the metadata reads JDBC/MCP clients and Monte Carlo
monitoring need, plus `system.numbers` for time-bucket / gap-fill queries (e.g. the trace
time-series). Shared by `monte_carlo` and `readonly_user`.

² **7 system-log tables** = `ALTER` on exactly `system.query_log`, `system.query_thread_log`,
`system.query_views_log`, `system.part_log`, `system.trace_log`, `system.metric_log`, and
`system.text_log` — the log tables whose TTLs the schema Job manages. The grant is *not* `system.*`;
it is scoped to these seven so it cannot touch system tables the Job never modifies.

³ **`CLUSTER`** — every statement the schema Job runs is `ON CLUSTER '{cluster}'`, and ClickHouse
gates ON CLUSTER queries behind the `CLUSTER` privilege
(`on_cluster_queries_require_cluster_grant` is on by default in this ClickHouse line). The
privilege is irreducibly global; it cannot be scoped below `*.*`.

Each password-backed user has an ExternalSecret sourcing its password from your secret store (see the
per-user `*.externalSecret` values below). Network *reachability* is typically restricted one layer
up at the load balancer; per-caller CH-user-level network scoping is handled separately.

The `hack/verify-deployment-aws.sh`, `hack/verify-deployment-azure.sh`, and
`hack/verify-deployment-gcp.sh` scripts run their ClickHouse data checks as `readonly_user`, so
set `clickhouse.readonlyUser.enabled=true` to use them.

### Upgrading an existing install (1.x → 2.0.0)

Chart `2.0.0` is a **breaking major release**. Running `helm upgrade` from a 1.x single-user
install without the steps below will error immediately — complete them first.

#### 1. Update your values file — renamed keys

Per-user value keys were reorganised so every user is a cohesive sub-map whose key mirrors its
ClickHouse username. Update any existing values file or `--set` overrides:

| Old key (1.x) | New key (2.0.0) |
|---|---|
| `clickhouse.otelSecret` | `clickhouse.otel.secret` |
| `clickhouse.otelNetworksIp` | `clickhouse.otel.networksIp` |
| `clickhouse.externalSecret.*` | `clickhouse.otel.externalSecret.*` |
| `clickhouse.llmWorkerUser.*` | `clickhouse.llmWorker.*` |
| `clickhouse.monteCarloUser.*` | `clickhouse.monteCarlo.*` |
| `clickhouse.adminUser.*` | `clickhouse.admin.*` |

(`clickhouse.otel`, `clickhouse.schemaOwner`, and `clickhouse.readonlyUser` are unchanged — each
already matches its SQL username `otel` / `schema_owner` / `readonly_user`.)

Example (AWS Secrets Manager):

```yaml
clickhouse:
  otel:
    externalSecret:
      secretStoreRef: {name: aws-secretsmanager, kind: ClusterSecretStore}
      remoteRef: {key: ao/clickhouse-otel-password}
```

#### 2. Provision secrets and wire ExternalSecrets for the new users

2.0.0 always provisions four users: `otel`, `schema_owner`, `llm_worker`, and `monte_carlo`. Create
secrets in your backend for each and add their ExternalSecret blocks to your values file
(see the "Configure the ExternalSecrets" step in the EKS walkthrough above for the full shape).

#### 3. Deploy Release A — backward-compatible

Set `clickhouse.otel.restrictGrants: false` (the default) and upgrade:

```bash
helm upgrade ao-data-platform oci://registry-1.docker.io/montecarlodata/ao-data-platform \
  --version 2.0.0 -n montecarlo -f my-values.yaml
```

The `0014` MV-DEFINER reparent migration runs automatically as part of the schema Job. At this
point all four users are live; `otel` still has unrestricted access so existing readers are
unaffected.

#### 4. Switch the Monte Carlo integration credential

In your Monte Carlo environment, update the ClickHouse data-source credential from the `otel` user
to the new `monte_carlo` user. Verify that dashboards and monitors are working before continuing.

#### 5. Deploy Release B — lock down `otel`

Once all external readers have moved to `monte_carlo`, set `restrictGrants: true` and redeploy:

```yaml
clickhouse:
  otel:
    restrictGrants: true
```

```bash
helm upgrade ao-data-platform oci://registry-1.docker.io/montecarlodata/ao-data-platform \
  --version 2.0.0 -n montecarlo -f my-values.yaml
```

`otel` is now INSERT-only. The upgrade is complete.

## Configuration

| Value | Default | Description |
|-------|---------|-------------|
| `clickhouse.replicasCount` | `2` | Number of ClickHouse replicas in the single-shard cluster. Default is the HA shape (hard per-zone spread); set `1` for dev/single-AZ installs. On installs with pre-existing plain-`MergeTree` data, hold at `1` until the in-place conversion has verified — see [ClickHouse replication and Keeper](#clickhouse-replication-and-keeper). |
| `clickhouse.storageSize` | `100Gi` | PVC size for ClickHouse data. |
| `clickhouse.ttlDays` | `30` | Retention in days for the telemetry tables (raw traces, trace-id index, normalized spans) and their conversation-derived annotations (`conversation_eval_scores`, `conversation_cluster_assignments`). Re-applied on every install/upgrade via `ALTER TABLE … MODIFY TTL`. Does **not** govern the `llm_*` worker queue tables (they keep a fixed TTL). See the telemetry-retention note above. |
| `clickhouse.nodeSelector` | `{}` | Node selector for the ClickHouse pod (wired into the CHI's `podTemplate`) |
| `clickhouse.tolerations` | `[]` | Tolerations for the ClickHouse pod (wired into the CHI's `podTemplate`) |
| `clickhouse.otel.secret` | `ao-clickhouse-otel-credentials` | Name of the K8s Secret (created by ESO) with a `password` key |
| `clickhouse.otel.networksIp` | `["0.0.0.0/0"]` | CIDR list allowed to authenticate as the `otel` user (default open). Reachability is typically restricted at the load balancer; per-caller CH-level scoping is handled separately. |
| `clickhouse.otel.restrictGrants` | `false` | When `true`, restrict `otel` to `INSERT` on `otel_traces.otel_traces` (least-privilege ingest). Leave `false` until external readers have been switched to `monte_carlo`. |
| `clickhouse.otel.externalSecret.secretStoreRef.name` | `""` | Name of the `SecretStore` or `ClusterSecretStore` to use (for the `otel` password) |
| `clickhouse.otel.externalSecret.secretStoreRef.kind` | `ClusterSecretStore` | Kind of the secret store reference |
| `clickhouse.otel.externalSecret.remoteRef.key` | `""` | Key in the external secrets backend |
| `clickhouse.otel.externalSecret.remoteRef.property` | `""` | Property within a JSON secret (optional) |
| `clickhouse.otel.externalSecret.remoteRef.version` | `""` | Version of the secret (required for Fake provider) |
| `clickhouse.otel.externalSecret.refreshInterval` | `1h` | How often ESO syncs the secret |
| `clickhouse.schemaOwner.secret` | `ao-clickhouse-schema-owner-credentials` | K8s Secret (ESO) for the always-provisioned `schema_owner` user. |
| `clickhouse.schemaOwner.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `schema_owner`. |
| `clickhouse.schemaOwner.externalSecret.*` | — | ExternalSecret config for `schema_owner` (same shape as `clickhouse.otel.externalSecret.*`). |
| `clickhouse.llmWorker.secret` | `ao-clickhouse-llm-worker-credentials` | K8s Secret (ESO) for the always-provisioned `llm_worker` user. |
| `clickhouse.llmWorker.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `llm_worker`. |
| `clickhouse.llmWorker.externalSecret.*` | — | ExternalSecret config for `llm_worker` (same shape as `clickhouse.otel.externalSecret.*`). |
| `clickhouse.monteCarlo.secret` | `ao-clickhouse-monte-carlo-credentials` | K8s Secret (ESO) for the always-provisioned `monte_carlo` user. |
| `clickhouse.monteCarlo.networksIp` | `["0.0.0.0/0"]` | CIDRs allowed to authenticate as `monte_carlo`. |
| `clickhouse.monteCarlo.externalSecret.*` | — | ExternalSecret config for `monte_carlo` (same shape as `clickhouse.otel.externalSecret.*`). |
| `clickhouse.admin.enabled` | `false` | When `true`, provision the gated `admin` break-glass superuser (full access + user management + `SYSTEM`) and its ExternalSecret. |
| `clickhouse.admin.secret` | `ao-clickhouse-admin-credentials` | K8s Secret (ESO) for `admin` (used when enabled). |
| `clickhouse.admin.networksIp` | `["127.0.0.1","::1"]` | CIDRs allowed to authenticate as `admin`. Defaults to loopback only, so `admin` is reachable only by exec-ing into the ClickHouse pod; override only if you need remote admin. |
| `clickhouse.admin.externalSecret.*` | — | ExternalSecret config for `admin` (same shape as `clickhouse.otel.externalSecret.*`). |
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
| `keeper.replicasCount` | `3` | Number of Keeper voters. Should be odd (Raft quorum); 3 for production HA, `1` for dev/single-AZ clusters (the default 3 require nodes in three zones — see the Keeper section). |
| `keeper.image` | `clickhouse/clickhouse-keeper:26.4.3` | Keeper image; pinned to track the ClickHouse server release line. |
| `keeper.storageClass` | `""` | StorageClass for the Keeper PVCs (empty = cluster default). |
| `keeper.storageSize` | `10Gi` | PVC size per Keeper voter. Keeper stores only Raft log + snapshots, so a small volume is ample. |
| `keeper.resources` | `500m`/`1Gi` requests, `4Gi` memory limit | Resource requests/limits for the Keeper container. CPU limit deliberately omitted (Keeper is bursty on failover). |
| `keeper.nodeSelector` | `{}` | Node selector for the Keeper voters (wired into the CHK's pod template). |
| `keeper.tolerations` | `[]` | Tolerations for the Keeper voters (wired into the CHK's pod template). |
| `keeper.networkPolicy.enabled` | `true` | Ship a `NetworkPolicy` restricting Keeper ingress to the ClickHouse pods + operator (client port) and Keeper peers (Raft). Inert without a NetworkPolicy engine — see [Keeper network exposure and hardening](#keeper-network-exposure-and-hardening). |
| `keeper.fourLetterWordAllowList` | `ruok,mntr,srvr,stat,conf` | Keeper four-letter-word commands served on the (unauthenticated) client port, trimmed from Keeper's broader built-in default. Must include `ruok` (liveness probe). Set `""` to fall back to Keeper's built-in default list. |
| `llmWorker.replicaCount` | `1` | Number of `llm-worker` pods — `0` or `1` only (the template rejects `>1`; the worker has no job-claim semantics, so concurrent copies would double-process batches). Set to `0` to pause the worker declaratively (survives `helm upgrade`, unlike a manual `kubectl scale`). |
| `llmWorker.image.repository` | `""` | Image repository for the `llm-worker` (required — e.g. `montecarlodata/ao-llm-worker`) |
| `llmWorker.image.tag` | `""` | Image tag for the `llm-worker` |
| `llmWorker.provider` | `bedrock` | LLM backend: `bedrock` (Claude on AWS Bedrock), `foundry` (Claude on Microsoft Foundry / Azure), or `vertex` (Claude on GCP Vertex AI). Selects `LLM_PROVIDER` and which provider-specific env the deployment renders. |
| `llmWorker.aws.region` | `us-east-1` | AWS region passed to the `llm-worker` (bedrock; block named `aws` for backward compatibility). |
| `llmWorker.foundry.resource` | `""` | Foundry resource name (required when `provider=foundry`). Auth is Entra ID via AKS Workload Identity. |
| `llmWorker.vertex.project` | `""` | GCP project id for Vertex (required when `provider=vertex`). Auth is ADC via GKE Workload Identity Federation. |
| `llmWorker.vertex.region` | `global` | Vertex location (`global` = the global endpoint). |
| `llmWorker.podLabels` | `{}` | Extra labels merged onto the `llm-worker` pod template (e.g. to opt the pod into an identity/admission webhook). |
| `opentelemetry-collector.service.type` | `ClusterIP` | OTel Collector Service type (`ClusterIP`, `LoadBalancer`) |
| `opentelemetry-collector.service.annotations` | `{}` | Annotations on the OTel Collector Service (e.g. AWS NLB, external-dns) |
| `tls.enabled` | `true` | Enable TLS between services (requires cert-manager) |
| `tls.certManager.createCA` | `true` | Create a self-signed CA; set to `false` if you have your own issuer |
| `tls.certManager.existingIssuerRef` | `{}` | Use an existing issuer instead of the generated CA (e.g. `{name: my-issuer, kind: ClusterIssuer}`) |
| `gateway.enabled` | `false` | Enable the internal Gateway API serving path (azure or gke — see `gateway.provider`). The Gateway's load balancer is always internal (private IP). See [Deploying to Azure (AKS)](#deploying-to-azure-aks) / [GCP (GKE)](#deploying-to-gcp-gke). |
| `gateway.provider` | `""` | Cloud that renders provider-specific Gateway resources: `azure` or `gke`. **Required when `gateway.enabled`** (no default). Selects the source-range mechanism and the cert-manager DNS-01 solver. |
| `gateway.className` | `""` | GatewayClass. Cloud-specific — derived from `gateway.provider` when empty (azure → `approuting-istio`, gke → `gke-l7-rilb`). Set only to override with a custom class. |
| `gateway.otelHostname` | `""` | Hostname for the OTel collector listener (required when `gateway.enabled`). Must resolve to the Gateway's private LB IP. |
| `gateway.clickhouseHostname` | `""` | Hostname for the ClickHouse listener (required when `gateway.enabled`). Must resolve to the Gateway's private LB IP. |
| `gateway.allowedSourceRanges` | `[]` | Azure source-range path: CIDRs allowed to reach the Gateway's internal load balancer (renders the `azure-allowed-ip-ranges` annotation). Empty = no restriction. Set only when `gateway.provider=azure`. |
| `gateway.gcpBackendSecurityPolicy` | `""` | GKE source-range path: name of a regional Cloud Armor policy attached to each backend via a `GCPBackendPolicy`. Empty = no restriction. Set only when `gateway.provider=gke` (mutually exclusive with `gateway.allowedSourceRanges`). |
| `gateway.tls.source` | `letsencrypt` | Listener cert source. Only `letsencrypt` is supported; Key Vault (BYO certs) is reserved for a future release. |
| `gateway.tls.letsencrypt.email` | `""` | ACME contact email (optional; omitted registers a contactless account). |
| `gateway.tls.letsencrypt.server` | `https://acme-v02.api.letsencrypt.org/directory` | ACME server URL (Let's Encrypt production). |
| `gateway.tls.letsencrypt.azureDNS.hostedZoneName` | `""` | DNS zone for the DNS-01 solver (required when `gateway.enabled`). |
| `gateway.tls.letsencrypt.azureDNS.resourceGroupName` | `""` | Resource group of the DNS zone (required when `gateway.enabled`). |
| `gateway.tls.letsencrypt.azureDNS.subscriptionID` | `""` | Subscription ID of the DNS zone (required when `gateway.enabled`). |
| `gateway.tls.letsencrypt.azureDNS.managedIdentityClientID` | `""` | cert-manager Workload Identity client ID for the DNS-01 solver (azure; required when `gateway.enabled` and `gateway.provider=azure`). |
| `gateway.tls.letsencrypt.cloudDNS.project` | `""` | GKE DNS-01 solver: project hosting the Cloud DNS zone (cert-manager uses ambient GKE Workload Identity, no key). Required when `gateway.enabled` and `gateway.provider=gke`. |

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

The Keeper voters follow the same pattern via `keeper.nodeSelector` /
`keeper.tolerations` (e.g. a `dedicated: keeper` node group). Their one-voter-per-AZ
topology spread is built into the CHK pod template and is not configurable through
values — the node groups you pin them to must span the required zones.

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
