{{/* Ports used by backup config, Services, NetworkPolicy, TLS, and scheduler endpoints.
   `metrics` also sets the Prometheus endpoint and container port on both the
   ClickHouse and Keeper pods, and the Keeper NetworkPolicy's scraper rule. */}}
{{- define "ao-data-platform.clickhousePorts" -}}
{{- dict "http" 8123 "https" 8443 "tcp" 9000 "tcpSecure" 9440 "interserver" 9009 "metrics" 9363 | toJson -}}
{{- end -}}

{{/* Native Prometheus endpoint settings, shared by the ClickHouse and Keeper installations. */}}
{{- define "ao-data-platform.prometheusSettings" -}}
{{- $ports := include "ao-data-platform.clickhousePorts" . | fromJson -}}
prometheus/endpoint: /metrics
prometheus/port: {{ $ports.metrics | toString | quote }}
prometheus/metrics: "true"
prometheus/events: "true"
prometheus/asynchronous_metrics: "true"
{{- end -}}

{{/* Keep the operator's cluster selectors aligned with the CHI layout. */}}
{{- define "ao-data-platform.clickhouseClusterName" -}}otel{{- end -}}
