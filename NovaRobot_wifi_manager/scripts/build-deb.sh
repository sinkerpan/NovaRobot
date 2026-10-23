#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="novarobot-wifi-manager"
VER="$(python3 -c "import pathlib; t=pathlib.Path('${ROOT}/NovaRobot_wifi_manager/__init__.py').read_text(); print([l.split('=')[1].strip().strip('\"') for l in t.splitlines() if l.startswith('__version__')][0])")"
ARCH="all"
OUT_DIR="${ROOT}/dist"
STAGE="${OUT_DIR}/${PKG}_${VER}_${ARCH}"
DEB="${OUT_DIR}/${PKG}_${VER}_${ARCH}.deb"

rm -rf "${STAGE}"
mkdir -p \
  "${STAGE}/DEBIAN" \
  "${STAGE}/usr/lib/${PKG}/NovaRobot_wifi_manager" \
  "${STAGE}/usr/lib/${PKG}/config" \
  "${STAGE}/etc/novarobot-wifi-manager" \
  "${STAGE}/etc/systemd/system/bluetooth.service.d" \
  "${STAGE}/lib/systemd/system" \
  "${STAGE}/usr/bin" \
  "${STAGE}/usr/share/doc/${PKG}" \
  "${STAGE}/var/lib/novarobot-wifi-manager"

install -m 0755 "${ROOT}/packaging/DEBIAN/postinst" "${STAGE}/DEBIAN/postinst"
install -m 0755 "${ROOT}/packaging/DEBIAN/prerm" "${STAGE}/DEBIAN/prerm"
install -m 0755 "${ROOT}/packaging/DEBIAN/postrm" "${STAGE}/DEBIAN/postrm"
install -m 0644 "${ROOT}/packaging/DEBIAN/conffiles" "${STAGE}/DEBIAN/conffiles"

sed "s/^Version:.*/Version: ${VER}/" "${ROOT}/packaging/DEBIAN/control" > "${STAGE}/DEBIAN/control"

install -m 0644 "${ROOT}"/NovaRobot_wifi_manager/*.py \
  "${STAGE}/usr/lib/${PKG}/NovaRobot_wifi_manager/"
install -m 0644 "${ROOT}/config/default.json" \
  "${STAGE}/usr/lib/${PKG}/config/default.json"
install -m 0644 "${ROOT}/packaging/default.json" \
  "${STAGE}/etc/novarobot-wifi-manager/default.json"
install -m 0644 "${ROOT}/packaging/novarobot-wifi-manager.service" \
  "${STAGE}/lib/systemd/system/novarobot-wifi-manager.service"
install -m 0644 "${ROOT}/systemd/zz-novarobot-bluetooth.conf" \
  "${STAGE}/etc/systemd/system/bluetooth.service.d/zz-novarobot-bluetooth.conf"
install -m 0755 "${ROOT}/scripts/NovaRobot-wifi" \
  "${STAGE}/usr/bin/NovaRobot-wifi"
install -m 0644 "${ROOT}/服务流程.md" \
  "${STAGE}/usr/share/doc/${PKG}/README.md"

# Installed tree uses /etc config; keep a copy of shipped defaults for reference.
gzip -9 -n -c "${ROOT}/packaging/default.json" \
  > "${STAGE}/usr/share/doc/${PKG}/default.json.gz" || true

SIZE_KB="$(du -sk "${STAGE}" | awk '{print $1}')"
printf 'Installed-Size: %s\n' "${SIZE_KB}" >> "${STAGE}/DEBIAN/control"

dpkg-deb --root-owner-group --build "${STAGE}" "${DEB}"
echo "built ${DEB}"
dpkg-deb -I "${DEB}"
ls -lh "${DEB}"
