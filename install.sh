#!/usr/bin/env bash
# Installa ONVIF Viewer su Raspberry Pi OS Lite (Bookworm o Trixie, 64 bit).
# Uso:  sudo ./install.sh
# Con IMAGE_BUILD=1 gira dentro un chroot durante la creazione dell'immagine SD:
# configura e abilita i servizi senza avviarli.
set -euo pipefail
IMAGE_BUILD="${IMAGE_BUILD:-0}"

APP_DIR=/opt/onvif-viewer
CFG_DIR=/etc/onvif-viewer
STATE_DIR=/var/lib/onvif-viewer
SVC_USER=onvifview
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Esegui con sudo: sudo ./install.sh"; exit 1; }
command -v apt-get >/dev/null || { echo "Serve Raspberry Pi OS (Debian)"; exit 1; }

echo "==> Installazione pacchetti"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    mpv python3 python3-flask python3-requests python3-waitress \
    network-manager ca-certificates
# Il nome del pacchetto polkit cambia tra le versioni di Debian
apt-get install -y --no-install-recommends polkitd 2>/dev/null \
    || apt-get install -y --no-install-recommends policykit-1

if [ "$IMAGE_BUILD" = 1 ]; then
    systemctl enable NetworkManager
elif ! systemctl is-active --quiet NetworkManager; then
    echo "!! NetworkManager non è attivo. Su Raspberry Pi OS Bookworm/Trixie lo è di default."
    echo "   Lo abilito ora: se usavi dhcpcd, ricontrolla la rete dopo il riavvio."
    systemctl enable --now NetworkManager
fi

echo "==> Utente di servizio $SVC_USER"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
for g in video render audio input; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done

echo "==> Copia applicazione in $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/app"
cp -r "$SRC/app" "$APP_DIR/app"
find "$APP_DIR/app" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

mkdir -p "$CFG_DIR" "$STATE_DIR"
chown "$SVC_USER:$SVC_USER" "$CFG_DIR" "$STATE_DIR"
chmod 700 "$CFG_DIR"

echo "==> Servizi systemd e permessi polkit"
install -m 644 "$SRC/systemd/onvif-player.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/onvif-web.service" /etc/systemd/system/
mkdir -p /etc/polkit-1/rules.d
install -m 644 "$SRC/polkit/50-onvif-viewer.rules" /etc/polkit-1/rules.d/
[ "$IMAGE_BUILD" = 1 ] || systemctl try-restart polkit.service 2>/dev/null || true

echo "==> Console HDMI pulita (niente login né cursore sul monitor)"
systemctl disable getty@tty1.service 2>/dev/null || true
[ "$IMAGE_BUILD" = 1 ] || systemctl stop getty@tty1.service 2>/dev/null || true
systemctl mask getty@tty1.service
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
if [ -f "$CMDLINE" ]; then
    cp -n "$CMDLINE" "$CMDLINE.onvif-viewer.bak" || true
    for opt in consoleblank=0 logo.nologo vt.global_cursor_default=0; do
        grep -qw "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
    done
fi
CONFIGTXT=/boot/firmware/config.txt
[ -f "$CONFIGTXT" ] || CONFIGTXT=/boot/config.txt
if [ -f "$CONFIGTXT" ] && ! grep -q '^dtoverlay=vc4-kms-v3d' "$CONFIGTXT"; then
    echo "!! $CONFIGTXT non contiene dtoverlay=vc4-kms-v3d: lo aggiungo (serve per l'uscita video KMS)."
    printf '\n[all]\ndtoverlay=vc4-kms-v3d\n' >> "$CONFIGTXT"
fi

systemctl enable onvif-web.service onvif-player.service
if [ "$IMAGE_BUILD" = 1 ]; then
    echo "ONVIF Viewer installato nell'immagine."
    exit 0
fi
systemctl daemon-reload
systemctl restart onvif-web.service onvif-player.service

IPS="$(hostname -I | tr ' ' '\n' | grep -v ':' | grep . || true)"
echo
echo "Installazione completata."
echo "Interfaccia web:"
for ip in $IPS; do echo "   http://$ip"; done
echo "Accesso iniziale: utente admin, password admin (cambiala subito)."
echo "Consigliato un riavvio per applicare le opzioni di avvio: sudo reboot"
