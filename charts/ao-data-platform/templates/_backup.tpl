{{- define "ao-data-platform.backupValidate" -}}
{{- $b := .Values.clickhouse.backup -}}
{{- if not (kindIs "bool" $b.cleanup.enabled) -}}{{- fail "clickhouse.backup.cleanup.enabled must be a boolean." -}}{{- end -}}
{{- if $b.enabled -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString .Values.clickhouse.replicasCount))) (lt (int .Values.clickhouse.replicasCount) 1) -}}{{- fail "clickhouse.replicasCount must be a positive integer when backups are enabled." -}}{{- end -}}
{{- if ne $b.provider "aws" -}}{{- fail "clickhouse.backup.provider currently supports only aws." -}}{{- end -}}
{{- range $key := list "bucket" "region" "roleArn" "path" -}}
{{- if not (index $b.aws $key) -}}{{- fail (printf "clickhouse.backup.aws.%s is required." $key) -}}{{- end -}}
{{- end -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$" $b.aws.bucket) -}}{{- fail "clickhouse.backup.aws.bucket must be an S3 bucket name." -}}{{- end -}}
{{- if not (regexMatch "^[a-z0-9-]+$" $b.aws.region) -}}{{- fail "clickhouse.backup.aws.region must be an AWS region." -}}{{- end -}}
{{- if not (regexMatch "^[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)*$" $b.aws.path) -}}{{- fail "clickhouse.backup.aws.path must contain safe directory names without leading or trailing slashes." -}}{{- end -}}
{{- if or (not $b.serviceAccount.name) (eq $b.serviceAccount.name "default") -}}{{- fail "clickhouse.backup.serviceAccount.name must be a dedicated account, not default." -}}{{- end -}}
{{- if not $b.user.secret -}}{{- fail "clickhouse.backup.user.secret must name the backup credentials Secret." -}}{{- end -}}
{{/* The startup wrapper and scheduler were verified against this exact stock build; mirrors must preserve its digest. */}}
{{- if not (regexMatch "^[^[:space:]@]+@sha256:08016b048f7e6035c048501315c2a788e5a782f15f168e042c7bd48d5a388cc4$" $b.sidecar.image) -}}{{- fail "clickhouse.backup.sidecar.image must use the verified Altinity 2.8.1 digest; another registry or repository is allowed." -}}{{- end -}}
{{- if not $b.probe.secret -}}{{- fail "clickhouse.backup.probe.secret is required." -}}{{- end -}}
{{- if not (regexMatch "^[a-zA-Z0-9._-]+$" (toString $b.api.passwordRevision)) -}}{{- fail "clickhouse.backup.api.passwordRevision must be a nonempty revision using letters, digits, dots, underscores, or hyphens." -}}{{- end -}}
{{- $externalAPI := not (empty (dig "secretStoreRef" "name" "" ($b.api.externalSecret | default dict))) -}}
{{- if eq (not (empty $b.api.existingSecret)) $externalAPI -}}{{- fail "Set exactly one of clickhouse.backup.api.existingSecret or api.externalSecret." -}}{{- end -}}
{{- if and $externalAPI (dig "remoteRef" "property" "" $b.api.externalSecret) -}}{{- fail "clickhouse.backup.api.externalSecret.remoteRef.property must be empty; the source must contain password and revision together." -}}{{- end -}}
{{- $apiSecret := include "ao-data-platform.backupAPISecretName" . -}}
{{- if or (eq $b.user.secret $b.probe.secret) (eq $apiSecret $b.user.secret) (eq $apiSecret $b.probe.secret) -}}{{- fail "Backup database, probe, and API credentials must use separate Secrets." -}}{{- end -}}
{{- $sharedSecrets := list .Values.clickhouse.authMethods.secret .Values.clickhouse.otel.secret .Values.clickhouse.schemaOwner.secret .Values.clickhouse.llmWorker.secret .Values.clickhouse.monteCarlo.secret .Values.clickhouse.admin.secret .Values.clickhouse.readonlyUser.secret -}}
{{- range $name := list $b.user.secret $b.probe.secret $apiSecret -}}
{{- if has $name $sharedSecrets -}}{{- fail "Backup credentials must not reuse a Secret belonging to another ClickHouse user or the shared auth bundle." -}}{{- end -}}
{{- end -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString $b.schedule.timeoutSeconds))) (lt (int $b.schedule.timeoutSeconds) 60) (gt (int $b.schedule.timeoutSeconds) 14400) -}}{{- fail "clickhouse.backup.schedule.timeoutSeconds must be an integer from 60 to 14400." -}}{{- end -}}
{{- range $key := list "maxReplicaDelaySeconds" "freshnessRetrySeconds" -}}
{{- if not (regexMatch "^[0-9]+$" (toString (index $b.schedule $key))) -}}{{- fail (printf "clickhouse.backup.schedule.%s must be a nonnegative integer." $key) -}}{{- end -}}
{{- end -}}
{{- if not (regexMatch "^[^[:space:]@]+@sha256:[a-f0-9]{64}$" $b.schedule.image) -}}{{- fail "clickhouse.backup.schedule.image must include an immutable sha256 digest." -}}{{- end -}}
{{- range $port := $b.networkPolicy.additionalPorts -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString $port))) (lt (int $port) 1) (gt (int $port) 65535) (eq (int $port) 7171) -}}
{{- fail "clickhouse.backup.networkPolicy.additionalPorts must contain TCP ports from 1 to 65535, excluding the protected backup port 7171." -}}
{{- end -}}
{{- end -}}
{{- if $b.cleanup.enabled -}}
{{- if ne (toString $b.cleanup.dryRun) "true" -}}{{- fail "Backup deletion is unavailable with stock clickhouse-backup 2.8.1; cleanup.dryRun must remain true." -}}{{- end -}}
{{- range $key, $minimum := dict "keepLast" 1 "keepDays" 0 "timeoutSeconds" 60 -}}
{{- $value := index $b.cleanup $key -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString $value))) (lt (int $value) $minimum) -}}{{- fail (printf "clickhouse.backup.cleanup.%s must be an integer >= %d." $key $minimum) -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- else -}}
{{- if $b.cleanup.enabled -}}{{- fail "Backup cleanup requires clickhouse.backup.enabled." -}}{{- end -}}
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

{{- define "ao-data-platform.backupAPISecretName" -}}
{{- .Values.clickhouse.backup.api.existingSecret | default .Values.clickhouse.backup.api.secret | default (printf "%s-backup-api" (include "ao-data-platform.chiName" .)) -}}
{{- end -}}

{{- define "ao-data-platform.backupDatabaseEndpoints" -}}
{{- $endpoints := list -}}
{{- $ports := include "ao-data-platform.clickhousePorts" . | fromJson -}}
{{- $scheme := ternary "https" "http" .Values.tls.enabled -}}
{{- $port := ternary (int $ports.https) (int $ports.http) .Values.tls.enabled -}}
{{- range $replica := (include "ao-data-platform.backupReplicas" . | fromJsonArray) -}}
{{- $endpoints = append $endpoints (printf "%s://%s-backup-%d:%d" $scheme (include "ao-data-platform.chiName" $) (int $replica) $port) -}}
{{- end -}}
{{- toJson $endpoints -}}
{{- end -}}

{{- define "ao-data-platform.backupDisk" -}}
{{- $b := .Values.clickhouse.backup -}}
<clickhouse>
  <!-- SQL S3 queries must not borrow the server's backup credentials. -->
  <s3><use_environment_credentials>0</use_environment_credentials></s3>
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
{{- $ports := include "ao-data-platform.clickhousePorts" . | fromJson -}}
general:
  remote_storage: s3
  backups_to_keep_local: 0
  backups_to_keep_remote: 0
  rbac_backup_always: false
  use_resumable_state: false
  log_level: info
clickhouse:
  host: 127.0.0.1
  port: {{ $ports.tcp }}
  username: backup
  password: {{ "{{ .password | quote }}" }}
  # Stock Altinity requires at least four hours for embedded backups.
  timeout: "4h"
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
  image: {{ .Values.clickhouse.backup.sidecar.image | quote }}
  command: ["/bin/sh", "/scripts/start-backup.sh"]
  env:
    - name: API_USERNAME
      value: backup
    - name: GOMEMLIMIT
      value: {{ .Values.clickhouse.backup.sidecar.goMemoryLimit | quote }}
    - name: BACKUP_PASSWORD_REVISION
      value: {{ .Values.clickhouse.backup.api.passwordRevision | quote }}
    - name: AWS_REGION
      value: {{ .Values.clickhouse.backup.aws.region | quote }}
  ports:
    - name: backup-api
      containerPort: 7171
  volumeMounts:
    - name: backup-startup
      mountPath: /scripts
      readOnly: true
    - name: backup-runtime
      mountPath: /tmp/clickhouse-backup
    - name: data
      mountPath: /var/lib/clickhouse
    - name: backup-config
      mountPath: /etc/clickhouse-backup
      readOnly: true
    - name: backup-api-credentials
      mountPath: /etc/clickhouse-backup-api
      readOnly: true
  resources:
    {{- toYaml .Values.clickhouse.backup.sidecar.resources | nindent 4 }}
  securityContext:
    # The ClickHouse image owns the shared data directory as uid/gid 101.
    runAsNonRoot: true
    runAsUser: 101
    runAsGroup: 101
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
{{- end -}}

{{/* One StatefulSet per replica, with Pod ordinal zero in the single-shard layout. */}}
{{- define "ao-data-platform.backupPodNames" -}}
{{- $names := list -}}
{{- range $replica := (include "ao-data-platform.backupReplicas" . | fromJsonArray) -}}
{{- $names = append $names (printf "chi-%s-%s-0-%d-0" (include "ao-data-platform.chiName" $) (include "ao-data-platform.clickhouseClusterName" $) (int $replica)) -}}
{{- end -}}
{{- toJson $names -}}
{{- end -}}
