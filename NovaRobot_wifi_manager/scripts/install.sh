#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

chmod 755 "${ROOT}/scripts/NovaRobot-wifi"
mkdir -p "${ROOT}/data" "${HOME}/.local/bin" "${HOME}/.config/systemd/user"
ln -sfn "${ROOT}/scripts/NovaRobot-wifi" "${HOME}/.local/bin/NovaRobot-wifi"

if [[ "${EUID}" -eq 0 ]]; then
  # 系统服务与用户服务不能同时占 18080
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    systemctl --user --machine="${SUDO_USER}@" disable --now NovaRobot-wifi-manager.service 2>/dev/null || true
    sudo -u "${SUDO_USER}" systemctl --user disable --now NovaRobot-wifi-manager.service 2>/dev/null || true
  fi
  mkdir -p /var/lib/NovaRobot-wifi-manager
  ln -sfn "${ROOT}/scripts/NovaRobot-wifi" /usr/local/bin/NovaRobot-wifi
  install -m 0644 "${ROOT}/systemd/NovaRobot-wifi-manager.service" /etc/systemd/system/NovaRobot-wifi-manager.service
  systemctl daemon-reload
  systemctl reset-failed NovaRobot-wifi-manager.service 2>/dev/null || true
  systemctl enable --now NovaRobot-wifi-manager.service
  sleep 2
  systemctl --no-pager --full status NovaRobot-wifi-manager.service | head -20
else
  install -m 0644 "${ROOT}/systemd/NovaRobot-wifi-manager.user.service" "${HOME}/.config/systemd/user/NovaRobot-wifi-manager.service"
  systemctl --user daemon-reload
  systemctl --user enable --now NovaRobot-wifi-manager.service
  sleep 2
  systemctl --user --no-pager --full status NovaRobot-wifi-manager.service | head -20
  echo
  echo "当前以用户服务运行。开机自启还需要: sudo loginctl enable-linger ${USER}"
  echo "系统级 AP/hostapd 需要: sudo ${ROOT}/scripts/install.sh"
fi

echo
echo "HTTP: http://127.0.0.1:18080/status"
echo "UDP:  53535  (发送 NOVAROBOT_DISCOVER)"
echo "CLI:  ${ROOT}/scripts/NovaRobot-wifi status"
