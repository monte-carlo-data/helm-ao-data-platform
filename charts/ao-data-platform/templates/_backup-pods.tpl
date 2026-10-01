{{/* One StatefulSet per replica, with Pod ordinal zero in the single-shard layout. */}}
{{- define "ao-data-platform.backupPodNames" -}}
{{- $names := list -}}
{{- range $replica := (include "ao-data-platform.backupReplicas" . | fromJsonArray) -}}
{{- $names = append $names (printf "chi-%s-%s-0-%d-0" (include "ao-data-platform.chiName" $) (include "ao-data-platform.clickhouseClusterName" $) (int $replica)) -}}
{{- end -}}
{{- toJson $names -}}
{{- end -}}
