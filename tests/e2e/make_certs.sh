#!/usr/bin/env bash
# Creates a throw-away CA and a server certificate for the mock API (SAN mock-aruba).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs
cd certs
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=aruba-e2e-ca" \
  -keyout ca.key -out ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=mock-aruba" -keyout mock.key -out mock.csr 2>/dev/null
printf 'subjectAltName=DNS:mock-aruba,DNS:localhost\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > ext.cnf
openssl x509 -req -in mock.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 30 -extfile ext.cnf \
  -out mock.pem 2>/dev/null
chmod 644 ./*.pem ./*.key
rm -f mock.csr ext.cnf ca.srl
echo "certs created in $(pwd)"
