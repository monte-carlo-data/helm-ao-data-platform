#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: $(basename "$0") --domain DOMAIN --hosted-zone-id ZONE_ID [--region REGION]

Request an ACM certificate and create the DNS validation record in Route 53.

Options:
  --domain          Domain name (e.g. clickhouse.example.com)
  --hosted-zone-id  Route 53 hosted zone ID
  --region          AWS region (defaults to AWS_DEFAULT_REGION or us-west-2)
EOF
  exit 1
}

DOMAIN=""
ZONE_ID=""
REGION="${AWS_DEFAULT_REGION:-us-west-2}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain=*)         DOMAIN="${1#*=}"; shift ;;
    --domain)           DOMAIN="$2"; shift 2 ;;
    --hosted-zone-id=*) ZONE_ID="${1#*=}"; shift ;;
    --hosted-zone-id)   ZONE_ID="$2"; shift 2 ;;
    --region=*)         REGION="${1#*=}"; shift ;;
    --region)           REGION="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -z "$DOMAIN" || -z "$ZONE_ID" ]] && usage

echo "Requesting ACM certificate for ${DOMAIN} in ${REGION}..."

CERT_ARN=$(aws acm request-certificate \
  --domain-name "$DOMAIN" \
  --validation-method DNS \
  --region "$REGION" \
  --output text \
  --query 'CertificateArn')

echo "Certificate ARN: ${CERT_ARN}"

echo "Waiting for validation record to become available..."
for i in $(seq 1 30); do
  VALIDATION=$(aws acm describe-certificate \
    --certificate-arn "$CERT_ARN" \
    --region "$REGION" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord' \
    --output json 2>/dev/null)

  if [[ "$VALIDATION" != "null" && -n "$VALIDATION" ]]; then
    break
  fi
  sleep 2
done

if [[ "$VALIDATION" == "null" || -z "$VALIDATION" ]]; then
  echo "ERROR: Timed out waiting for validation record." >&2
  exit 1
fi

RECORD_NAME=$(echo "$VALIDATION" | jq -r '.Name')
RECORD_VALUE=$(echo "$VALIDATION" | jq -r '.Value')

echo "Creating DNS validation record:"
echo "  ${RECORD_NAME} -> ${RECORD_VALUE}"

aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch "$(cat <<EOF
{
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "${RECORD_NAME}",
      "Type": "CNAME",
      "TTL": 300,
      "ResourceRecords": [{"Value": "${RECORD_VALUE}"}]
    }
  }]
}
EOF
)"

echo "Waiting for certificate validation (this may take a few minutes)..."
aws acm wait certificate-validated \
  --certificate-arn "$CERT_ARN" \
  --region "$REGION"

echo ""
echo "Certificate issued!"
echo "ARN: ${CERT_ARN}"
