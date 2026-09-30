{{- define "ao-data-platform.backupValidate" -}}
{{- $b := .Values.clickhouse.backup -}}
{{- if $b.enabled -}}
{{- if ne $b.provider "aws" -}}{{- fail "clickhouse.backup.provider currently supports only aws." -}}{{- end -}}
{{- range $key := list "bucket" "region" "roleArn" "path" -}}
{{- if not (index $b.aws $key) -}}{{- fail (printf "clickhouse.backup.aws.%s is required." $key) -}}{{- end -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$" $b.aws.bucket) -}}{{- fail "clickhouse.backup.aws.bucket must be an S3 bucket name." -}}{{- end -}}
{{- if not (regexMatch "^[a-z0-9-]+$" $b.aws.region) -}}{{- fail "clickhouse.backup.aws.region must be an AWS region." -}}{{- end -}}
{{- if not (regexMatch "^[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)*$" $b.aws.path) -}}{{- fail "clickhouse.backup.aws.path must contain safe directory names without leading or trailing slashes." -}}{{- end -}}
{{- if or (not $b.serviceAccount.name) (eq $b.serviceAccount.name "default") -}}{{- fail "clickhouse.backup.serviceAccount.name must be a dedicated account, not default." -}}{{- end -}}
{{- if not $b.secret -}}{{- fail "clickhouse.backup.secret must name the backup credentials Secret." -}}{{- end -}}
{{- if not $b.api.existingSecret -}}{{- fail "clickhouse.backup.api.existingSecret must name a separate Secret containing the API password." -}}{{- end -}}
{{- if eq $b.api.existingSecret $b.secret -}}{{- fail "The backup API secret must be separate from the rotating ClickHouse password secret." -}}{{- end -}}
{{- if lt (int $b.schedule.timeoutSeconds) 60 -}}{{- fail "clickhouse.backup.schedule.timeoutSeconds must be at least 60." -}}{{- end -}}
{{- range $port := $b.networkPolicy.additionalPorts -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString $port))) (lt (int $port) 1) (gt (int $port) 65535) (eq (int $port) 7171) -}}
{{- fail "clickhouse.backup.networkPolicy.additionalPorts must contain TCP ports from 1 to 65535, excluding the protected backup port 7171." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Match the single-shard CHI layout; every configured replica can be a source. */}}
{{- define "ao-data-platform.backupReplicas" -}}
{{- until (int .Values.clickhouse.replicasCount) | toJson -}}
{{- end -}}

{{- define "ao-data-platform.backupEndpoints" -}}
{{- $endpoints := list -}}
{{- range $replica := (include "ao-data-platform.backupReplicas" . | fromJsonArray) -}}
{{- $endpoints = append $endpoints (printf "http://%s-backup-%d:7171" (include "ao-data-platform.chiName" $) (int $replica)) -}}
{{- end -}}
{{- toJson $endpoints -}}
{{- end -}}

{{- define "ao-data-platform.backupDisk" -}}
{{- $b := .Values.clickhouse.backup -}}
<clickhouse>
  <storage_configuration>
    <disks>
      <backups_s3>
        <type>s3</type>
        <endpoint>https://s3.{{ $b.aws.region }}.amazonaws.com/{{ $b.aws.bucket }}/{{ $b.aws.path }}/native/</endpoint>
        <use_environment_credentials>1</use_environment_credentials>
        <cache_enabled>false</cache_enabled>
        <send_metadata>false</send_metadata>
        <!-- Backup storage trouble must fail backups, not ClickHouse startup. -->
        <skip_access_check>true</skip_access_check>
      </backups_s3>
    </disks>
  </storage_configuration>
  <backups><allowed_disk>backups_s3</allowed_disk></backups>
</clickhouse>
{{- end -}}

{{/* ESO substitutes only the database password. API auth comes from a separate Secret. */}}
{{- define "ao-data-platform.backupConfig" -}}
general:
  remote_storage: s3
  backups_to_keep_local: 0
  backups_to_keep_remote: 0
  rbac_backup_always: false
  use_resumable_state: false
  log_level: info
clickhouse:
  host: 127.0.0.1
  port: 9000
  username: backup
  password: {{ "{{ .password | quote }}" }}
  timeout: {{ printf "%ds" (int .Values.clickhouse.backup.schedule.timeoutSeconds) | quote }}
  use_embedded_backup_restore: true
  embedded_backup_disk: backups_s3
  # A local backup can run while another replica is down. ON CLUSTER requires
  # every replica; the embedded cluster-restore path has not been verified.
  use_embedded_backup_restore_cluster: ""
s3:
  bucket: {{ .Values.clickhouse.backup.aws.bucket | quote }}
  region: {{ .Values.clickhouse.backup.aws.region | quote }}
  path: {{ printf "%s/catalog" .Values.clickhouse.backup.aws.path | quote }}
  compression_format: none
  acl: ""
api:
  listen: 0.0.0.0:7171
  create_integration_tables: false
  allow_parallel: false
  complete_resumable_after_restart: false
{{- end -}}

{{- define "ao-data-platform.backupContainer" -}}
- name: clickhouse-backup
  image: {{ .Values.clickhouse.backup.image | quote }}
  args: ["server"]
  env:
    - name: API_USERNAME
      value: backup
    - name: GOMEMLIMIT
      value: {{ .Values.clickhouse.backup.goMemoryLimit | quote }}
    - name: API_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ .Values.clickhouse.backup.api.existingSecret }}
          key: password
    - name: AWS_REGION
      value: {{ .Values.clickhouse.backup.aws.region | quote }}
  ports:
    - name: backup-api
      containerPort: 7171
  volumeMounts:
    - name: data
      mountPath: /var/lib/clickhouse
    - name: backup-config
      mountPath: /etc/clickhouse-backup
      readOnly: true
  resources:
    {{- toYaml .Values.clickhouse.backup.resources | nindent 4 }}
  securityContext:
    # The ClickHouse image owns the shared data directory as uid/gid 101.
    runAsNonRoot: true
    runAsUser: 101
    runAsGroup: 101
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
{{- end -}}
