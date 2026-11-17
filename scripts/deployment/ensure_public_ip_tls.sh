#!/bin/bash
# Public IP certificates: https://letsencrypt.org/2026/03/11/shorter-certs-certbot/
set -euo pipefail
umask 077

PUBLIC_IP=$(cat /etc/aidp-lab-public-ip)
python3 -c 'import ipaddress,sys; assert ipaddress.ip_address(sys.argv[1]).is_global' "$PUBLIC_IP"
# Official certbot/certbot:v5.4.0, verified linux/amd64 manifest. Webroot supports IP SANs from 5.4.
IMAGE=certbot/certbot@sha256:1dc5b4a99cce916f154c706569baf062600d7dea13e0711e7d7e1461d6230e39
case "$1" in
  issue) args=(certonly --webroot --webroot-path /var/lib/letsencrypt --preferred-profile shortlived
               --ip-address "$PUBLIC_IP" --cert-name aidp-lab-ip --agree-tos --register-unsafely-without-email --non-interactive) ;;
  renew) args=(renew --cert-name aidp-lab-ip --non-interactive) ;;
  *) echo 'Expected issue or renew' >&2; exit 2 ;;
esac
install -d -m 0700 /etc/letsencrypt /var/log/letsencrypt
install -d -m 0755 /var/lib/letsencrypt
docker run --rm --network host --security-opt no-new-privileges:true --cap-drop ALL \
  -v /etc/letsencrypt:/etc/letsencrypt:Z \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt:z \
  -v /var/log/letsencrypt:/var/log/letsencrypt:Z \
  "$IMAGE" "${args[@]}"

CERT=/etc/letsencrypt/live/aidp-lab-ip
TLS=/opt/aidp-lab/tls
# Keep the previous files if issuance/renewal or certificate validation fails.
openssl verify -CAfile /etc/pki/tls/certs/ca-bundle.crt -untrusted "$CERT/chain.pem" "$CERT/cert.pem"
openssl x509 -in "$CERT/cert.pem" -noout -checkip "$PUBLIC_IP"
openssl x509 -in "$CERT/cert.pem" -noout -checkend 3600
test "$(openssl x509 -in "$CERT/cert.pem" -pubkey -noout | openssl sha256)" = \
     "$(openssl pkey -in "$CERT/privkey.pem" -pubout | openssl sha256)"
install -m 0644 "$CERT/fullchain.pem" "$TLS/tls.crt.next"
install -m 0600 "$CERT/privkey.pem" "$TLS/tls.key.next"
chcon --reference="$TLS" "$TLS/tls.crt.next" "$TLS/tls.key.next"
mv -f "$TLS/tls.crt.next" "$TLS/tls.crt"
mv -f "$TLS/tls.key.next" "$TLS/tls.key"
docker exec aidp-lab nginx -t -c /run/nginx.conf
docker exec aidp-lab nginx -s reload -c /run/nginx.conf
