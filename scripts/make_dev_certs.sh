#!/usr/bin/env bash
set -euo pipefail

config_home="${XDG_CONFIG_HOME:-${HOME}/.config}"
target_dir="${XR_MEDIA_TLS_DIR:-${config_home}/xr_media/tls}"
install -d -m 700 "${target_dir}"
san="DNS:localhost,DNS:*.local,IP:127.0.0.1"
while read -r ip; do
  [[ -n "${ip:-}" ]] || continue
  san="${san},IP:${ip}"
done < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' || true)
if [[ -n "${EXTRA_IP:-}" ]]; then
  san="${san},IP:${EXTRA_IP}"
fi

openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 825 \
  -keyout "${target_dir}/key.pem" -out "${target_dir}/cert.pem" \
  -subj "/CN=webrtc-media.local" \
  -addext "subjectAltName=${san}"
chmod 600 "${target_dir}/key.pem"
chmod 644 "${target_dir}/cert.pem"
echo "Created ${target_dir}/cert.pem and key.pem"
echo "SAN=${san}"
