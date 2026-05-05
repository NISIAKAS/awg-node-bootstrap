#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

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

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine and Compose plugin"
  wait_for_apt_locks
  apt-get update -qq
  wait_for_apt_locks
  apt-get install -y ca-certificates curl gnupg -qq
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  wait_for_apt_locks
  apt-get update -qq
  wait_for_apt_locks
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -qq
fi

log "Starting Docker"
systemctl enable docker >/dev/null 2>&1 || true
systemctl restart docker >/dev/null 2>&1 || systemctl start docker >/dev/null 2>&1 || true

mkdir -p /opt/awg-node-runtime /etc/awg-agent /etc/amnezia/amneziawg /etc/iptables
chmod 700 /etc/awg-agent
touch /etc/iptables/rules.v4
