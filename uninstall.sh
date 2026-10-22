#!/usr/bin/env bash
# Rimuove ONVIF Viewer. La configurazione in /etc/onvif-viewer viene conservata
# a meno di passare --purge.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Esegui con sudo"; exit 1; }

systemctl disable --now onvif-player.service onvif-web.service 2>/dev/null || true
rm -f /etc/systemd/system/onvif-player.service /etc/systemd/system/onvif-web.service
rm -f /etc/polkit-1/rules.d/50-onvif-viewer.rules
systemctl daemon-reload
systemctl unmask getty@tty1.service
systemctl enable --now getty@tty1.service || true
rm -rf /opt/onvif-viewer
if [ "${1:-}" = "--purge" ]; then
    rm -rf /etc/onvif-viewer /var/lib/onvif-viewer
    userdel onvifview 2>/dev/null || true
fi
echo "ONVIF Viewer rimosso. Le opzioni aggiunte a cmdline.txt restano (c'è una copia .onvif-viewer.bak)."
