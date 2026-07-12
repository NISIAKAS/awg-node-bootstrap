#!/usr/bin/env bash
set -euo pipefail

AWG_MODULE_VERSION="${AWG_MODULE_VERSION:-1.0.20260712.1}"
AWG_MODULE_COMMIT="${AWG_MODULE_COMMIT:-2a6e1a02ac024f54a23e18f894a279b7f870b8fb}"
AWG_MODULE_REPOSITORY="${AWG_MODULE_REPOSITORY:-https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git}"
LEGACY_MODULE_VERSION="1.0.20260712"
KVER="${KVER:-$(uname -r)}"
SOURCE_DIR="/usr/src/amneziawg-${AWG_MODULE_VERSION}"
BUILD_DIR=""

wait_for_apt_locks() {
  command -v fuser >/dev/null 2>&1 || return 0
  local attempt
  for attempt in $(seq 1 90); do
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
  done
  echo "Timed out waiting for apt/dpkg lock" >&2
  return 1
}

cleanup() {
  [[ -z "${BUILD_DIR}" ]] || rm -rf "${BUILD_DIR}"
}
trap cleanup EXIT

export DEBIAN_FRONTEND=noninteractive
wait_for_apt_locks
apt-get update -qq
wait_for_apt_locks
apt-get install -y dkms git make gcc "linux-headers-${KVER}" -qq \
  || apt-get install -y dkms git make gcc linux-headers-generic -qq

if [[ ! -f "${SOURCE_DIR}/dkms.conf" ]]; then
  BUILD_DIR="$(mktemp -d /tmp/amneziawg-dkms.XXXXXX)"
  git clone "${AWG_MODULE_REPOSITORY}" "${BUILD_DIR}/source"
  git -C "${BUILD_DIR}/source" checkout --detach "${AWG_MODULE_COMMIT}"
  mkdir -p "${SOURCE_DIR}"
  cp -a "${BUILD_DIR}/source/src" "${SOURCE_DIR}/src"

  cat > "${SOURCE_DIR}/dkms.conf" <<EOF
PACKAGE_NAME="amneziawg"
PACKAGE_VERSION="${AWG_MODULE_VERSION}"
BUILT_MODULE_NAME[0]="amneziawg"
BUILT_MODULE_LOCATION[0]="src"
DEST_MODULE_LOCATION[0]="/updates"
AUTOINSTALL="yes"
MAKE[0]="make -C \${kernel_source_dir} M=\${dkms_tree}/amneziawg/${AWG_MODULE_VERSION}/build/src modules"
CLEAN="make -C \${kernel_source_dir} M=\${dkms_tree}/amneziawg/${AWG_MODULE_VERSION}/build/src clean"
EOF
fi

if [[ ! -e "/var/lib/dkms/amneziawg/${AWG_MODULE_VERSION}/source" ]]; then
  dkms add -m amneziawg -v "${AWG_MODULE_VERSION}"
fi

for module_dir in /lib/modules/*; do
  [[ -d "${module_dir}/build" ]] || continue
  kernel="${module_dir##*/}"
  kernel_status="$(dkms status -m amneziawg -v "${AWG_MODULE_VERSION}" -k "${kernel}" 2>/dev/null || true)"
  if ! grep -q ': installed' <<<"${kernel_status}"; then
    dkms install -m amneziawg -v "${AWG_MODULE_VERSION}" -k "${kernel}"
  fi

  # A historical manual build in updates/ shadows updates/dkms/ after depmod.
  # Removing the on-disk duplicate is safe for the currently loaded module.
  rm -f "${module_dir}/updates/amneziawg.ko" \
    "${module_dir}/updates/amneziawg.ko.xz" \
    "${module_dir}/updates/amneziawg.ko.zst"
done

depmod -a
modprobe amneziawg
modinfo -k "${KVER}" amneziawg >/dev/null

# Remove the initial unpinned rollout only after the pinned replacement is ready.
if [[ -e "/var/lib/dkms/amneziawg/${LEGACY_MODULE_VERSION}/source" ]]; then
  dkms remove -m amneziawg -v "${LEGACY_MODULE_VERSION}" --all >/dev/null
fi

echo "AmneziaWG DKMS ready: version=${AWG_MODULE_VERSION} commit=${AWG_MODULE_COMMIT} kernel=${KVER}"
