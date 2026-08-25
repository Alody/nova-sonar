#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${HOME}/.local/share/nova-sonar"
BIN_DIR="${HOME}/.local/bin"
APPLICATIONS_DIR="${HOME}/.local/share/applications"
AUTOSTART_DIR="${HOME}/.config/autostart"
ICON_DIR="${HOME}/.local/share/icons/hicolor/512x512/apps"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
PIPEWIRE_DIR="${HOME}/.config/pipewire/pipewire.conf.d"
FILTER_CHAIN_DIR="${HOME}/.config/pipewire/filter-chain.conf.d"

systemctl --user disable --now nova-sonar-game.service >/dev/null 2>&1 || true

rm -f "${BIN_DIR}/nova-sonar"
rm -f "${BIN_DIR}/nova-sonar-hrtf"
rm -f "${BIN_DIR}/nova-sonar-diagnostics"
rm -f "${APPLICATIONS_DIR}/nova-sonar.desktop"
rm -f "${AUTOSTART_DIR}/nova-sonar.desktop"
rm -f "${ICON_DIR}/nova-sonar.png"
rm -f "${SYSTEMD_USER_DIR}/nova-sonar-game.service"
rm -f "${PIPEWIRE_DIR}/90-nova-sonar-mic-eq.conf"
rm -f "${PIPEWIRE_DIR}/91-nova-sonar-advanced-eq.conf"
rm -f "${FILTER_CHAIN_DIR}/92-nova-sonar-game.conf"

if [[ -d "${INSTALL_ROOT}/venv" ]]; then
    rm -rf "${INSTALL_ROOT}/venv"
fi
rmdir "${INSTALL_ROOT}" >/dev/null 2>&1 || true

systemctl --user daemon-reload
systemctl --user restart pipewire pipewire-pulse wireplumber

echo "Nova Sonar removed."
echo "Settings and downloaded HRTFs were preserved."
