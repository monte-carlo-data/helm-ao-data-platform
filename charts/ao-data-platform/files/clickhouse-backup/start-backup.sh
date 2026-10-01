#!/bin/sh
# Wait for projected Secrets, then keep one checked configuration for this process.
set +x
set -eu
umask 077

config_path=${CLICKHOUSE_BACKUP_CONFIG:-/etc/clickhouse-backup/config.yml}
secret_dir=${BACKUP_API_SECRET_DIR:-/etc/clickhouse-backup-api}
expected_revision=${BACKUP_PASSWORD_REVISION:-}
runtime_dir=/tmp/clickhouse-backup
snapshot=$runtime_dir/config.yml
export API_USERNAME=backup CLICKHOUSE_USERNAME=backup
unset API_PASSWORD CLICKHOUSE_PASSWORD
trap 'exit 0' TERM INT

wait_for_credentials() {
    # Reasons are fixed strings. Never log a credential or parser output.
    echo "Waiting for backup credentials: $1."
    unset API_PASSWORD CLICKHOUSE_PASSWORD
    sleep 2 &
    wait "$!" || true
}

while :; do
    if [ -z "$expected_revision" ]; then
        wait_for_credentials 'expected API revision is missing'
        continue
    fi
    # Resolve one generation for each Secret before reading its related keys.
    db_config=$(readlink -f "$config_path" 2>/dev/null || true)
    db_generation=${db_config%/*}
    api_revision=$(readlink -f "$secret_dir/revision" 2>/dev/null || true)
    api_generation=${api_revision%/*}
    if [ -z "$db_config" ] || [ ! -s "$db_config" ]; then
        wait_for_credentials 'database configuration is missing'
        continue
    fi
    if ! db_password=$(cat "$db_generation/password" 2>/dev/null && printf '.'); then
        wait_for_credentials 'database password is missing'
        continue
    fi
    db_password=${db_password%.}
    if [ -z "$db_password" ]; then
        wait_for_credentials 'database password is empty'
        continue
    fi
    actual_revision=$(cat "$api_revision" 2>/dev/null || true)
    if [ -z "$actual_revision" ] || [ "$actual_revision" != "$expected_revision" ]; then
        wait_for_credentials 'API revision is missing or does not match'
        continue
    fi
    if ! api_password=$(cat "$api_generation/password" 2>/dev/null && printf '.'); then
        wait_for_credentials 'API password is missing'
        continue
    fi
    api_password=${api_password%.}
    if [ -z "$api_password" ]; then
        wait_for_credentials 'API password is empty'
        continue
    fi
    # The API and database passwords remain fixed even if mounted Secrets rotate.
    export API_PASSWORD="$api_password" CLICKHOUSE_PASSWORD="$db_password"
    unset api_password db_password
    if ! cp "$db_config" "$snapshot" 2>/dev/null; then
        wait_for_credentials 'database configuration could not be copied'
        continue
    fi
    # Validate the same snapshot used by the server. Both streams may contain
    # credentials on parse failures, so neither stream belongs in container logs.
    if ! /bin/clickhouse-backup --config "$snapshot" print-config >/dev/null 2>&1; then
        rm -f "$snapshot"
        wait_for_credentials 'database configuration is not valid'
        continue
    fi
    echo 'Backup credentials are ready; starting the authenticated API.'
    # Successful startup intentionally replaces this waiting loop.
    # shellcheck disable=SC2093
    exec /bin/clickhouse-backup --config "$snapshot" server
done
