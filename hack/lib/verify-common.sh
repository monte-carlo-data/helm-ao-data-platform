# shellcheck shell=bash
#
# verify-common.sh — shared helpers for the verify-deployment-{aws,azure,gcp}.sh
# post-deployment verification scripts in the parent directory.
#
# This file is meant to be *sourced*, not executed:
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/lib/verify-common.sh"
#
# It provides the presentation helpers (banner/run_cmd/pass/fail + colours), the
# ClickHouse exec helpers, and the cloud-independent least-privilege user-model check
# that is identical across all three clouds. Cloud-specific checks (gateway/DNS/LB,
# AWS IRSA/NLB, StorageClass, smoke test, etc.) stay in the per-cloud scripts.
#
# Sourcing scripts must have `set -euo pipefail` in effect. Helpers that read cluster
# state resolve $NS (namespace) and $CH_POD (ClickHouse pod) at call time, so those
# must be set before the helper is *called* (they need not exist when this is sourced).

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

STEP=0

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
  # Run and capture; stream output indented.
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

# Run a ClickHouse query as a given user via `kubectl exec` into the ClickHouse pod.
# Password ($2) is fed over the exec *stdin* stream, never on the exec command array — so it
# stays out of clickhouse-client's argv, the ClickHouse query_log, this script's output (incl.
# run_cmd's command echo), AND the kube-apiserver exec audit requestURI (which records the
# command array, not stdin). The user ($1) and SQL ($3) still travel on argv, but neither is
# sensitive. Requires $NS and $CH_POD.
ch_exec() {  # user pw sql
  # shellcheck disable=SC2016  # $1/$2/$p are expanded by the inner `sh -c`, not the outer shell — intentional.
  printf '%s\n' "$2" | kubectl exec -i -n "$NS" "$CH_POD" -- \
    sh -c 'IFS= read -r p; CLICKHOUSE_PASSWORD="$p" clickhouse-client --user "$1" --query "$2"' _ "$1" "$3"
}
ch_query() { ch_exec "$1" "$2" "$3" 2>/dev/null || true; }

# Fetch a per-user ClickHouse password from its Secret. ch_as runs a query and captures both
# stdout and stderr. Both tolerate non-zero (set -euo pipefail is on): callers inspect the
# *output*, not the exit status — a missing secret yields "" and a denied query yields the error
# text, so the explicit guards/assertions below fire and print instead of the script dying
# silently mid-check.
ch_pw() { kubectl get secret -n "$NS" "$1" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true; }
ch_as() { ch_exec "$1" "$2" "$3" 2>&1 || true; }

expect_grant() {  # label user pw needle
  if ch_as "$2" "$3" "SHOW GRANTS" | grep -qiF "$4"; then pass "$1"; else fail "$1 — expected grant missing: $4"; fi
}
forbid_grant() {  # label user pw needle
  # Positively confirm SHOW GRANTS actually ran before trusting "needle absent" = grant absent:
  # a transient exec/auth failure also yields output without the needle, which would false-PASS.
  local o; o=$(ch_as "$2" "$3" "SHOW GRANTS")
  if echo "$o" | grep -qiE "exception|access_denied|not enough priv" || ! echo "$o" | grep -qiF "GRANT"; then
    fail "$1 — could not read grants (SHOW GRANTS failed or returned no grant lines): $o"
  elif echo "$o" | grep -qiF "$4"; then
    fail "$1 — unexpected grant present: $4"
  else
    pass "$1"
  fi
}
expect_ok() {     # label user pw sql
  local o; o=$(ch_as "$2" "$3" "$4")
  if echo "$o" | grep -qiE "exception|access_denied|not enough priv"; then fail "$1 — $o"; else pass "$1"; fi
}
expect_denied() { # label user pw sql
  local o; o=$(ch_as "$2" "$3" "$4")
  if echo "$o" | grep -qiE "access_denied|not enough priv"; then pass "$1"; else fail "$1 — expected ACCESS_DENIED, got: $o"; fi
}
expect_rows() {   # label user pw sql — runs a scalar count() query and asserts the result is >= 1
  local o n; o=$(ch_as "$2" "$3" "$4")
  if echo "$o" | grep -qiE "exception|access_denied|not enough priv"; then fail "$1 — $o"; fi
  n=$(printf '%s' "$o" | tr -d '[:space:]')
  if [[ "$n" =~ ^[0-9]+$ && "$n" -ge 1 ]]; then pass "$1 (${n} row(s))"; else fail "$1 — expected a row (count >= 1), got: $o"; fi
}

# ─────────────────────────────────────────────────────────────────────────────
# ClickHouse least-privilege user model
#
# Each user authenticates and holds the expected grants, and the security-critical
# denials are enforced. Queries run as each user over the pod's local port via
# `kubectl exec`, so this works while the users' networks/ip include loopback (the
# default). Once per-caller network scoping restricts a user to specific external
# CIDRs, the loopback path stops working for that user and these checks must move to
# the Service endpoint.
#
# Requires $NS and $CH_POD, plus $CH_PASSWORD (otel user) and $CH_READ_PW (readonly_user)
# set by the caller; reads $VERIFY_OTEL_RESTRICTED.
# ─────────────────────────────────────────────────────────────────────────────
verify_clickhouse_user_model() {
  banner "ClickHouse least-privilege user model"

  local SO_PW WK_PW MC_PW AD_PW
  SO_PW=$(ch_pw ao-clickhouse-schema-owner-credentials)
  WK_PW=$(ch_pw ao-clickhouse-llm-worker-credentials)
  MC_PW=$(ch_pw ao-clickhouse-monte-carlo-credentials)
  AD_PW=$(ch_pw ao-clickhouse-admin-credentials)
  # otel password = $CH_PASSWORD; readonly_user = $CH_READ_PW (both set by the caller).
  local pair name pw
  for pair in "schema_owner:$SO_PW" "llm_worker:$WK_PW" "monte_carlo:$MC_PW"; do
    name="${pair%%:*}"; pw="${pair#*:}"
    [[ -z "$pw" ]] && fail "ClickHouse per-user secret for '$name' is missing — this cluster predates the least-privilege user model (chart not yet migrated)."
  done

  # schema_owner — owns the schema (DDL); deliberately has no access management rights.
  expect_grant   "schema_owner holds DDL on otel_traces"     schema_owner "$SO_PW" "CREATE TABLE"
  forbid_grant   "schema_owner has no access management"     schema_owner "$SO_PW" "ACCESS MANAGEMENT"
  # SHOW USERS is an access-management-gated op that creates no entity, so it stays correct on
  # re-runs. A CREATE USER probe could false-FAIL on a leftover user (ALREADY_EXISTS, not denied),
  # and schema_owner lacks the privilege to drop it for cleanup.
  expect_denied  "schema_owner cannot manage users"          schema_owner "$SO_PW" "SHOW USERS"

  # llm_worker — queue read/write only; must NOT read telemetry.
  expect_ok      "llm_worker reads the queue"                llm_worker "$WK_PW" "SELECT count() FROM otel_traces.llm_batches"
  expect_grant   "llm_worker can append results"             llm_worker "$WK_PW" "INSERT ON otel_traces.llm_results"
  # The schema Job seeds a row into llm_worker_info, so assert a row is actually present (not just
  # that the read is permitted) — a missing seed means the marker was never written.
  expect_rows    "llm_worker reads its seeded cloud/provider marker" llm_worker "$WK_PW" "SELECT count() FROM otel_traces.llm_worker_info"
  expect_grant   "llm_worker writes its cloud/provider marker" llm_worker "$WK_PW" "INSERT ON otel_traces.llm_worker_info"
  expect_denied  "llm_worker cannot read telemetry"          llm_worker "$WK_PW" "SELECT count() FROM otel_traces.spans_normalized"
  expect_denied  "llm_worker cannot write telemetry"         llm_worker "$WK_PW" "INSERT INTO otel_traces.otel_traces (Timestamp) VALUES (now())"

  # monte_carlo — reads everything + produces to the queue, but must NOT write telemetry.
  expect_ok      "monte_carlo reads telemetry"               monte_carlo "$MC_PW" "SELECT count() FROM otel_traces.spans_normalized"
  # system.numbers backs time-bucket / gap-fill queries (e.g. getTraceTimeSeries); reader bundle grant.
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
  # SHOW GRANTS renders as `GRANT ALL ON *.* TO admin WITH GRANT OPTION`, so the grant-all and
  # the grant-option are non-contiguous — check each substring separately (expect_grant is grep -F).
  if [[ -n "$AD_PW" ]]; then
    expect_grant "admin is a superuser"      admin "$AD_PW" "GRANT ALL ON *.*"
    expect_grant "admin can delegate grants" admin "$AD_PW" "WITH GRANT OPTION"
  else
    echo -e "  ${YELLOW}▸ admin user not enabled — skipping${RESET}"
  fi

  # Materialized views must run under schema_owner as their DEFINER.
  local mv
  for mv in otel_traces_trace_id_ts_mv spans_normalized_mv conversations_normalized_mv; do
    if ch_as schema_owner "$SO_PW" "SHOW CREATE TABLE otel_traces.$mv" | grep -qiE "DEFINER *= *\`?schema_owner\`?"; then
      pass "MV $mv runs as schema_owner (DEFINER)"
    else
      fail "MV $mv is not owned by schema_owner — the normalization cascade will break once otel is restricted."
    fi
  done

  pass "ClickHouse least-privilege user model verified."
}
