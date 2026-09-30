#!/bin/sh
# Secret volumes may arrive after the database starts. Do not start an API with
# defaults or an empty password. This container has no readiness dependency.
set +x
set -eu

config_path=${CLICKHOUSE_BACKUP_CONFIG:-/etc/clickhouse-backup/config.yml}
config_dir=$(dirname "$config_path")
secret_dir=${BACKUP_API_SECRET_DIR:-/etc/clickhouse-backup-api}
expected_revision=${BACKUP_PASSWORD_REVISION:-}
export API_USERNAME=backup
unset API_PASSWORD
trap 'exit 0' TERM INT

echo 'Waiting for valid backup configuration and the matching API credential revision.'
while :; do
    if [ -n "$expected_revision" ] && [ -s "$config_path" ] && [ -s "$config_dir/password" ]; then
        # Resolve one projected Secret generation before reading both keys. A
        # Kubernetes symlink swap must not mix an old revision with a new password.
        revision_path=$(readlink -f "$secret_dir/revision" 2>/dev/null || true)
        generation_dir=${revision_path%/*}
        actual_revision=$(cat "$revision_path" 2>/dev/null || true)
        if [ -n "$revision_path" ] && [ "$actual_revision" = "$expected_revision" ]; then
            # Keep trailing newlines in the password; do not print it or write a
            # copy. The Secret volume can remain read-only throughout startup.
            if api_password=$(cat "$generation_dir/password" 2>/dev/null && printf '.'); then
                api_password=${api_password%.}
                if [ -n "$api_password" ]; then
                    export API_PASSWORD="$api_password"
                    if /usr/local/bin/check-backup-config "$config_path" >/dev/null 2>&1; then
                        unset api_password
                        echo 'Backup credentials are ready; starting the authenticated API.'
                        exec /bin/clickhouse-backup --config "$config_path" server
                    fi
                fi
            fi
        fi
    fi
    unset API_PASSWORD
    sleep 2 &
    wait "$!" || true
done
