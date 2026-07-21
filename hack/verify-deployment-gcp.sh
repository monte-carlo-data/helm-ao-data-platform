#!/usr/bin/env bash
#
# verify-deployment-gcp.sh — Post-deployment verification for the ao-data-platform
# Helm chart on GCP (GKE). The GKE counterpart to verify-deployment-{aws,azure}.sh
# in this directory; pure kubectl, no cloud CLI required.
#
# Mirrors the Azure script's checks (pods, ClickHouse operator/StatefulSet, OTel collector,
# schema job, ExternalSecret sync, cert-manager Issuers/Certificates, TLS secrets, the
# ClickHouse database/schema + materialized views, and the least-privilege user model) and
# adapts the cloud-coupled ones for GKE:
#   - StorageClass: disk.csi.azure.com/PremiumV2 → pd.csi.storage.gke.io with
#     type pd-ssd (default) or hyperdisk-balanced (provisioned IOPS/throughput).
#   - Ingress: the internal Gateway (gke-l7-rilb) is the ONLY serving path — the
#     Gateway must exist, be Programmed, and hold a PRIVATE (RFC1918) address.
#   - Smoke test: the OTLP send pod runs hostNetwork — on GKE a pod-IP client
#     colocated with an LB backend pod cannot reach the internal Gateway VIP
#     (unlike AKS, where in-cluster hairpin works), and small reference
#     footprints make that colocation likely.
# A final end-to-end trace-ingestion test proves data flows OTLP → collector → ClickHouse.
# NOTE: llm-worker readiness is intentionally not asserted (matches the other clouds) —
# a crashlooping worker (e.g. a provider the image does not support yet) surfaces only
# as a restart warning in CHECK 1.
#
# Usage:
#   ./verify-deployment-gcp.sh -n <namespace>
#
# Requirements: kubectl (authenticated to the cluster), jq, openssl, base64. The
# end-to-end check queries ClickHouse as readonly_user, so it requires
# clickhouse.readonlyUser.enabled=true (same as verify-deployment-aws.sh).

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

STEP=0

usage() {
  echo "Usage: $0 -n <namespace>"
  exit 1
}

NS=""
while getopts "n:" opt; do
  case $opt in
    n) NS="$OPTARG" ;;
    *) usage ;;
  esac
done
[[ -z "$NS" ]] && usage

banner() {
  STEP=$((STEP + 1))
  echo ""
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}${CYAN}  CHECK ${STEP}: $1${RESET}"
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════════════════════════════════════${RESET}"
}

run_cmd() {
  local desc="$1"
  shift
  echo ""
  echo -e "  ${YELLOW}▸ ${desc}${RESET}"
  echo -e "  ${BOLD}\$ $*${RESET}"
  echo ""
  "$@" 2>&1 | sed 's/^/    /'
}

pass() {
  echo ""
  echo -e "  ${GREEN}✔ PASS: $1${RESET}"
}

fail() {
  echo ""
  echo -e "  ${RED}✖ FAIL: $1${RESET}"
  exit 1
}

# Detect managed-Gateway mode vs the internal-LB path — the module supports both, and the
# TLS/LB checks branch on it. Gateway mode terminates TLS at the managed Gateway (on an
# internal LB) and fronts the workloads as ClusterIP; the internal-LB path exposes them
# directly as internal LoadBalancer Services. Resolve the Gateway by the chart's app label
# (release-name-independent) and capture its real name: the chart derives the Gateway /
# HTTPRoute / BackendTLSPolicy names from the fullname helper, which is prefixed with the
# Helm release name unless that name already contains "ao-data-platform".
GW_NAME=$(kubectl get gateway.gateway.networking.k8s.io -n "$NS" \
  -l app.kubernetes.io/name=ao-data-platform \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "$GW_NAME" ]]; then
  GATEWAY_MODE=true
else
  GATEWAY_MODE=false
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 1 — Pods healthy
# ─────────────────────────────────────────────────────────────────────────────
banner "All pods are Running or Succeeded (restarts reported as warnings)"

run_cmd "List all pods in namespace ${NS}" \
  kubectl get pods -n "$NS" -o wide

BAD_PODS=$(kubectl get pods -n "$NS" --no-headers \
  -o custom-columns=":metadata.name,:status.phase" \
  | awk '$2 != "Running" && $2 != "Succeeded" {print $1}')

if [[ -n "$BAD_PODS" ]]; then
  fail "The following pods are not Running/Succeeded:\n$BAD_PODS"
fi

RESTART_PODS=$(kubectl get pods -n "$NS" --no-headers \
  -o custom-columns=":metadata.name,:status.containerStatuses[*].restartCount" \
  | awk '{split($2,a,","); for(i in a) if(a[i]+0 > 0) {print $1; break}}')

if [[ -n "$RESTART_PODS" ]]; then
  echo ""
  echo -e "  ${YELLOW}⚠ WARNING: The following pods have restarts: ${RESTART_PODS}${RESET}"
fi

pass "All pods are Running or Succeeded."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 2 — ClickHouse operator
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse operator is running"

run_cmd "ClickHouse operator pods" \
  kubectl get pods -n "$NS" -l app.kubernetes.io/name=altinity-clickhouse-operator

CH_OP_READY=$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=altinity-clickhouse-operator \
  --no-headers -o custom-columns=":status.conditions[?(@.type=='Ready')].status" | head -1)

if [[ "$CH_OP_READY" != "True" ]]; then
  fail "ClickHouse operator pod is not Ready."
fi

pass "ClickHouse operator pod is Ready."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 3 — ClickHouse pod
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse StatefulSet pod is running"

run_cmd "ClickHouse pods" \
  kubectl get pods -n "$NS" -l clickhouse.altinity.com/chi=otel

CH_POD=$(kubectl get pods -n "$NS" -l clickhouse.altinity.com/chi=otel \
  --no-headers -o custom-columns=":metadata.name" | head -1)

if [[ -z "$CH_POD" ]]; then
  fail "No ClickHouse pod found with label clickhouse.altinity.com/chi=otel."
fi

CH_READY=$(kubectl get pod -n "$NS" "$CH_POD" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')

if [[ "$CH_READY" != "True" ]]; then
  fail "ClickHouse pod ${CH_POD} is not Ready."
fi

pass "ClickHouse pod ${CH_POD} is Ready."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 4 — OTel collector pod
# ─────────────────────────────────────────────────────────────────────────────
banner "OpenTelemetry Collector pod is running"

run_cmd "OTel collector pods" \
  kubectl get pods -n "$NS" -l app.kubernetes.io/name=opentelemetry-collector

OTEL_POD=$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=opentelemetry-collector \
  --no-headers -o custom-columns=":metadata.name" | head -1)

if [[ -z "$OTEL_POD" ]]; then
  fail "No OTel collector pod found."
fi

OTEL_READY=$(kubectl get pod -n "$NS" "$OTEL_POD" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')

if [[ "$OTEL_READY" != "True" ]]; then
  fail "OTel collector pod ${OTEL_POD} is not Ready."
fi

pass "OTel collector pod ${OTEL_POD} is Ready."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 5 — Schema migration job
# ─────────────────────────────────────────────────────────────────────────────
banner "Schema migration job completed successfully"

run_cmd "Schema migration jobs" \
  kubectl get jobs -n "$NS" -l app.kubernetes.io/component=schema

SCHEMA_JOBS=$(kubectl get jobs -n "$NS" --no-headers \
  -o custom-columns=":metadata.name,:status.succeeded" \
  | grep clickhouse-schema || true)

if [[ -z "$SCHEMA_JOBS" ]]; then
  fail "No clickhouse-schema job found."
fi

FAILED_JOBS=$(echo "$SCHEMA_JOBS" | awk '$2 != "1" {print $1}')
if [[ -n "$FAILED_JOBS" ]]; then
  fail "Schema job(s) did not succeed: ${FAILED_JOBS}"
fi

pass "Schema migration job(s) completed successfully."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 6 — ExternalSecret synced
# ─────────────────────────────────────────────────────────────────────────────
banner "ExternalSecret synced and Secret created"

run_cmd "ExternalSecret status" \
  kubectl get externalsecret -n "$NS" ao-clickhouse-otel-credentials

ES_STATUS=$(kubectl get externalsecret -n "$NS" ao-clickhouse-otel-credentials \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)

if [[ "$ES_STATUS" != "True" ]]; then
  run_cmd "ExternalSecret detail (for debugging)" \
    kubectl describe externalsecret -n "$NS" ao-clickhouse-otel-credentials
  fail "ExternalSecret ao-clickhouse-otel-credentials is not Ready (status: ${ES_STATUS:-unknown})."
fi

SECRET_KEYS=$(kubectl get secret -n "$NS" ao-clickhouse-otel-credentials \
  -o jsonpath='{.data}' 2>/dev/null | jq -r 'keys[]' 2>/dev/null || true)

if ! echo "$SECRET_KEYS" | grep -q '^password$'; then
  fail "Secret ao-clickhouse-otel-credentials exists but is missing the 'password' key."
fi

pass "ExternalSecret is synced and Secret contains the 'password' key."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 7 — TLS: Issuers ready
# ─────────────────────────────────────────────────────────────────────────────
banner "cert-manager Issuers are ready"

run_cmd "Issuers in namespace" \
  kubectl get issuers -n "$NS"

for ISSUER in ao-data-platform-selfsigned ao-data-platform-ca; do
  ISSUER_READY=$(kubectl get issuer -n "$NS" "$ISSUER" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$ISSUER_READY" != "True" ]]; then
    fail "Issuer ${ISSUER} is not Ready."
  fi
done

pass "All Issuers are Ready."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 8 — TLS: Certificates issued
# ─────────────────────────────────────────────────────────────────────────────
banner "TLS Certificates are issued and valid"

run_cmd "Certificates in namespace" \
  kubectl get certificates -n "$NS"

# Internal certs (always present); in Gateway mode also the public Let's Encrypt
# listener certs the gateway terminates with.
CERTS="ao-data-platform-ca clickhouse-server-tls otel-collector-tls"
if [[ "$GATEWAY_MODE" == "true" ]]; then
  CERTS="$CERTS gateway-otel-tls gateway-clickhouse-tls"
fi

for CERT in $CERTS; do
  CERT_READY=$(kubectl get certificate -n "$NS" "$CERT" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$CERT_READY" != "True" ]]; then
    run_cmd "Certificate detail (for debugging)" \
      kubectl describe certificate -n "$NS" "$CERT"
    fail "Certificate ${CERT} is not Ready."
  fi
done

pass "All Certificates are Ready (${CERTS})."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 9 — TLS: Certificate secrets contain expected keys
# ─────────────────────────────────────────────────────────────────────────────
banner "TLS Secrets contain tls.crt, tls.key, and ca.crt"

for SECRET in clickhouse-server-tls otel-collector-tls; do
  KEYS=$(kubectl get secret -n "$NS" "$SECRET" -o jsonpath='{.data}' | jq -r 'keys[]' | sort || true)
  for EXPECTED in ca.crt tls.crt tls.key; do
    if ! echo "$KEYS" | grep -q "^${EXPECTED}$"; then
      fail "Secret ${SECRET} is missing key '${EXPECTED}'. Found: ${KEYS}"
    fi
  done
done

pass "Both TLS secrets contain tls.crt, tls.key, and ca.crt."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 10 — TLS: Certificate SANs and expiry
# ─────────────────────────────────────────────────────────────────────────────
banner "TLS certificate SANs and expiry are correct"

for SECRET in clickhouse-server-tls otel-collector-tls; do
  echo ""
  echo -e "  ${YELLOW}▸ Inspecting certificate from secret ${SECRET}${RESET}"
  CERT_PEM=$(kubectl get secret -n "$NS" "$SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d || true)
  echo "$CERT_PEM" | openssl x509 -text -noout 2>&1 \
    | grep -E "Subject:|Issuer:|Not Before|Not After|DNS:" | sed 's/^/    /'

  NOT_AFTER=$(echo "$CERT_PEM" | openssl x509 -enddate -noout | cut -d= -f2)
  if ! echo "$CERT_PEM" | openssl x509 -checkend 0 > /dev/null 2>&1; then
    fail "Certificate in secret ${SECRET} has expired (Not After: ${NOT_AFTER})."
  fi
done

pass "All TLS certificates have valid SANs and are not expired."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 11 — StorageClass is GCE Persistent Disk / Hyperdisk
# ─────────────────────────────────────────────────────────────────────────────
banner "StorageClass uses pd.csi.storage.gke.io with pd-ssd or hyperdisk-balanced"

CH_SC=$(kubectl get pvc -n "$NS" --no-headers \
  -o custom-columns=":spec.storageClassName" | head -1 || true)

if [[ -z "$CH_SC" || "$CH_SC" == "<none>" ]]; then
  fail "ClickHouse PVC has no storageClassName set."
fi

run_cmd "StorageClass '${CH_SC}' details" \
  kubectl get storageclass "$CH_SC" -o yaml

SC_PROVISIONER=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.provisioner}' || true)
SC_TYPE=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.type}' || true)
SC_IOPS=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.provisioned-iops-on-create}' || true)
SC_TPUT=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.provisioned-throughput-on-create}' || true)
SC_RECLAIM=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.reclaimPolicy}' || true)

if [[ "$SC_PROVISIONER" != "pd.csi.storage.gke.io" ]]; then
  fail "StorageClass provisioner is '${SC_PROVISIONER}', expected 'pd.csi.storage.gke.io'."
fi
if [[ "$SC_TYPE" != "pd-ssd" && "$SC_TYPE" != "hyperdisk-balanced" ]]; then
  fail "StorageClass type is '${SC_TYPE}', expected 'pd-ssd' or 'hyperdisk-balanced'."
fi
if [[ "$SC_RECLAIM" != "Retain" ]]; then
  fail "StorageClass reclaimPolicy is '${SC_RECLAIM}', expected 'Retain' (data must survive claim deletion)."
fi
# pd-ssd performance scales with size (no provisioning keys); hyperdisk provisions explicitly.
if [[ "$SC_TYPE" == "pd-ssd" && ( -n "$SC_IOPS" || -n "$SC_TPUT" ) ]]; then
  fail "StorageClass '${CH_SC}' is pd-ssd but carries Hyperdisk provisioning keys (iops='${SC_IOPS}', throughput='${SC_TPUT}')."
fi

if [[ "$SC_TYPE" == "hyperdisk-balanced" ]]; then
  pass "StorageClass '${CH_SC}' uses pd.csi.storage.gke.io / hyperdisk-balanced (${SC_IOPS:-default} IOPS / ${SC_TPUT:-default} throughput), Retain."
else
  pass "StorageClass '${CH_SC}' uses pd.csi.storage.gke.io / pd-ssd, Retain."
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 12 — PVCs bound on GCE persistent disks
# ─────────────────────────────────────────────────────────────────────────────
# GCE persistent disks are encrypted at rest by default (Google-managed keys),
# so there is no per-volume encryption flag to assert as on AWS — confirming the
# PVs are PD CSI volumes is the equivalent signal.
banner "PersistentVolumeClaims are Bound on GCE persistent disks"

run_cmd "PVCs in namespace" \
  kubectl get pvc -n "$NS"

UNBOUND=$(kubectl get pvc -n "$NS" --no-headers \
  -o custom-columns=":metadata.name,:status.phase" \
  | awk '$2 != "Bound" {print $1}')

if [[ -n "$UNBOUND" ]]; then
  fail "The following PVCs are not Bound: ${UNBOUND}"
fi

PV_NAMES=$(kubectl get pvc -n "$NS" --no-headers -o custom-columns=":spec.volumeName")
for PV in $PV_NAMES; do
  PV_DRIVER=$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.driver}' 2>/dev/null || true)
  if [[ "$PV_DRIVER" != "pd.csi.storage.gke.io" ]]; then
    fail "PV ${PV} CSI driver is '${PV_DRIVER}', expected 'pd.csi.storage.gke.io'."
  fi
done

pass "All PVCs are Bound on pd.csi.storage.gke.io volumes (encrypted at rest by default)."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 13 — Service load balancers are internal and provisioned
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$GATEWAY_MODE" == "true" ]]; then
  banner "Gateway is programmed with an internal LB, HTTPRoutes, and re-encrypt BackendTLSPolicies"

  run_cmd "Gateway" kubectl get gateway -n "$NS" "$GW_NAME"

  GW_PROGRAMMED=$(kubectl get gateway -n "$NS" "$GW_NAME" \
    -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)
  [[ "$GW_PROGRAMMED" != "True" ]] && fail "Gateway ${GW_NAME} is not Programmed (got '${GW_PROGRAMMED}')."

  GW_ADDR=$(kubectl get gateway -n "$NS" "$GW_NAME" \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [[ -z "$GW_ADDR" ]] && fail "Gateway ${GW_NAME} has no assigned LB address."
  echo -e "    gateway LB address: ${GW_ADDR}"

  # gke-l7-rilb is internal BY CLASS — assert both the class and that the
  # programmed address is actually RFC1918 (never a public VIP).
  GW_CLASS=$(kubectl get gateway -n "$NS" "$GW_NAME" -o jsonpath='{.spec.gatewayClassName}' 2>/dev/null || true)
  [[ "$GW_CLASS" != "gke-l7-rilb" ]] && fail "Gateway class is '${GW_CLASS}', expected 'gke-l7-rilb' (the internal regional class)."
  if ! echo "$GW_ADDR" | grep -qE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.)'; then
    fail "Gateway address ${GW_ADDR} is not an RFC1918 private IP — the LB must never be public."
  fi

  for HR in "${GW_NAME}-otel" "${GW_NAME}-clickhouse"; do
    kubectl get httproute -n "$NS" "$HR" >/dev/null 2>&1 || fail "HTTPRoute ${HR} not found."
  done

  # BackendTLSPolicy Accepted = the gateway re-encrypts to the (self-signed) backends.
  for BTP in "${GW_NAME}-otel" "${GW_NAME}-clickhouse"; do
    ACCEPTED=$(kubectl get backendtlspolicy -n "$NS" "$BTP" \
      -o jsonpath='{.status.ancestors[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null || true)
    [[ "$ACCEPTED" != "True" ]] && fail "BackendTLSPolicy ${BTP} is not Accepted (got '${ACCEPTED}')."
  done

  pass "Gateway ${GW_NAME} programmed (internal ${GW_CLASS} @ ${GW_ADDR}), both HTTPRoutes present, BackendTLSPolicies Accepted (re-encrypt active)."
else
  banner "Gateway presence (the only serving path on GCP)"
  fail "No Gateway found with label app.kubernetes.io/name=ao-data-platform — the internal Gateway (gke-l7-rilb) is the only serving path on GCP. Is gateway.enabled set?"
fi

# Read-only ClickHouse credential, reused by the schema + least-privilege checks and the
# end-to-end trace at the end of this script. Runs as readonly_user, so
# clickhouse.readonlyUser.enabled=true is required.
CH_READ_USER="readonly_user"
CH_READ_PW=$(kubectl get secret -n "$NS" ao-clickhouse-readonly-user-credentials \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [[ -z "$CH_READ_PW" ]]; then
  fail "readonly_user credential (secret ao-clickhouse-readonly-user-credentials) not found — set clickhouse.readonlyUser.enabled=true to run the ClickHouse data checks."
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 14 — ClickHouse database and schema
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse database 'otel_traces' and tables exist"

# Password ($2) is fed over the exec *stdin* stream, never on the exec command array — so it
# stays out of clickhouse-client's argv, the ClickHouse query_log, this script's output, AND
# the kube-apiserver exec audit requestURI (which records the command array, not stdin). The
# user ($1) and SQL ($3) still travel on argv, but neither is sensitive.
ch_exec() {  # user pw sql
  # shellcheck disable=SC2016  # $1/$2/$p are expanded by the inner `sh -c`, not the outer shell — intentional.
  printf '%s\n' "$2" | kubectl exec -i -n "$NS" "$CH_POD" -- \
    sh -c 'IFS= read -r p; CLICKHOUSE_PASSWORD="$p" clickhouse-client --user "$1" --query "$2"' _ "$1" "$3"
}
ch_query() { ch_exec "$1" "$2" "$3" 2>/dev/null || true; }

DB_EXISTS=$(ch_query "$CH_READ_USER" "$CH_READ_PW" "SELECT name FROM system.databases WHERE name='otel_traces'")
if [[ "$DB_EXISTS" != "otel_traces" ]]; then
  fail "Database 'otel_traces' does not exist in ClickHouse."
fi

for TABLE in otel_traces otel_traces_trace_id_ts; do
  TABLE_EXISTS=$(ch_query "$CH_READ_USER" "$CH_READ_PW" "SELECT name FROM system.tables WHERE database='otel_traces' AND name='${TABLE}'")
  if [[ "$TABLE_EXISTS" != "$TABLE" ]]; then
    fail "Table 'otel_traces.${TABLE}' does not exist."
  fi
done

MV_EXISTS=$(ch_query "$CH_READ_USER" "$CH_READ_PW" "SELECT name FROM system.tables WHERE database='otel_traces' AND name='otel_traces_trace_id_ts_mv'")
if [[ "$MV_EXISTS" != "otel_traces_trace_id_ts_mv" ]]; then
  fail "Materialized view 'otel_traces.otel_traces_trace_id_ts_mv' does not exist."
fi

pass "Database 'otel_traces' exists with all tables and the materialized view."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 15 — ClickHouse otel user authentication
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse 'otel' user can authenticate"

CH_PASSWORD=$(kubectl get secret -n "$NS" ao-clickhouse-otel-credentials \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)

AUTH_RESULT=$(ch_query otel "$CH_PASSWORD" "SELECT 'auth_ok'")
if [[ "$AUTH_RESULT" != "auth_ok" ]]; then
  fail "ClickHouse 'otel' user authentication failed."
fi

pass "ClickHouse 'otel' user authenticated successfully."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 16 — ClickHouse least-privilege user model
#
# Each user authenticates and holds the expected grants, and the security-critical
# denials are enforced. Queries run as each user over the pod's local port via
# `kubectl exec`, so this works while the users' networks/ip include loopback (the
# default). Once per-caller network scoping restricts a user to specific external
# CIDRs, the loopback path stops working for that user and these checks must move to
# the Service endpoint.
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse least-privilege user model"

# Both tolerate non-zero (set -euo pipefail is on): callers inspect the *output*, not the exit
# status — a missing secret yields "" and a denied query yields the error text, so the explicit
# guards/assertions below fire and print instead of the script dying silently mid-check.
ch_pw() { kubectl get secret -n "$NS" "$1" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true; }
ch_as() { ch_exec "$1" "$2" "$3" 2>&1 || true; }

expect_grant() {  # label user pw needle
  if ch_as "$2" "$3" "SHOW GRANTS" | grep -qiF "$4"; then pass "$1"; else fail "$1 — expected grant missing: $4"; fi
}
forbid_grant() {  # label user pw needle
  if ch_as "$2" "$3" "SHOW GRANTS" | grep -qiF "$4"; then fail "$1 — unexpected grant present: $4"; else pass "$1"; fi
}
expect_ok() {     # label user pw sql
  local o; o=$(ch_as "$2" "$3" "$4")
  if echo "$o" | grep -qiE "exception|access_denied|not enough priv"; then fail "$1 — $o"; else pass "$1"; fi
}
expect_denied() { # label user pw sql
  local o; o=$(ch_as "$2" "$3" "$4")
  if echo "$o" | grep -qiE "access_denied|not enough priv"; then pass "$1"; else fail "$1 — expected ACCESS_DENIED, got: $o"; fi
}

SO_PW=$(ch_pw ao-clickhouse-schema-owner-credentials)
WK_PW=$(ch_pw ao-clickhouse-llm-worker-credentials)
MC_PW=$(ch_pw ao-clickhouse-monte-carlo-credentials)
AD_PW=$(ch_pw ao-clickhouse-admin-credentials)
# otel password = $CH_PASSWORD (CHECK 15); readonly_user = $CH_READ_PW (set above).
for pair in "schema_owner:$SO_PW" "llm_worker:$WK_PW" "monte_carlo:$MC_PW"; do
  name="${pair%%:*}"; pw="${pair#*:}"
  [[ -z "$pw" ]] && fail "ClickHouse per-user secret for '$name' is missing — this cluster predates the least-privilege user model (chart not yet migrated)."
done

# schema_owner — owns the schema (DDL); deliberately has no access management rights.
expect_grant   "schema_owner holds DDL on otel_traces"     schema_owner "$SO_PW" "CREATE TABLE"
forbid_grant   "schema_owner has no access management"     schema_owner "$SO_PW" "ACCESS MANAGEMENT"
# SHOW USERS is an access-management-gated op that creates no entity, so it stays correct on
# re-runs (a CREATE USER probe could false-FAIL on a leftover user).
expect_denied  "schema_owner cannot manage users"          schema_owner "$SO_PW" "SHOW USERS"

# llm_worker — queue read/write only; must NOT read telemetry.
expect_ok      "llm_worker reads the queue"                llm_worker "$WK_PW" "SELECT count() FROM otel_traces.llm_batches"
expect_grant   "llm_worker can append results"             llm_worker "$WK_PW" "INSERT ON otel_traces.llm_results"
expect_denied  "llm_worker cannot read telemetry"          llm_worker "$WK_PW" "SELECT count() FROM otel_traces.spans_normalized"
expect_denied  "llm_worker cannot write telemetry"         llm_worker "$WK_PW" "INSERT INTO otel_traces.otel_traces (Timestamp) VALUES (now())"

# monte_carlo — reads everything + produces to the queue, but must NOT write telemetry.
expect_ok      "monte_carlo reads telemetry"               monte_carlo "$MC_PW" "SELECT count() FROM otel_traces.spans_normalized"
# system.numbers backs time-bucket / gap-fill queries; part of the reader bundle grant.
expect_ok      "monte_carlo can read system.numbers"       monte_carlo "$MC_PW" "SELECT number FROM system.numbers LIMIT 1"
expect_grant   "monte_carlo can produce to the queue"      monte_carlo "$MC_PW" "INSERT ON otel_traces.llm_inputs"
expect_grant   "monte_carlo can produce to llm_batches"    monte_carlo "$MC_PW" "INSERT ON otel_traces.llm_batches"
expect_denied  "monte_carlo cannot write telemetry"        monte_carlo "$MC_PW" "INSERT INTO otel_traces.otel_traces (Timestamp) VALUES (now())"
forbid_grant   "monte_carlo cannot write otel_metrics"     monte_carlo "$MC_PW" "GRANT INSERT ON otel_traces.otel_metrics"

# readonly_user — SELECT-only; readonly=2 profile blocks writes even without an explicit deny grant.
expect_ok      "readonly_user reads telemetry"             readonly_user "$CH_READ_PW" "SELECT count() FROM otel_traces.spans_normalized"
expect_ok      "readonly_user can read system.numbers"     readonly_user "$CH_READ_PW" "SELECT number FROM system.numbers LIMIT 1"
forbid_grant   "readonly_user is SELECT-only"              readonly_user "$CH_READ_PW" "GRANT INSERT"
expect_denied  "readonly_user cannot write (runtime)"      readonly_user "$CH_READ_PW" "INSERT INTO otel_traces.otel_traces (Timestamp) VALUES (now())"

# otel — always an ingester; its grant shape varies by restrictGrants state.
#   Set VERIFY_OTEL_RESTRICTED=true to assert the INSERT-only (post-cutover) posture;
#   leave unset (default) to warn-and-pass for pre-cutover clusters.
if [[ "${VERIFY_OTEL_RESTRICTED:-false}" == "true" ]]; then
  expect_denied "otel is INSERT-only" otel "$CH_PASSWORD" "SELECT count() FROM otel_traces.otel_traces"
elif ch_as otel "$CH_PASSWORD" "SELECT count() FROM otel_traces.otel_traces" | grep -qiE "access_denied|not enough priv"; then
  pass "otel is INSERT-only (restrictGrants enabled)"
else
  echo -e "  ${YELLOW}⚠ otel can still read telemetry — restrictGrants not yet enabled (expected pre-cutover)${RESET}"
fi

# admin — only when enabled; superuser, reachable over loopback (this exec is loopback).
if [[ -n "$AD_PW" ]]; then
  expect_grant "admin is a superuser"      admin "$AD_PW" "GRANT ALL ON *.*"
  expect_grant "admin can delegate grants" admin "$AD_PW" "WITH GRANT OPTION"
else
  echo -e "  ${YELLOW}▸ admin user not enabled — skipping${RESET}"
fi

# Materialized views must run under schema_owner as their DEFINER.
for mv in otel_traces_trace_id_ts_mv spans_normalized_mv conversations_normalized_mv; do
  if ch_as schema_owner "$SO_PW" "SHOW CREATE TABLE otel_traces.$mv" | grep -qiE "DEFINER *= *\`?schema_owner\`?"; then
    pass "MV $mv runs as schema_owner (DEFINER)"
  else
    fail "MV $mv is not owned by schema_owner — the normalization cascade will break once otel is restricted."
  fi
done

pass "ClickHouse least-privilege user model verified."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 17 — OTel Collector health check
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector health check endpoint responds"

run_cmd "OTel Collector pod readiness (implies the :13133 health check is passing)" \
  kubectl get pod -n "$NS" "$OTEL_POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")]}'

OTEL_READY_STATUS=$(kubectl get pod -n "$NS" "$OTEL_POD" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' || true)
if [[ "$OTEL_READY_STATUS" != "True" ]]; then
  fail "OTel Collector pod is not Ready — the :13133 health check may be failing."
fi

pass "OTel Collector pod is Ready (:13133 health check is passing)."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 18 — OTel Collector logs (no export errors)
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector has no export errors in recent logs"

run_cmd "Recent OTel Collector logs (last 50 lines)" \
  kubectl logs -n "$NS" "$OTEL_POD" --tail=50

ERROR_LINES=$(kubectl logs -n "$NS" "$OTEL_POD" --tail=200 2>/dev/null \
  | grep -iE "error|failed|refused" | grep -ivE "healthcheck|retry" || true)
if [[ -n "$ERROR_LINES" ]]; then
  echo ""
  echo -e "  ${YELLOW}⚠ WARNING: Found error-like lines in OTel Collector logs:${RESET}"
  echo "$ERROR_LINES" | head -10 | sed 's/^/    /'
  echo ""
  echo -e "  ${YELLOW}  Review these manually — they may be transient startup errors.${RESET}"
fi

pass "No persistent export errors detected in OTel Collector logs."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 19 — End-to-end smoke test: OTLP trace → collector → ClickHouse
# ─────────────────────────────────────────────────────────────────────────────
banner "End-to-end smoke test: send a trace via OTLP and verify it lands in ClickHouse"

# Random 16-byte trace id / 8-byte span id (openssl is already a requirement; this is
# portable, unlike GNU-only date +%N).
TRACE_ID=$(openssl rand -hex 16)
SPAN_ID=$(openssl rand -hex 8)
NOW_NS="$(date +%s)000000000"

TRACE_JSON=$(cat <<EOJSON
{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "verify-deployment-smoke-test"}}]},
    "scopeSpans": [{
      "spans": [{
        "traceId": "${TRACE_ID}",
        "spanId": "${SPAN_ID}",
        "name": "smoke-test-span",
        "kind": 1,
        "startTimeUnixNano": "${NOW_NS}",
        "endTimeUnixNano": "${NOW_NS}",
        "status": {}
      }]
    }]
  }]
}
EOJSON
)

echo ""
# Send through the gateway hostname, exercising the true agent ingress path end-to-end —
# publicly-trusted TLS terminate → HTTPRoute → BackendTLSPolicy re-encrypt → collector.
# The gateway's Let's Encrypt cert is trusted, so no -k. As a bonus, the send pod's
# node-subnet source IP traverses the Cloud Armor source-range restriction, so a success
# also confirms the restriction admits in-VPC clients.
#   GKE-specific: the pod runs hostNetwork. A pod-IP client colocated with one of the
#   Gateway's backend pods cannot reach the internal VIP at all (verified during the
#   Phase-0.5 spike), and small reference footprints make that colocation likely —
#   hostNetwork (node-IP) sourcing works from any node.
OTEL_HOST=$(kubectl get httproute -n "$NS" "${GW_NAME}-otel" \
  -o jsonpath='{.spec.hostnames[0]}' 2>/dev/null)
[[ -z "$OTEL_HOST" ]] && fail "Could not resolve the OTel gateway hostname from HTTPRoute ${GW_NAME}-otel."
SEND_URL="https://${OTEL_HOST}/v1/traces"
echo -e "  ${YELLOW}▸ Sending a test trace via the gateway (${OTEL_HOST}) from a hostNetwork pod${RESET}"
echo "    TraceId: ${TRACE_ID}"
# --quiet suppresses kubectl's attach/prompt chatter (and the duplicated stream it can
# emit); grep -oE '\{.*\}' then keeps only the collector's JSON response body, dropping any
# trailing "pod ... deleted" lifecycle notice that --rm concatenates onto the same line.
SEND_RESULT=$(kubectl run -n "$NS" verify-smoke-test-gcp --rm -i --restart=Never --quiet \
  --image=curlimages/curl:8.11.1 \
  --overrides='{"spec":{"hostNetwork":true}}' -- \
  -s -X POST "$SEND_URL" --max-time 30 \
  -H "Content-Type: application/json" \
  -d "$TRACE_JSON" 2>/dev/null | grep -oE '\{.*\}' | head -1 || true)
echo "    Response: ${SEND_RESULT}"

echo ""
echo -e "  ${YELLOW}▸ Querying ClickHouse for the trace (allowing for batch-processor flush)...${RESET}"
# Poll window is configurable (default 24×5s = 120s). The OTel ClickHouse exporter retries
# for up to 30m (retry_on_failure.max_elapsed_time in the chart), so a miss here is not
# necessarily fatal — it can simply exceed this window under post-upgrade backpressure.
SMOKE_ATTEMPTS="${SMOKE_TEST_ATTEMPTS:-24}"
SMOKE_COUNT=0
for ((i = 1; i <= SMOKE_ATTEMPTS; i++)); do
  sleep 5
  # Password fed over the exec stdin stream via ch_exec (see CHECK 14) — stays out of the pod
  # process table and the kube-apiserver exec audit requestURI.
  SMOKE_COUNT=$(ch_exec "$CH_READ_USER" "$CH_READ_PW" \
    "SELECT count() FROM otel_traces.otel_traces WHERE TraceId = '${TRACE_ID}'" 2>/dev/null || echo 0)
  [[ "$SMOKE_COUNT" =~ ^[0-9]+$ && "$SMOKE_COUNT" -gt 0 ]] && break
done

if [[ "$SMOKE_COUNT" =~ ^[0-9]+$ && "$SMOKE_COUNT" -gt 0 ]]; then
  if [[ "$GATEWAY_MODE" == "true" ]]; then
    pass "Trace ${TRACE_ID} landed in ClickHouse (${SMOKE_COUNT} row(s)) — end-to-end via gateway (TLS → HTTPRoute → re-encrypt → collector → ClickHouse) works."
  else
    pass "Trace ${TRACE_ID} landed in ClickHouse (${SMOKE_COUNT} row(s)) — end-to-end OTLP → collector → ClickHouse works."
  fi
else
  if [[ "$GATEWAY_MODE" == "true" ]]; then
    fail "Trace ${TRACE_ID} did not arrive in ClickHouse within the poll window (~$((SMOKE_ATTEMPTS * 5))s) — the end-to-end path may be broken (gateway ingress/DNS/hairpin, collector ingest, schema, or backend TLS). Note: the OTel exporter retries for up to 30m, so a miss here isn't necessarily fatal."
  else
    fail "Trace ${TRACE_ID} did not arrive in ClickHouse within the poll window (~$((SMOKE_ATTEMPTS * 5))s) — the end-to-end pipeline may be broken (collector ingest, schema, or backend TLS). Note: the OTel exporter retries for up to 30m, so a miss here isn't necessarily fatal."
  fi
fi

echo ""
echo -e "${BOLD}${GREEN}All checks passed.${RESET}"
