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
