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
ClickHouseInstallation name.
The Altinity operator stamps the label `clickhouse.altinity.com/chi: <name>`
onto the ClickHouse pods, so anything selecting those pods (e.g. the PDB) must
use this same value. Centralized here so the CHI name and its selectors can't
drift apart.
*/}}
{{- define "ao-data-platform.chiName" -}}
otel
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
*/}}
{{- define "ao-data-platform.externalSecret" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- $es := .externalSecret -}}
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {{ $name }}
  labels:
    {{- include "ao-data-platform.labels" $root | nindent 4 }}
spec:
  refreshInterval: {{ $es.refreshInterval }}
  secretStoreRef:
    name: {{ required (printf "externalSecret.secretStoreRef.name is required for secret %s" $name) $es.secretStoreRef.name }}
    kind: {{ $es.secretStoreRef.kind }}
  target:
    name: {{ $name }}
    creationPolicy: Owner
  data:
    - secretKey: password
      remoteRef:
        key: {{ required (printf "externalSecret.remoteRef.key is required for secret %s" $name) $es.remoteRef.key }}
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
Rendered into a CHI user's `<user>/grants/query` list; call with the root context.
*/}}
{{- define "ao-data-platform.readerGrants" -}}
- "GRANT SELECT ON otel_traces.*"
- "GRANT SELECT ON system.tables"
- "GRANT SELECT ON system.parts"
- "GRANT SELECT ON system.query_log"
- "GRANT SELECT ON information_schema.*"
{{- end }}
