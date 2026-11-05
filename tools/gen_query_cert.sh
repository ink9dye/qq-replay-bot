#!/usr/bin/env bash
# Generate a new self-signed certificate locally. Does not install or reload it.
# Usage: bash tools/gen_query_cert.sh PUBLIC_IP [EMPTY_OUTPUT_DIRECTORY]
set -euo pipefail
umask 077
query_ip="${1:?Provide the server public IP}"
query_cert_dir="${2:-.}"
mkdir -p -- "$query_cert_dir"
if [[ -e "$query_cert_dir/ygobot-query.key" || -e "$query_cert_dir/ygobot-query.crt" ]]; then
    echo 'Certificate already exists; refusing to replace it.' >&2
    exit 1
fi
openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout "$query_cert_dir/ygobot-query.key" \
    -out "$query_cert_dir/ygobot-query.crt" \
    -subj "/CN=${query_ip}" -addext "subjectAltName=IP:${query_ip}"
echo 'Generated certificate. Clients must explicitly trust this certificate.'
