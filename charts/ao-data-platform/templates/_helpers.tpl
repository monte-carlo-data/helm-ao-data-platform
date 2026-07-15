{{/*
Chart name, truncated to 63 characters.
*/}}
{{- define "ao-data-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name, truncated to 63 characters.
*/}}
{{- define "ao-data-platform.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Namespace-qualified name for CLUSTER-SCOPED resources (the ClusterIssuer and the
trust-manager Bundle). fullname derives from the release + chart name only, so two
same-named releases in different namespaces would collide on cluster-scoped objects
(silently overwriting each other's config; an uninstall deleting the survivor's shared
object). Appending the namespace keeps them distinct. Namespaced resources must NOT use
this — they are already namespace-isolated and use stable literals.
*/}}
{{- define "ao-data-platform.clusterScopedName" -}}
{{- printf "%s-%s" (include "ao-data-platform.fullname" .) .Release.Namespace -}}
{{- end }}

{{/*
Gateway feature validation — the single source of truth for the gateway path's render-time
guards. Included at the top of every gateway template so ANY gateway render (not just
gateway.yaml) enforces the same invariants; when a new gateway.tls.source branch lands, add
its guard here once instead of in each template. A no-op when gateway.enabled is false.
*/}}
{{- define "ao-data-platform.gatewayValidate" -}}
{{- if .Values.gateway.enabled -}}
{{- if ne .Values.gateway.tls.source "letsencrypt" -}}
{{- fail "gateway.tls.source must be \"letsencrypt\" — a BYO-cert source is reserved for a future release and not yet implemented." -}}
{{- end -}}
{{- if not .Values.tls.enabled -}}
{{- fail "gateway.enabled requires tls.enabled=true — the Gateway always re-encrypts to the ClickHouse https/8443 listener, which only exists when internal TLS is on. Set tls.enabled=true, or gateway.enabled=false." -}}
{{- end -}}
{{- if not .Values.tls.certManager.createCA -}}
{{- fail "gateway.enabled currently requires tls.certManager.createCA=true — the Gateway backend-TLS Bundle sources the chart-managed ao-data-platform-ca secret and does not yet support tls.certManager.existingIssuerRef." -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
LLM-worker provider validation — the single source of truth for the worker path's render-time
guards. Included at the top of the llm-worker templates so any render enforces the same
invariants: the provider must be a known enum, and foundry requires its resource (auth is
Entra ID via AKS Workload Identity, wired separately via serviceAccount.annotations + podLabels).
When a new provider lands (e.g. vertex), add its guard here once.
*/}}
{{- define "ao-data-platform.llmWorkerValidate" -}}
{{- $p := .Values.llmWorker.provider -}}
{{- if not (or (eq $p "bedrock") (eq $p "foundry") (eq $p "vertex")) -}}
{{- fail (printf "llmWorker.provider must be \"bedrock\", \"foundry\", or \"vertex\", got %q." $p) -}}
{{- end -}}
{{- if eq $p "foundry" -}}
{{- if not .Values.llmWorker.foundry.resource -}}
{{- fail "llmWorker.provider=foundry requires llmWorker.foundry.resource." -}}
{{- end -}}
{{- end -}}
{{- if eq $p "vertex" -}}
{{- if not .Values.llmWorker.vertex.project -}}
{{- fail "llmWorker.provider=vertex requires llmWorker.vertex.project." -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
ClickHouseInstallation name.
The Altinity operator stamps the label `clickhouse.altinity.com/chi: <name>`
onto the ClickHouse pods, so anything selecting those pods must use this same
value. Centralized here so the CHI name and its selectors can't drift apart.
*/}}
{{- define "ao-data-platform.chiName" -}}
otel
{{- end }}

{{/*
ClickHouseKeeperInstallation name.
The Altinity operator stamps the label `clickhouse-keeper.altinity.com/chk: <name>`
onto the Keeper pods and names the client Service `keeper-<name>`, so the CHK pod
topology-spread selector and the CHI's zookeeper host must both derive from this
same value. Centralized here so they can't drift apart.
*/}}
{{- define "ao-data-platform.chkName" -}}
otel
{{- end }}

{{/*
Keeper client Service DNS name (in-namespace).
The operator creates a ClusterIP Service `keeper-<chkName>` on the Keeper client port
(2181) — mirrors how the CHI's Service is `clickhouse-<chiName>`. This is the host the
ClickHouse server points its `zookeeper.nodes` at.
*/}}
{{- define "ao-data-platform.keeperServiceName" -}}
keeper-{{ include "ao-data-platform.chkName" . }}
{{- end }}

{{/*
TLS certificate issuer reference.
Returns the issuer ref block for Certificate resources.
*/}}
{{- define "ao-data-platform.issuerRef" -}}
{{- if .Values.tls.certManager.existingIssuerRef.name -}}
name: {{ .Values.tls.certManager.existingIssuerRef.name }}
kind: {{ .Values.tls.certManager.existingIssuerRef.kind | default "Issuer" }}
{{- else -}}
name: ao-data-platform-ca
kind: Issuer
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "ao-data-platform.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "ao-data-platform.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "ao-data-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ao-data-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ExternalSecret for a ClickHouse user password.
One ExternalSecret per CH user that has a Secrets-Manager-backed password, factored here so the
otel / schema_owner / llm_worker / monte_carlo / admin / readonly_user blocks don't each repeat it.
Call with a dict: {root: $, name: <k8s secret name>, externalSecret: <the user's externalSecret cfg>}.
Adding a ClickHouse user? Also update values.yaml and templates/clickhouse-installation.yaml.
*/}}
{{- define "ao-data-platform.externalSecret" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- $es := .externalSecret -}}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  labels:
    {{- include "ao-data-platform.labels" $root | nindent 4 }}
spec:
  refreshInterval: {{ $es.refreshInterval }}
  secretStoreRef:
    name: {{ required (printf "externalSecret.secretStoreRef.name is required for secret %s — set via clickhouse.<user>.externalSecret.secretStoreRef.name" $name) $es.secretStoreRef.name }}
    kind: {{ $es.secretStoreRef.kind }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
  data:
    - secretKey: password
      remoteRef:
        key: {{ required (printf "externalSecret.remoteRef.key is required for secret %s — set via clickhouse.<user>.externalSecret.remoteRef.key" $name) $es.remoteRef.key }}
        {{- if $es.remoteRef.property }}
        property: {{ $es.remoteRef.property }}
        {{- end }}
        {{- if $es.remoteRef.version }}
        version: {{ $es.remoteRef.version }}
        {{- end }}
{{- end }}

{{/*
Shared read-only grant bundle (the "reader bundle").
Granted to both monte_carlo (reader + queue producer) and readonly_user (human/MCP/JDBC). Covers the
telemetry DB plus the metadata reads DataGrip/MCP and Monte Carlo data-source monitoring need. Keep
this as the single source of truth — adding a read target means editing it here once.
Emits YAML list items (`- "GRANT …"`) intended for inclusion under a CHI `<user>/grants/query`
sequence. Must be called with `| nindent 8` to align with the surrounding 8-space indent used in
templates/clickhouse-installation.yaml. Call with the root context (`.`).
*/}}
{{- define "ao-data-platform.readerGrants" -}}
- "GRANT SELECT ON otel_traces.*"
- "GRANT SELECT ON system.tables"
- "GRANT SELECT ON system.parts"
- "GRANT SELECT ON system.query_log"
# system.numbers is the generator table for time-bucket / gap-fill queries (e.g. the
# getTraceTimeSeries time series), not a metadata read — but reader clients need it.
- "GRANT SELECT ON system.numbers"
- "GRANT SELECT ON information_schema.*"
{{- end }}
