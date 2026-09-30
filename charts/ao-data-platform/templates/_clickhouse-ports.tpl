{{/* Shared with the CHI settings, listeners, and backup NetworkPolicy. */}}
{{- define "ao-data-platform.clickhousePorts" -}}
{{- dict "http" 8123 "https" 8443 "tcp" 9000 "tcpSecure" 9440 "interserver" 9009 "metrics" 9363 | toJson -}}
{{- end -}}

{{/* Keep the operator's cluster selectors aligned with the CHI layout. */}}
{{- define "ao-data-platform.clickhouseClusterName" -}}otel{{- end -}}
