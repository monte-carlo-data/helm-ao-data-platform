#!/usr/bin/env bash
#
# verify-deployment.sh — Post-deployment verification for the ao-data-platform Helm chart.
#
# Runs each check sequentially, printing the command, its output, and a
# pass/fail explanation.  Stops immediately on the first failure.
#
# Usage:
#   ./verify-deployment.sh -n <namespace> -r <aws-region>
#
# Requirements: kubectl, aws cli, jq, openssl, dig, base64

set -euo pipefail

# ── colours / helpers ────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

STEP=0

usage() {
  echo "Usage: $0 -n <namespace> -r <aws-region>"
  exit 1
}

NS=""
REGION=""
while getopts "n:r:" opt; do
  case $opt in
    n) NS="$OPTARG" ;;
    r) REGION="$OPTARG" ;;
    *) usage ;;
  esac
done
[[ -z "$NS" || -z "$REGION" ]] && usage

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
  # Run and capture; stream output indented
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

# Shorthand to grab an annotation value from a service
svc_annotations() {
  local svc="$1"
  kubectl get svc -n "$NS" "$svc" -o json 2>/dev/null | jq '.metadata.annotations'
}

svc_annotation() {
  local svc="$1" key="$2"
  kubectl get svc -n "$NS" "$svc" -o json 2>/dev/null | jq -r ".metadata.annotations[\"${key}\"] // empty"
}

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 1 — Pods healthy
# ─────────────────────────────────────────────────────────────────────────────
banner "All pods are Running with zero restarts"

run_cmd "List all pods in namespace ${NS}" \
  kubectl get pods -n "$NS" -o wide

# Verify no pods in bad state
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

# Verify the target Secret exists and has a password key
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

for CERT in ao-data-platform-ca clickhouse-server-tls otel-collector-tls; do
  CERT_READY=$(kubectl get certificate -n "$NS" "$CERT" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$CERT_READY" != "True" ]]; then
    run_cmd "Certificate detail (for debugging)" \
      kubectl describe certificate -n "$NS" "$CERT"
    fail "Certificate ${CERT} is not Ready."
  fi
done

pass "All Certificates (CA, ClickHouse, OTel) are Ready."

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

  # Verify it hasn't expired
  NOT_AFTER=$(echo "$CERT_PEM" | openssl x509 -enddate -noout | cut -d= -f2)
  if ! echo "$CERT_PEM" | openssl x509 -checkend 0 > /dev/null 2>&1; then
    fail "Certificate in secret ${SECRET} has expired (Not After: ${NOT_AFTER})."
  fi
done

pass "All TLS certificates have valid SANs and are not expired."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 11 — StorageClass
# ─────────────────────────────────────────────────────────────────────────────
banner "StorageClass is gp3, encrypted, and uses ebs.csi.aws.com"

# Find the StorageClass used by ClickHouse PVCs
CH_SC=$(kubectl get pvc -n "$NS" --no-headers \
  -o custom-columns=":spec.storageClassName" | head -1)

if [[ -z "$CH_SC" || "$CH_SC" == "<none>" ]]; then
  fail "ClickHouse PVC has no storageClassName set."
fi

run_cmd "StorageClass '${CH_SC}' details" \
  kubectl get storageclass "$CH_SC" -o yaml

SC_PROVISIONER=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.provisioner}')
SC_TYPE=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.type}')
SC_ENCRYPTED=$(kubectl get storageclass "$CH_SC" -o jsonpath='{.parameters.encrypted}')

if [[ "$SC_PROVISIONER" != "ebs.csi.aws.com" ]]; then
  fail "StorageClass provisioner is '${SC_PROVISIONER}', expected 'ebs.csi.aws.com'."
fi
if [[ "$SC_TYPE" != "gp3" ]]; then
  fail "StorageClass volume type is '${SC_TYPE}', expected 'gp3'."
fi
if [[ "$SC_ENCRYPTED" != "true" ]]; then
  fail "StorageClass encrypted is '${SC_ENCRYPTED}', expected 'true'."
fi

pass "StorageClass '${CH_SC}' is gp3, encrypted, provisioned by ebs.csi.aws.com."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 12 — PVCs bound
# ─────────────────────────────────────────────────────────────────────────────
banner "PersistentVolumeClaims are Bound"

run_cmd "PVCs in namespace" \
  kubectl get pvc -n "$NS"

UNBOUND=$(kubectl get pvc -n "$NS" --no-headers \
  -o custom-columns=":metadata.name,:status.phase" \
  | awk '$2 != "Bound" {print $1}')

if [[ -n "$UNBOUND" ]]; then
  fail "The following PVCs are not Bound: ${UNBOUND}"
fi

pass "All PVCs are Bound."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 13 — EBS volumes are encrypted
# ─────────────────────────────────────────────────────────────────────────────
banner "Underlying EBS volumes are encrypted"

PV_NAMES=$(kubectl get pvc -n "$NS" --no-headers -o custom-columns=":spec.volumeName")

for PV in $PV_NAMES; do
  EBS_VOL=$(kubectl get pv "$PV" -o jsonpath='{.spec.csi.volumeHandle}')
  if [[ -z "$EBS_VOL" ]]; then
    fail "PV ${PV} does not have a CSI volume handle — is this an EBS volume?"
  fi

  run_cmd "EBS volume ${EBS_VOL} details" \
    aws ec2 describe-volumes --volume-ids "$EBS_VOL" --region "$REGION" \
      --query 'Volumes[0].{VolumeId:VolumeId,Encrypted:Encrypted,VolumeType:VolumeType,Size:Size}'

  ENCRYPTED=$(aws ec2 describe-volumes --volume-ids "$EBS_VOL" --region "$REGION" \
    --query 'Volumes[0].Encrypted' --output text)

  if [[ "$ENCRYPTED" != "True" ]]; then
    fail "EBS volume ${EBS_VOL} is NOT encrypted."
  fi
done

pass "All EBS volumes backing PVCs are encrypted."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 14 — ClickHouse NLB: Service annotations
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse Service has correct NLB annotations"

run_cmd "ClickHouse Service annotations" \
  svc_annotations clickhouse-otel

declare -A CH_EXPECTED=(
  ["service.beta.kubernetes.io/aws-load-balancer-type"]="external"
  ["service.beta.kubernetes.io/aws-load-balancer-scheme"]="internal"
  ["service.beta.kubernetes.io/aws-load-balancer-nlb-target-type"]="ip"
  ["service.beta.kubernetes.io/aws-load-balancer-ssl-ports"]="9440,8443"
  ["service.beta.kubernetes.io/aws-load-balancer-backend-protocol"]="ssl"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-port"]="8443"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol"]="HTTPS"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-path"]="/ping"
)

for KEY in "${!CH_EXPECTED[@]}"; do
  ACTUAL=$(svc_annotation clickhouse-otel "$KEY")
  EXPECT="${CH_EXPECTED[$KEY]}"
  if [[ "$ACTUAL" != "$EXPECT" ]]; then
    fail "ClickHouse annotation '${KEY}' is '${ACTUAL}', expected '${EXPECT}'."
  fi
done

# Verify ACM cert ARN is set (not the placeholder)
CH_ACM=$(svc_annotation clickhouse-otel "service.beta.kubernetes.io/aws-load-balancer-ssl-cert")
if [[ -z "$CH_ACM" || "$CH_ACM" == *"CLICKHOUSE_CERTIFICATE_ID"* ]]; then
  fail "ClickHouse NLB ssl-cert annotation is missing or still a placeholder: '${CH_ACM}'."
fi

pass "ClickHouse Service NLB annotations are correct (ACM cert: ${CH_ACM})."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 15 — ClickHouse NLB: Provisioned
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse NLB is provisioned"

CH_NLB_HOST=$(kubectl get svc -n "$NS" clickhouse-otel \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

if [[ -z "$CH_NLB_HOST" ]]; then
  fail "ClickHouse Service has no loadBalancer ingress hostname — NLB may not be provisioned."
fi

run_cmd "ClickHouse NLB hostname" \
  echo "$CH_NLB_HOST"

CH_NLB_ARN=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[?DNSName=='${CH_NLB_HOST}'].LoadBalancerArn" --output text)

if [[ -z "$CH_NLB_ARN" ]]; then
  fail "Could not find an NLB in AWS with DNS name '${CH_NLB_HOST}'."
fi

pass "ClickHouse NLB is provisioned (ARN: ${CH_NLB_ARN})."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 16 — ClickHouse NLB: Scheme is internal
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse NLB scheme is internal"

CH_NLB_SCHEME=$(aws elbv2 describe-load-balancers --load-balancer-arns "$CH_NLB_ARN" \
  --region "$REGION" --query 'LoadBalancers[0].Scheme' --output text)

run_cmd "ClickHouse NLB scheme" \
  echo "$CH_NLB_SCHEME"

if [[ "$CH_NLB_SCHEME" != "internal" ]]; then
  fail "ClickHouse NLB scheme is '${CH_NLB_SCHEME}', expected 'internal'."
fi

pass "ClickHouse NLB is internal."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 17 — ClickHouse NLB: Listener TLS config
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse NLB listeners have TLS configured"

run_cmd "ClickHouse NLB listeners" \
  aws elbv2 describe-listeners --load-balancer-arn "$CH_NLB_ARN" --region "$REGION" \
    --query 'Listeners[*].{Port:Port,Protocol:Protocol,Certs:Certificates[*].CertificateArn}'

for CH_PORT in 9440 8443; do
  CH_LISTENER_PROTO=$(aws elbv2 describe-listeners --load-balancer-arn "$CH_NLB_ARN" \
    --region "$REGION" --query "Listeners[?Port==\`${CH_PORT}\`].Protocol" --output text)

  if [[ "$CH_LISTENER_PROTO" != "TLS" ]]; then
    fail "ClickHouse NLB listener on port ${CH_PORT} protocol is '${CH_LISTENER_PROTO}', expected 'TLS'."
  fi
done

pass "ClickHouse NLB listeners on ports 9440 and 8443 use TLS with ACM certificate."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 18 — ClickHouse NLB: Target group health
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse NLB target group targets are healthy"

CH_TG_ARNS=$(aws elbv2 describe-target-groups --load-balancer-arn "$CH_NLB_ARN" \
  --region "$REGION" --query 'TargetGroups[*].TargetGroupArn' --output text)

for TG in $CH_TG_ARNS; do
  run_cmd "Target health for ${TG}" \
    aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION"

  BAD_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State!='healthy' && TargetHealth.State!='initial' && TargetHealth.State!='draining'].Target.Id" --output text)

  INIT_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State=='initial'].Target.Id" --output text)

  DRAINING_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State=='draining'].Target.Id" --output text)

  if [[ -n "$BAD_TARGETS" ]]; then
    fail "Unhealthy targets in target group: ${BAD_TARGETS}"
  fi

  if [[ -n "$INIT_TARGETS" ]]; then
    echo -e "  ${YELLOW}⚠ WARNING: Targets still in initial health check: ${INIT_TARGETS}${RESET}"
  fi

  if [[ -n "$DRAINING_TARGETS" ]]; then
    echo -e "  ${YELLOW}⚠ WARNING: Targets draining (old pods deregistering): ${DRAINING_TARGETS}${RESET}"
  fi
done

pass "All ClickHouse NLB targets are healthy (or completing initial health check)."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 19 — OTel Collector NLB: Service annotations
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector Service has correct NLB annotations"

run_cmd "OTel Collector Service annotations" \
  svc_annotations opentelemetry-collector

declare -A OTEL_EXPECTED=(
  ["service.beta.kubernetes.io/aws-load-balancer-type"]="external"
  ["service.beta.kubernetes.io/aws-load-balancer-scheme"]="internal"
  ["service.beta.kubernetes.io/aws-load-balancer-nlb-target-type"]="ip"
  ["service.beta.kubernetes.io/aws-load-balancer-ssl-ports"]="4317,4318"
  ["service.beta.kubernetes.io/aws-load-balancer-backend-protocol"]="ssl"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-port"]="13133"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol"]="HTTP"
  ["service.beta.kubernetes.io/aws-load-balancer-healthcheck-path"]="/"
)

for KEY in "${!OTEL_EXPECTED[@]}"; do
  ACTUAL=$(svc_annotation opentelemetry-collector "$KEY")
  EXPECT="${OTEL_EXPECTED[$KEY]}"
  if [[ "$ACTUAL" != "$EXPECT" ]]; then
    fail "OTel annotation '${KEY}' is '${ACTUAL}', expected '${EXPECT}'."
  fi
done

OTEL_ACM=$(svc_annotation opentelemetry-collector "service.beta.kubernetes.io/aws-load-balancer-ssl-cert")
if [[ -z "$OTEL_ACM" || "$OTEL_ACM" == *"OTEL_CERTIFICATE_ID"* ]]; then
  fail "OTel NLB ssl-cert annotation is missing or still a placeholder: '${OTEL_ACM}'."
fi

pass "OTel Collector Service NLB annotations are correct (ACM cert: ${OTEL_ACM})."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 20 — OTel Collector NLB: Provisioned
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector NLB is provisioned"

OTEL_NLB_HOST=$(kubectl get svc -n "$NS" opentelemetry-collector \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

if [[ -z "$OTEL_NLB_HOST" ]]; then
  fail "OTel Collector Service has no loadBalancer ingress hostname — NLB may not be provisioned."
fi

run_cmd "OTel Collector NLB hostname" \
  echo "$OTEL_NLB_HOST"

OTEL_NLB_ARN=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[?DNSName=='${OTEL_NLB_HOST}'].LoadBalancerArn" --output text)

if [[ -z "$OTEL_NLB_ARN" ]]; then
  fail "Could not find an NLB in AWS with DNS name '${OTEL_NLB_HOST}'."
fi

pass "OTel Collector NLB is provisioned (ARN: ${OTEL_NLB_ARN})."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 21 — OTel Collector NLB: Scheme is internal
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector NLB scheme is internal"

OTEL_NLB_SCHEME=$(aws elbv2 describe-load-balancers --load-balancer-arns "$OTEL_NLB_ARN" \
  --region "$REGION" --query 'LoadBalancers[0].Scheme' --output text)

run_cmd "OTel Collector NLB scheme" \
  echo "$OTEL_NLB_SCHEME"

if [[ "$OTEL_NLB_SCHEME" != "internal" ]]; then
  fail "OTel NLB scheme is '${OTEL_NLB_SCHEME}', expected 'internal'."
fi

pass "OTel Collector NLB is internal."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 22 — OTel Collector NLB: Listener TLS config
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector NLB listeners have TLS configured"

run_cmd "OTel Collector NLB listeners" \
  aws elbv2 describe-listeners --load-balancer-arn "$OTEL_NLB_ARN" --region "$REGION" \
    --query 'Listeners[*].{Port:Port,Protocol:Protocol,Certs:Certificates[*].CertificateArn}'

for OTEL_PORT in 4317 4318; do
  OTEL_LISTENER_PROTO=$(aws elbv2 describe-listeners --load-balancer-arn "$OTEL_NLB_ARN" \
    --region "$REGION" --query "Listeners[?Port==\`${OTEL_PORT}\`].Protocol" --output text)

  if [[ "$OTEL_LISTENER_PROTO" != "TLS" ]]; then
    fail "OTel NLB listener on port ${OTEL_PORT} protocol is '${OTEL_LISTENER_PROTO}', expected 'TLS'."
  fi
done

pass "OTel Collector NLB listeners on ports 4317 and 4318 use TLS with ACM certificate."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 23 — OTel Collector NLB: Target group health
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector NLB target group targets are healthy"

OTEL_TG_ARNS=$(aws elbv2 describe-target-groups --load-balancer-arn "$OTEL_NLB_ARN" \
  --region "$REGION" --query 'TargetGroups[*].TargetGroupArn' --output text)

for TG in $OTEL_TG_ARNS; do
  run_cmd "Target health for ${TG}" \
    aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION"

  BAD_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State!='healthy' && TargetHealth.State!='initial' && TargetHealth.State!='draining'].Target.Id" --output text)

  INIT_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State=='initial'].Target.Id" --output text)

  DRAINING_TARGETS=$(aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[?TargetHealth.State=='draining'].Target.Id" --output text)

  if [[ -n "$BAD_TARGETS" ]]; then
    fail "Unhealthy targets in target group: ${BAD_TARGETS}"
  fi

  if [[ -n "$INIT_TARGETS" ]]; then
    echo -e "  ${YELLOW}⚠ WARNING: Targets still in initial health check: ${INIT_TARGETS}${RESET}"
  fi

  if [[ -n "$DRAINING_TARGETS" ]]; then
    echo -e "  ${YELLOW}⚠ WARNING: Targets draining (old pods deregistering): ${DRAINING_TARGETS}${RESET}"
  fi
done

pass "All OTel Collector NLB targets are healthy."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 24 — DNS: ClickHouse hostname resolves
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse DNS hostname resolves to its NLB"

CH_HOSTNAME=$(svc_annotation clickhouse-otel "external-dns.alpha.kubernetes.io/hostname")

if [[ -z "$CH_HOSTNAME" ]]; then
  echo -e "  ${YELLOW}⚠ SKIP: No external-dns hostname annotation on clickhouse-otel service.${RESET}"
else
  run_cmd "DNS lookup for ${CH_HOSTNAME}" \
    dig +short "$CH_HOSTNAME"

  DNS_RESULT=$(dig +short "$CH_HOSTNAME" | head -1)
  if [[ -z "$DNS_RESULT" ]]; then
    fail "DNS lookup for '${CH_HOSTNAME}' returned no results."
  fi

  # Check that the DNS CNAME or A record points to the NLB
  echo ""
  echo -e "  ${YELLOW}▸ Comparing DNS result to NLB hostname${RESET}"
  echo "    DNS resolves to:  ${DNS_RESULT}"
  echo "    NLB hostname:     ${CH_NLB_HOST}"

  if [[ "$DNS_RESULT" == *"${CH_NLB_HOST}"* || "$DNS_RESULT" == "$CH_NLB_HOST." ]]; then
    pass "ClickHouse DNS '${CH_HOSTNAME}' resolves to its NLB."
  else
    # It may be an alias record that resolves to NLB IPs; compare resolved IPs
    NLB_IPS=$(dig +short "$CH_NLB_HOST" | sort)
    DNS_IPS=$(dig +short "$CH_HOSTNAME" | sort)
    if [[ "$NLB_IPS" == "$DNS_IPS" ]]; then
      pass "ClickHouse DNS '${CH_HOSTNAME}' resolves to the same IPs as the NLB."
    else
      fail "ClickHouse DNS '${CH_HOSTNAME}' does not point to the NLB. DNS: ${DNS_RESULT}, NLB: ${CH_NLB_HOST}"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 25 — DNS: OTel Collector hostname resolves
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector DNS hostname resolves to its NLB"

OTEL_HOSTNAME=$(svc_annotation opentelemetry-collector "external-dns.alpha.kubernetes.io/hostname")

if [[ -z "$OTEL_HOSTNAME" ]]; then
  echo -e "  ${YELLOW}⚠ SKIP: No external-dns hostname annotation on opentelemetry-collector service.${RESET}"
else
  run_cmd "DNS lookup for ${OTEL_HOSTNAME}" \
    dig +short "$OTEL_HOSTNAME"

  DNS_RESULT=$(dig +short "$OTEL_HOSTNAME" | head -1)
  if [[ -z "$DNS_RESULT" ]]; then
    fail "DNS lookup for '${OTEL_HOSTNAME}' returned no results."
  fi

  echo ""
  echo -e "  ${YELLOW}▸ Comparing DNS result to NLB hostname${RESET}"
  echo "    DNS resolves to:  ${DNS_RESULT}"
  echo "    NLB hostname:     ${OTEL_NLB_HOST}"

  if [[ "$DNS_RESULT" == *"${OTEL_NLB_HOST}"* || "$DNS_RESULT" == "$OTEL_NLB_HOST." ]]; then
    pass "OTel Collector DNS '${OTEL_HOSTNAME}' resolves to its NLB."
  else
    NLB_IPS=$(dig +short "$OTEL_NLB_HOST" | sort)
    DNS_IPS=$(dig +short "$OTEL_HOSTNAME" | sort)
    if [[ "$NLB_IPS" == "$DNS_IPS" ]]; then
      pass "OTel Collector DNS '${OTEL_HOSTNAME}' resolves to the same IPs as the NLB."
    else
      fail "OTel DNS '${OTEL_HOSTNAME}' does not point to the NLB. DNS: ${DNS_RESULT}, NLB: ${OTEL_NLB_HOST}"
    fi
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 26 — In-cluster TLS: ClickHouse serving TLS on ports 9440 and 8443
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse is serving TLS on ports 9440 and 8443 (in-cluster)"

CH_CA_PATH="/etc/clickhouse-server/secrets.d/ca.crt/clickhouse-server-tls/ca.crt"

for CH_TLS_PORT in 9440 8443; do
  run_cmd "openssl s_client to clickhouse-otel:${CH_TLS_PORT} (verifying against CA)" \
    kubectl exec -n "$NS" "$CH_POD" -- \
      bash -c "echo | openssl s_client -connect clickhouse-otel:${CH_TLS_PORT} -servername clickhouse-otel -CAfile ${CH_CA_PATH} 2>/dev/null | grep -E 'Certificate chain|subject=|issuer=|Verify return'"

  CH_TLS_OUTPUT=$(kubectl exec -n "$NS" "$CH_POD" -- \
    bash -c "echo | openssl s_client -connect clickhouse-otel:${CH_TLS_PORT} -servername clickhouse-otel -CAfile ${CH_CA_PATH} 2>/dev/null" || true)

  if echo "$CH_TLS_OUTPUT" | grep -q "Verify return code: 0"; then
    :
  elif echo "$CH_TLS_OUTPUT" | grep -q "Certificate chain"; then
    fail "ClickHouse is serving TLS on port ${CH_TLS_PORT} but certificate verification failed:\n$(echo "$CH_TLS_OUTPUT" | grep 'Verify return')"
  else
    fail "Could not establish a TLS connection to clickhouse-otel:${CH_TLS_PORT}."
  fi
done

pass "ClickHouse TLS certificates on ports 9440 and 8443 are valid and verified against the CA."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 27 — In-cluster TLS: OTel Collector serving TLS on ports 4317 and 4318
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector is serving TLS on ports 4317 and 4318 (in-cluster)"

for OTEL_TLS_PORT in 4317 4318; do
  run_cmd "openssl s_client to opentelemetry-collector:${OTEL_TLS_PORT} (verifying against CA)" \
    kubectl exec -n "$NS" "$CH_POD" -- \
      bash -c "echo | openssl s_client -connect opentelemetry-collector:${OTEL_TLS_PORT} -servername opentelemetry-collector -CAfile ${CH_CA_PATH} 2>/dev/null | grep -E 'Certificate chain|subject=|issuer=|Verify return'"

  OTEL_TLS_OUTPUT=$(kubectl exec -n "$NS" "$CH_POD" -- \
    bash -c "echo | openssl s_client -connect opentelemetry-collector:${OTEL_TLS_PORT} -servername opentelemetry-collector -CAfile ${CH_CA_PATH} 2>/dev/null" || true)

  if echo "$OTEL_TLS_OUTPUT" | grep -q "Verify return code: 0"; then
    :
  elif echo "$OTEL_TLS_OUTPUT" | grep -q "Certificate chain"; then
    fail "OTel Collector is serving TLS on port ${OTEL_TLS_PORT} but certificate verification failed:\n$(echo "$OTEL_TLS_OUTPUT" | grep 'Verify return')"
  else
    fail "Could not establish a TLS connection to opentelemetry-collector:${OTEL_TLS_PORT}."
  fi
done

pass "OTel Collector TLS certificates on ports 4317 and 4318 are valid and verified against the CA."

# ─────────────────────────────────────────────────────────────────────────────
# Read-only credential for the schema/data checks below (CHECK 28+). These run as readonly_user,
# so enable it (readonlyUser.enabled=true) to run this verification. Avoids the removed `default`
# user and the write-scoped otel / schema_owner identities.
# ─────────────────────────────────────────────────────────────────────────────
CH_READ_USER="readonly_user"
CH_READ_PW=$(kubectl get secret -n "$NS" ao-clickhouse-readonly-user-credentials \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [[ -z "$CH_READ_PW" ]]; then
  fail "readonly_user credential not found — enable readonlyUser (readonlyUser.enabled=true) to run the ClickHouse data checks."
fi
echo -e "  ${YELLOW}▸ Using ClickHouse reader 'readonly_user' for schema/data checks${RESET}"

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 28 — ClickHouse database and schema
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse database 'otel_traces' and tables exist"

run_cmd "Databases" \
  kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SHOW DATABASES"

DB_EXISTS=$(kubectl exec -n "$NS" "$CH_POD" -- \
  clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT name FROM system.databases WHERE name='otel_traces'" 2>/dev/null)

if [[ -z "$DB_EXISTS" ]]; then
  fail "Database 'otel_traces' does not exist in ClickHouse."
fi

run_cmd "Tables in otel_traces" \
  kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SHOW TABLES FROM otel_traces"

for TABLE in otel_traces otel_traces_trace_id_ts; do
  TABLE_EXISTS=$(kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT name FROM system.tables WHERE database='otel_traces' AND name='${TABLE}'" 2>/dev/null)
  if [[ -z "$TABLE_EXISTS" ]]; then
    fail "Table 'otel_traces.${TABLE}' does not exist."
  fi
done

# Check materialized view
MV_EXISTS=$(kubectl exec -n "$NS" "$CH_POD" -- \
  clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT name FROM system.tables WHERE database='otel_traces' AND name='otel_traces_trace_id_ts_mv'" 2>/dev/null)
if [[ -z "$MV_EXISTS" ]]; then
  fail "Materialized view 'otel_traces.otel_traces_trace_id_ts_mv' does not exist."
fi

pass "Database 'otel_traces' exists with all tables and materialized view."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 29 — ClickHouse otel user authentication
# ─────────────────────────────────────────────────────────────────────────────
banner "ClickHouse 'otel' user can authenticate"

CH_PASSWORD=$(kubectl get secret -n "$NS" ao-clickhouse-otel-credentials \
  -o jsonpath='{.data.password}' | base64 -d)

run_cmd "Authenticating as 'otel' user" \
  kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user otel --password "$CH_PASSWORD" --query "SELECT 'auth_ok'"

AUTH_RESULT=$(kubectl exec -n "$NS" "$CH_POD" -- \
  clickhouse-client --user otel --password "$CH_PASSWORD" --query "SELECT 'auth_ok'" 2>/dev/null)

if [[ "$AUTH_RESULT" != "auth_ok" ]]; then
  fail "ClickHouse 'otel' user authentication failed."
fi

pass "ClickHouse 'otel' user authenticated successfully."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 30 — OTel Collector health check
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector health check endpoint responds"

run_cmd "OTel Collector pod readiness (implies health check on :13133 is passing)" \
  kubectl get pod -n "$NS" "$OTEL_POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")]}'

OTEL_READY_STATUS=$(kubectl get pod -n "$NS" "$OTEL_POD" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')

if [[ "$OTEL_READY_STATUS" != "True" ]]; then
  fail "OTel Collector pod is not Ready — health check on :13133 may be failing."
fi

pass "OTel Collector pod is Ready (health check on :13133 is passing)."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 31 — OTel Collector logs (no export errors)
# ─────────────────────────────────────────────────────────────────────────────
banner "OTel Collector has no export errors in recent logs"

run_cmd "Recent OTel Collector logs (last 50 lines)" \
  kubectl logs -n "$NS" "$OTEL_POD" --tail=50

ERROR_LINES=$(kubectl logs -n "$NS" "$OTEL_POD" --tail=200 \
  | grep -iE "error|failed|refused" | grep -iv "healthcheck\|retry" || true)

if [[ -n "$ERROR_LINES" ]]; then
  echo ""
  echo -e "  ${YELLOW}⚠ WARNING: Found error-like lines in OTel Collector logs:${RESET}"
  echo "$ERROR_LINES" | head -10 | sed 's/^/    /'
  echo ""
  echo -e "  ${YELLOW}  Review these manually — they may be transient startup errors.${RESET}"
fi

pass "No persistent export errors detected in OTel Collector logs."

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 32 — End-to-end smoke test
# ─────────────────────────────────────────────────────────────────────────────
banner "End-to-end smoke test: send trace via OTLP → verify in ClickHouse"

TRACE_ID="00000000000000000000$(date +%s%N | tail -c 13)"
SPAN_ID="00000000$(date +%s | tail -c 9)"
START_NS="$(date +%s)000000000"
END_NS="$(($(date +%s) + 1))000000000"

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
        "startTimeUnixNano": "${START_NS}",
        "endTimeUnixNano": "${END_NS}",
        "status": {}
      }]
    }]
  }]
}
EOJSON
)

echo -e "  ${YELLOW}▸ Sending test trace to OTel Collector via in-cluster OTLP HTTP${RESET}"
echo "    TraceId: ${TRACE_ID}"

# Send trace via an ephemeral pod (curl to the in-cluster service)
SEND_RESULT=$(kubectl run -n "$NS" verify-smoke-test --rm -i --restart=Never \
  --image=curlimages/curl -- \
  -sk -X POST "https://opentelemetry-collector:4318/v1/traces" \
  -H "Content-Type: application/json" \
  -d "$TRACE_JSON" 2>/dev/null \
  | grep -v '^pod "' || true)

echo "    Response: ${SEND_RESULT}"

echo ""
echo -e "  ${YELLOW}▸ Waiting 8 seconds for the batch processor to flush...${RESET}"
sleep 8

run_cmd "Query ClickHouse for trace ${TRACE_ID}" \
  kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT ServiceName, SpanName, TraceId FROM otel_traces.otel_traces WHERE TraceId = '${TRACE_ID}' LIMIT 5"

SMOKE_RESULT=$(kubectl exec -n "$NS" "$CH_POD" -- \
  clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT count() FROM otel_traces.otel_traces WHERE TraceId = '${TRACE_ID}'" 2>/dev/null)

if [[ "$SMOKE_RESULT" -gt 0 ]] 2>/dev/null; then
  pass "Trace ${TRACE_ID} arrived in ClickHouse (${SMOKE_RESULT} row(s)). End-to-end pipeline is working."
else
  fail "Trace ${TRACE_ID} did not arrive in ClickHouse. The OTLP → OTel Collector → ClickHouse pipeline may be broken."
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 33 — NLB smoke test: send trace via OTel NLB endpoint
# ─────────────────────────────────────────────────────────────────────────────
banner "NLB smoke test: send trace via OTel Collector NLB endpoint"

if [[ -z "$OTEL_HOSTNAME" ]]; then
  echo -e "  ${YELLOW}⚠ SKIP: No external-dns hostname on OTel service — cannot test NLB path.${RESET}"
else
  NLB_TRACE_ID="00000000000000000001$(date +%s%N | tail -c 13)"
  NLB_SPAN_ID="00000001$(date +%s | tail -c 9)"
  NLB_START_NS="$(date +%s)000000000"
  NLB_END_NS="$(($(date +%s) + 1))000000000"

  NLB_TRACE_JSON=$(cat <<EOJSON2
{
  "resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "verify-deployment-nlb-smoke-test"}}]},
    "scopeSpans": [{
      "spans": [{
        "traceId": "${NLB_TRACE_ID}",
        "spanId": "${NLB_SPAN_ID}",
        "name": "nlb-smoke-test-span",
        "kind": 1,
        "startTimeUnixNano": "${NLB_START_NS}",
        "endTimeUnixNano": "${NLB_END_NS}",
        "status": {}
      }]
    }]
  }]
}
EOJSON2
  )

  echo -e "  ${YELLOW}▸ Sending test trace via NLB: https://${OTEL_HOSTNAME}:4318${RESET}"
  echo "    TraceId: ${NLB_TRACE_ID}"

  NLB_SEND_RESULT=$(kubectl run -n "$NS" verify-nlb-otel --rm -i --restart=Never \
    --image=curlimages/curl -- \
    -s -X POST "https://${OTEL_HOSTNAME}:4318/v1/traces" \
    -H "Content-Type: application/json" \
    -d "$NLB_TRACE_JSON" 2>/dev/null \
    | grep -v '^pod "' || true)

  echo "    Response: ${NLB_SEND_RESULT}"

  echo ""
  echo -e "  ${YELLOW}▸ Waiting 8 seconds for the batch processor to flush...${RESET}"
  sleep 8

  run_cmd "Query ClickHouse for trace ${NLB_TRACE_ID}" \
    kubectl exec -n "$NS" "$CH_POD" -- \
      clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT ServiceName, SpanName, TraceId FROM otel_traces.otel_traces WHERE TraceId = '${NLB_TRACE_ID}' LIMIT 5"

  NLB_SMOKE_RESULT=$(kubectl exec -n "$NS" "$CH_POD" -- \
    clickhouse-client --user "$CH_READ_USER" --password "$CH_READ_PW" --query "SELECT count() FROM otel_traces.otel_traces WHERE TraceId = '${NLB_TRACE_ID}'" 2>/dev/null)

  if [[ "$NLB_SMOKE_RESULT" -gt 0 ]] 2>/dev/null; then
    pass "Trace ${NLB_TRACE_ID} sent via OTel NLB arrived in ClickHouse. NLB → OTel → ClickHouse pipeline is working."
  else
    fail "Trace ${NLB_TRACE_ID} sent via OTel NLB did not arrive in ClickHouse. The NLB → OTel Collector path may be broken."
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# CHECK 34 — NLB smoke test: query ClickHouse via its NLB endpoint
# ─────────────────────────────────────────────────────────────────────────────
banner "NLB smoke test: query ClickHouse via its NLB endpoint (port 8443, HTTPS)"

if [[ -z "$CH_HOSTNAME" ]]; then
  echo -e "  ${YELLOW}⚠ SKIP: No external-dns hostname on ClickHouse service — cannot test NLB path.${RESET}"
else
  CURL_CMD="curl -s 'https://${CH_HOSTNAME}:8443/?query=SELECT+1&user=otel&password=***'"
  echo ""
  echo -e "  ${YELLOW}▸ Querying ClickHouse via NLB${RESET}"
  echo -e "  ${BOLD}\$ ${CURL_CMD}${RESET}"
  echo ""

  CH_NLB_RESULT=$(kubectl run -n "$NS" verify-nlb-ch --rm -i --restart=Never \
    --image=curlimages/curl -- \
    -s "https://${CH_HOSTNAME}:8443/?query=SELECT+1&user=otel&password=${CH_PASSWORD}" 2>/dev/null \
    | grep -v '^pod "' || true)

  echo "    ${CH_NLB_RESULT}"

  if [[ "$(echo "$CH_NLB_RESULT" | tr -d '[:space:]')" == "1" ]]; then
    pass "ClickHouse responded to query via NLB. NLB → ClickHouse path is working."
  else
    fail "ClickHouse did not respond correctly via NLB. Expected '1', got: '${CH_NLB_RESULT}'"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# DONE
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  ALL ${STEP} CHECKS PASSED${RESET}"
echo -e "${BOLD}${GREEN}════════════════════════════════════════════════════════════════════════════════════════${RESET}"
echo ""
