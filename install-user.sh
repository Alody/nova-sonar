#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
APPLICATIONS_DIR="${HOME}/.local/share/applications"
AUTOSTART_DIR="${HOME}/.config/autostart"
ICON_DIR="${HOME}/.local/share/icons/hicolor/512x512/apps"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
FILTER_CHAIN_DIR="${HOME}/.config/pipewire/filter-chain.conf.d"
HRTF_DIR="${HOME}/.local/share/nova-sonar/hrtf"

for command in python3 pipewire pactl pw-cli pw-dump parec systemctl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Missing required command: ${command}" >&2
        exit 1
    fi
done

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -e "${PROJECT_DIR}"

mkdir -p "${APPLICATIONS_DIR}" "${AUTOSTART_DIR}" "${ICON_DIR}" "${BIN_DIR}" \
    "${SYSTEMD_USER_DIR}" "${FILTER_CHAIN_DIR}"
install -m 644 "${PROJECT_DIR}/assets/nova-sonar.png" \
    "${ICON_DIR}/nova-sonar.png"
ln -sfn "${VENV_DIR}/bin/nova-sonar" "${BIN_DIR}/nova-sonar"
ln -sfn "${VENV_DIR}/bin/nova-sonar-hrtf" "${BIN_DIR}/nova-sonar-hrtf"
ln -sfn "${VENV_DIR}/bin/nova-sonar-diagnostics" \
    "${BIN_DIR}/nova-sonar-diagnostics"

desktop_temp="$(mktemp)"
spatial_temp="$(mktemp)"
trap 'rm -f "${desktop_temp}" "${spatial_temp}"' EXIT
sed "s|@EXEC@|${BIN_DIR}/nova-sonar|g" \
    "${PROJECT_DIR}/packaging/nova-sonar.desktop.in" > "${desktop_temp}"
install -m 644 "${desktop_temp}" "${APPLICATIONS_DIR}/nova-sonar.desktop"
install -m 644 "${desktop_temp}" "${AUTOSTART_DIR}/nova-sonar.desktop"
install -m 644 "${PROJECT_DIR}/nova-sonar-game.service" \
    "${SYSTEMD_USER_DIR}/nova-sonar-game.service"
sed "s|@HRTF_DIR@|${HRTF_DIR}|g" \
    "${PROJECT_DIR}/92-nova-sonar-game.conf" > "${spatial_temp}"
install -m 644 "${spatial_temp}" \
    "${FILTER_CHAIN_DIR}/92-nova-sonar-game.conf"
systemctl --user daemon-reload

echo
echo "Nova Sonar application installed."
echo "Launch: ${BIN_DIR}/nova-sonar"
echo "Diagnostics: ${BIN_DIR}/nova-sonar-diagnostics"
echo "Spatial graph and service installed but not enabled."
echo "Install the default ARI NH1230 HRTF, then enable the service; see README.md."
echo
echo "The advanced PipeWire graphs require LSP LV2, RNNoise LADSPA,"
echo "and a headset-specific sink/source. See README.md before enabling them."
