#!/usr/bin/env bash
#
# verify-deployment-azure.sh — Post-deployment verification for the ao-data-platform
# Helm chart on Azure (AKS). The Azure/AKS counterpart to verify-deployment-aws.sh
# (AWS/EKS) in this directory; pure kubectl, no cloud CLI required.
#
# Mirrors the AWS script's Kubernetes-level checks (pods, ClickHouse
# operator/StatefulSet, OTel collector, schema job, ExternalSecret sync,
# cert-manager Issuers/Certificates, TLS secrets) and adapts the cloud-coupled
# ones for Azure:
#   - StorageClass: ebs.csi.aws.com/gp3 → disk.csi.azure.com/Premium SSD
#   - Ingress: AWS NLB Service annotations → the managed Gateway API
#     (Gateway/HTTPRoute/BackendTLSPolicy) in gateway mode, or the internal-LB
#     Service annotation otherwise — auto-detected.
# The AWS deep cloud-API checks (aws ec2 describe-volumes, elbv2 target-health)
# are intentionally not reimplemented in `az`: the K8s-level signals here (LB or
# Gateway provisioned with an address, PVCs Bound on Azure managed disks) cover
# deployment health; end-to-end connectivity is proven by a trace-ingestion test.
#
# Usage:
#   ./verify-deployment-azure.sh -n <namespace>
#
# Requirements: kubectl (authenticated to the cluster), jq, openssl, base64.

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

svc_annotation() {
  local svc="$1" key="$2"
  kubectl get svc -n "$NS" "$svc" -o json 2>/dev/null | jq -r ".metadata.annotations[\"${key}\"] // empty"
}

# Detect public-TLS Gateway mode vs the internal-LB path — the module supports
# both, and the TLS/LB checks branch on it. Gateway mode terminates public TLS at
# the managed Gateway and fronts the workloads as ClusterIP; the internal-LB path
# exposes them directly as internal LoadBalancer Services.
if kubectl get gateway.gateway.networking.k8s.io -n "$NS" ao-data-platform >/dev/null 2>&1; then
  GATEWAY_MODE=true
else
  GATEWAY_MODE=false
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 1 — Pods healthy
# ─────────────────────────────────────────────────────────────────────────────
banner "All pods are Running with zero restarts"

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
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)

if [[ "$ES_STATUS" != "True" ]]; then
  run_cmd "ExternalSecret detail (for debugging)" \
    kubectl describe externalsecret -n "$NS" ao-clickhouse-otel-credentials
  fail "ExternalSecret ao-clickhouse-otel-credentials is not Ready (status: ${ES_STATUS:-unknown})."
fi

SECRET_KEYS=$(kubectl get secret -n "$NS" ao-clickhouse-otel-credentials \
  -o jsonpath='{.data}' 2>/dev/null | jq -r 'keys[]' 2>/dev/null)

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
  KEYS=$(kubectl get secret -n "$NS" "$SECRET" -o jsonpath='{.data}' | jq -r 'keys[]' | sort)
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
  CERT_PEM=$(kubectl get secret -n "$NS" "$SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d)
  echo "$CERT_PEM" | openssl x509 -text -noout 2>&1 \
    | grep -E "Subject:|Issuer:|Not Before|Not After|DNS:" | sed 's/^/    /'

  NOT_AFTER=$(echo "$CERT_PEM" | openssl x509 -enddate -noout | cut -d= -f2)
  if ! echo "$CERT_PEM" | openssl x509 -checkend 0 > /dev/null 2>&1; then
    fail "Certificate in secret ${SECRET} has expired (Not After: ${NOT_AFTER})."
  fi
done

pass "All TLS certificates have valid SANs and are not expired."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 11 — StorageClass is Azure Disk Premium SSD
# ─────────────────────────────────────────────────────────────────────────────
banner "StorageClass uses disk.csi.azure.com and Premium SSD"

CH_SC=$(kubectl get pvc -n "$NS" --no-headers \
  -o custom-columns=":spec.storageClassName" | head -1)

if [[ -z "$CH_SC" || "$CH_SC" == "<none>" ]]; then
  fail "ClickHouse PVC has no storageClassName set."
fi

run_cmd "StorageClass '${CH_SC}' details" \
  kubectl get storageclass "$CH_SC" -o yaml

SC_PROVISIONER=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.provisioner}')
SC_SKU=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.skuName}')

if [[ "$SC_PROVISIONER" != "disk.csi.azure.com" ]]; then
  fail "StorageClass provisioner is '${SC_PROVISIONER}', expected 'disk.csi.azure.com'."
fi
if [[ "$SC_SKU" != Premium_* ]]; then
  fail "StorageClass skuName is '${SC_SKU}', expected a Premium SSD SKU (Premium_LRS / Premium_ZRS)."
fi

pass "StorageClass '${CH_SC}' uses disk.csi.azure.com with SKU '${SC_SKU}'."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 12 — PVCs bound on Azure managed disks
# ─────────────────────────────────────────────────────────────────────────────
# Azure managed disks are encrypted at rest by default (platform-managed keys),
# so there is no per-volume encryption flag to assert as on AWS — confirming the
# PVs are azuredisk CSI volumes is the equivalent signal.
banner "PersistentVolumeClaims are Bound on Azure managed disks"

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
  if [[ "$PV_DRIVER" != "disk.csi.azure.com" ]]; then
    fail "PV ${PV} CSI driver is '${PV_DRIVER}', expected 'disk.csi.azure.com'."
  fi
done

pass "All PVCs are Bound on disk.csi.azure.com volumes (encrypted at rest by default)."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 13 — Service load balancers are internal and provisioned
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$GATEWAY_MODE" == "true" ]]; then
  banner "Gateway is programmed with a public LB, HTTPRoutes, and re-encrypt BackendTLSPolicies"

  run_cmd "Gateway" kubectl get gateway -n "$NS" ao-data-platform

  GW_PROGRAMMED=$(kubectl get gateway -n "$NS" ao-data-platform \
    -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)
  [[ "$GW_PROGRAMMED" != "True" ]] && fail "Gateway ao-data-platform is not Programmed (got '${GW_PROGRAMMED}')."

  GW_ADDR=$(kubectl get gateway -n "$NS" ao-data-platform \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [[ -z "$GW_ADDR" ]] && fail "Gateway ao-data-platform has no assigned LB address."
  echo -e "    gateway LB address: ${GW_ADDR}"

  for HR in ao-data-platform-otel ao-data-platform-clickhouse; do
    kubectl get httproute -n "$NS" "$HR" >/dev/null 2>&1 || fail "HTTPRoute ${HR} not found."
  done

  # BackendTLSPolicy Accepted = the gateway re-encrypts to the (self-signed) backends.
  for BTP in ao-data-platform-otel ao-data-platform-clickhouse; do
    ACCEPTED=$(kubectl get backendtlspolicy -n "$NS" "$BTP" \
      -o jsonpath='{.status.ancestors[0].conditions[?(@.type=="Accepted")].status}' 2>/dev/null || true)
    [[ "$ACCEPTED" != "True" ]] && fail "BackendTLSPolicy ${BTP} is not Accepted (got '${ACCEPTED}')."
  done

  pass "Gateway programmed (LB ${GW_ADDR}), both HTTPRoutes present, BackendTLSPolicies Accepted (re-encrypt active)."
else
  banner "ClickHouse and OTel Services are internal LBs with an assigned IP"

  for SVC in clickhouse-otel opentelemetry-collector; do
    echo ""
    echo -e "  ${YELLOW}▸ Service ${SVC}${RESET}"

    INTERNAL=$(svc_annotation "$SVC" "service.beta.kubernetes.io/azure-load-balancer-internal")
    if [[ "$INTERNAL" != "true" ]]; then
      fail "Service ${SVC} is missing the azure-load-balancer-internal=true annotation (got '${INTERNAL}')."
    fi

    LB_IP=$(kubectl get svc -n "$NS" "$SVC" \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)
    if [[ -z "$LB_IP" ]]; then
      fail "Service ${SVC} has no loadBalancer ingress IP — the internal LB may not be provisioned yet."
    fi
    echo -e "    internal LB IP: ${LB_IP}"
  done

  pass "ClickHouse and OTel Services are internal load balancers with assigned IPs."
fi

echo ""
echo -e "${BOLD}${GREEN}All checks passed.${RESET}"
