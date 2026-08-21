#!/usr/bin/env bash
set -euo pipefail

AWG_SERVER_ROLE="${AWG_SERVER_ROLE:-relay}"
AWG_SERVER_ID="${AWG_SERVER_ID:-0}"
AWG_NODE_PORT="${AWG_NODE_PORT:-2222}"
AWG_AGENT_IMAGE="${AWG_AGENT_IMAGE:-ghcr.io/dayzu111/awg-node-agent:latest}"
NODE_EXPORTER_IMAGE="${NODE_EXPORTER_IMAGE:-prom/node-exporter:v1.8.2}"
AWG_BOOTSTRAP_RAW_BASE="${AWG_BOOTSTRAP_RAW_BASE:-https://raw.githubusercontent.com/NISIAKAS/awg-node-bootstrap/main}"
AWG_MASTER_ENROLL_URL="${AWG_MASTER_ENROLL_URL:-}"
AWG_MASTER_WEBHOOK_URL="${AWG_MASTER_WEBHOOK_URL:-}"
AWG_TOOLS_URL="${AWG_TOOLS_URL:-https://github.com/amnezia-vpn/amneziawg-tools/releases/download/v1.0.20260223/ubuntu-22.04-amneziawg-tools.zip}"
AWG_TOOLS_SHA256="${AWG_TOOLS_SHA256:-994289b71dfc8b3392d60f4461a65a869f392653aa5027d217f9519f57340bf0}"
AWG_TOOLS_COMMIT="${AWG_TOOLS_COMMIT:-5d6179a6d0842e98dfb349c28cf1bd8e4b9d1079}"
AWG_RUNTIME_ROOT="/opt/awg-node-runtime"
AWG_RUNTIME_COMPOSE_PATH="${AWG_RUNTIME_ROOT}/docker-compose.node.yml"
AWG_AGENT_BUILD_DIR="${AWG_RUNTIME_ROOT}/agent-build"
AWG_CONFIG_DIR="/etc/awg-agent"

log() {
  printf '[awg-node] %s\n' "$*"
}

wait_for_apt_locks() {
  if ! command -v fuser >/dev/null 2>&1; then
    return 0
  fi

  local attempt
  for attempt in $(seq 1 90); do
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; then
      return 0
    fi
    log "Waiting for apt/dpkg lock (${attempt}/90). Another package task is running."
    fuser -v /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>&1 || true
    sleep 10
  done

  log "Timed out waiting for apt/dpkg lock."
  return 1
}

if [[ -z "${AWG_MASTER_ENROLL_URL}" ]]; then
  echo "AWG_MASTER_ENROLL_URL is required" >&2
  exit 1
fi

if [[ -z "${AWG_MASTER_WEBHOOK_URL}" ]]; then
  echo "AWG_MASTER_WEBHOOK_URL is required" >&2
  exit 1
fi

if [[ "${AWG_SERVER_ID}" == "0" ]]; then
  echo "AWG_SERVER_ID is required" >&2
  exit 1
fi

case "${AWG_SERVER_ROLE}" in
  exit)
    AWG_INTERFACE="awg0"
    ;;
  relay)
    AWG_INTERFACE=""
    ;;
  *)
    echo "AWG_SERVER_ROLE must be exit or relay" >&2
    exit 1
    ;;
esac

download_raw_file() {
  local relative_path="$1"
  local destination="$2"
  mkdir -p "$(dirname "${destination}")"
  curl -fsSL "${AWG_BOOTSTRAP_RAW_BASE}/${relative_path}" -o "${destination}"
}

render_compose() {
  cat <<EOF
services:
  awg-agent:
    image: ${AWG_AGENT_IMAGE}
    container_name: awg-node-agent
    restart: unless-stopped
    network_mode: host
    privileged: true
    env_file:
      - /etc/awg-agent/config
    environment:
      AWG_AGENT_CONFIG_PATH: /etc/awg-agent/config
      AWG_BIN: /host-usr-local-bin/awg:/host-usr-bin/awg:awg
      AWG_QUICK_BIN: /host-usr-local-bin/awg-quick:/host-usr-bin/awg-quick:awg-quick
    volumes:
      - /etc/awg-agent:/etc/awg-agent
      - /etc/amnezia/amneziawg:/etc/amnezia/amneziawg
      - /etc/iptables:/etc/iptables
      - /etc/sysctl.d:/etc/sysctl.d
      - /usr/local/bin:/host-usr-local-bin:ro
      - /usr/bin:/host-usr-bin:ro

  node-exporter:
    image: ${NODE_EXPORTER_IMAGE}
    container_name: awg-node-exporter
    restart: unless-stopped
    network_mode: host
    pid: host
    command:
      - --web.listen-address=0.0.0.0:9100
      - --path.procfs=/host/proc
      - --path.sysfs=/host/sys
      - --path.rootfs=/rootfs
      - --collector.filesystem.mount-points-exclude=^/(dev|proc|sys|run|var/lib/docker/.+)($|/)
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro,rslave
EOF
}

render_agent_config() {
  cat <<EOF
AWG_AGENT_PORT=${AWG_NODE_PORT}
AWG_AGENT_TOKEN=
AWG_ENROLLMENT_SECRET=${AWG_ENROLLMENT_SECRET}
MASTER_ENROLL_URL=${AWG_MASTER_ENROLL_URL}
MASTER_WEBHOOK_URL=${AWG_MASTER_WEBHOOK_URL}
AWG_INTERFACE=${AWG_INTERFACE}
AWG_AGENT_LOG_LEVEL=INFO
AWG_AGENT_VERSION=docker-v1
SERVER_ID=${AWG_SERVER_ID}
EOF
}

install_awg_tools_if_needed() {
  if [[ -x /usr/local/bin/awg && -x /usr/local/bin/awg-quick ]]; then
    return 0
  fi

  local archive="/tmp/awg-tools.zip"
  local extract_dir="/tmp/awg-tools"
  local binary_dir="${extract_dir}/ubuntu-22.04-amneziawg-tools"

  rm -rf "${archive}" "${extract_dir}"
  log "Installing pinned AmneziaWG tools"
  if curl -fsSL "${AWG_TOOLS_URL}" -o "${archive}" \
    && printf '%s  %s\n' "${AWG_TOOLS_SHA256}" "${archive}" | sha256sum -c - \
    && unzip -q -o "${archive}" -d "${extract_dir}" \
    && [[ -f "${binary_dir}/awg" && -f "${binary_dir}/awg-quick" ]]; then
    install -m 0755 "${binary_dir}/awg" /usr/local/bin/awg
    install -m 0755 "${binary_dir}/awg-quick" /usr/local/bin/awg-quick
    test -x /usr/local/bin/awg
    test -x /usr/local/bin/awg-quick
    return 0
  fi

  log "Pinned tools archive unavailable; building tools from pinned source"
  rm -rf /tmp/awg-tools-src
  git clone https://github.com/amnezia-vpn/amneziawg-tools.git /tmp/awg-tools-src
  git -C /tmp/awg-tools-src checkout --detach "${AWG_TOOLS_COMMIT}"
  make -C /tmp/awg-tools-src/src -j"$(nproc)"
  install -m 0755 /tmp/awg-tools-src/src/wg /usr/local/bin/awg
  install -m 0755 /tmp/awg-tools-src/src/wg-quick/linux.bash /usr/local/bin/awg-quick
  test -x /usr/local/bin/awg
  test -x /usr/local/bin/awg-quick
}

install_awg_if_needed() {
  if [[ "${AWG_SERVER_ROLE}" != "exit" ]]; then
    return 0
  fi

  mkdir -p /etc/amnezia/amneziawg
  sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
  grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

  log "Installing persistent AmneziaWG DKMS module"
  download_raw_file "ops/deploy/install_awg_dkms.sh" "/tmp/install-awg-dkms.sh"
  bash /tmp/install-awg-dkms.sh
  modinfo -k "$(uname -r)" amneziawg >/dev/null
  modprobe amneziawg
  install_awg_tools_if_needed

  if ! systemctl list-unit-files | grep -q 'awg-quick@.service'; then
    cat > /etc/systemd/system/awg-quick@.service <<'EOF'
[Unit]
Description=AmneziaWG via awg-quick(8) for %I
After=network-online.target nss-lookup.target
Wants=network-online.target nss-lookup.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/awg-quick up %i
ExecStop=/usr/local/bin/awg-quick down %i
Environment=WG_ENDPOINT_RESOLUTION_RETRIES=infinity

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
}

download_node_agent_build_context() {
  mkdir -p "${AWG_AGENT_BUILD_DIR}/awg_agent"
  download_raw_file "agent/Dockerfile" "${AWG_AGENT_BUILD_DIR}/Dockerfile"
  for file in __init__.py __main__.py api.py awg.py config.py enroll.py metrics.py server.py sync_module.py watcher.py; do
    download_raw_file "agent/awg_agent/${file}" "${AWG_AGENT_BUILD_DIR}/awg_agent/${file}"
  done
}

pull_awg_agent_or_build() {
  log "Pulling awg-agent image: ${AWG_AGENT_IMAGE}"
  if docker compose -f "${AWG_RUNTIME_COMPOSE_PATH}" pull awg-agent; then
    log "Using pulled awg-agent image"
    return 0
  fi

  log "Could not pull awg-agent image; building from GitHub sources"
  download_node_agent_build_context
  docker build -t "${AWG_AGENT_IMAGE}" "${AWG_AGENT_BUILD_DIR}"
}

export DEBIAN_FRONTEND=noninteractive
log "Installing base packages"
wait_for_apt_locks
apt-get update -qq
wait_for_apt_locks
apt-get install -y ca-certificates curl software-properties-common unzip git make gcc iproute2 -qq

mkdir -p "${AWG_CONFIG_DIR}" "${AWG_RUNTIME_ROOT}" "${AWG_AGENT_BUILD_DIR}"

if [[ -z "${AWG_ENROLLMENT_SECRET:-}" ]]; then
  read -rp "Secret key: " AWG_ENROLLMENT_SECRET
fi

install_awg_if_needed

log "Installing Docker runtime"
download_raw_file "ops/deploy/bootstrap_node_runtime.sh" "/tmp/bootstrap-awg-node-runtime.sh"
bash /tmp/bootstrap-awg-node-runtime.sh

render_compose > "${AWG_RUNTIME_COMPOSE_PATH}"
render_agent_config > "${AWG_CONFIG_DIR}/config"
chmod 600 "${AWG_CONFIG_DIR}/config"

systemctl stop awg-agent node_exporter 2>/dev/null || true
systemctl disable awg-agent node_exporter 2>/dev/null || true

cd "${AWG_RUNTIME_ROOT}"
log "Pulling node-exporter image: ${NODE_EXPORTER_IMAGE}"
docker compose -f "${AWG_RUNTIME_COMPOSE_PATH}" pull node-exporter || true
pull_awg_agent_or_build
docker compose -f "${AWG_RUNTIME_COMPOSE_PATH}" up -d awg-agent node-exporter

echo "SSH configuration is intentionally not managed by this script."
echo "Install complete. Host firewall is intentionally not managed by this script."
echo "Agent is starting and will enroll with master."
