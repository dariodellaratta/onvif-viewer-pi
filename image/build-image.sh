#!/usr/bin/env bash
# Crea un'immagine microSD pronta all'uso: Raspberry Pi OS Lite 64 bit (senza desktop)
# con ONVIF Viewer già installato.
#
# Va eseguito come root su Linux (x86_64 o arm64) con accesso a Internet:
#   sudo apt-get install -y qemu-user-static binfmt-support parted e2fsprogs xz-utils curl
#   sudo ./image/build-image.sh
#
# Variabili opzionali:
#   BASE_IMAGE   immagine di partenza (.img o .img.xz) già scaricata; altrimenti scarica
#                l'ultima Raspberry Pi OS Lite arm64 ufficiale
#   OUT          nome del file prodotto (default: onvif-viewer-pi.img)
#   PI_USER      utente per SSH/console (default: pi)
#   PI_PASSWORD  sua password (default: raspberry, va cambiata al primo accesso)
#   ENABLE_SSH   1 = SSH attivo (default), 0 = disattivo
#   HOSTNAME_PI  nome host (default: onvif-viewer)
#   EXTRA_MB     spazio aggiunto alla partizione per i pacchetti (default: 1536)
#   NO_XZ        1 = non comprimere il risultato
set -euo pipefail

BASE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$SRC/image/work}"
OUT="${OUT:-onvif-viewer-pi.img}"
PI_USER="${PI_USER:-pi}"
PI_PASSWORD="${PI_PASSWORD:-raspberry}"
ENABLE_SSH="${ENABLE_SSH:-1}"
HOSTNAME_PI="${HOSTNAME_PI:-onvif-viewer}"
EXTRA_MB="${EXTRA_MB:-1536}"

[ "$(id -u)" -eq 0 ] || { echo "Esegui come root (sudo)"; exit 1; }
for t in parted losetup e2fsck resize2fs xz curl chroot; do
    command -v "$t" >/dev/null || { echo "Manca il comando $t"; exit 1; }
done
if [ "$(uname -m)" != "aarch64" ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    echo "Serve l'emulazione arm64: sudo apt-get install qemu-user-static binfmt-support"
    exit 1
fi

mkdir -p "$WORK"
ROOT="$WORK/rootfs"
LOOP_BOOT=""
LOOP_ROOT=""

# Collega una partizione dell'immagine a un loop device usando l'offset
# (funziona anche nei container senza udev, dove /dev/loopXpN non compare).
part_loop() {
    local img="$1" n="$2" line start size
    line="$(parted -s -m "$img" unit B print | grep "^$n:")"
    start="$(echo "$line" | cut -d: -f2 | tr -d B)"
    size="$(echo "$line" | cut -d: -f4 | tr -d B)"
    losetup -f --show -o "$start" --sizelimit "$size" "$img"
}

cleanup() {
    set +e
    for m in boot/firmware boot dev/pts dev proc sys run; do
        mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"
    done
    mountpoint -q "$ROOT" && umount -l "$ROOT"
    [ -n "$LOOP_BOOT" ] && losetup -d "$LOOP_BOOT"
    [ -n "$LOOP_ROOT" ] && losetup -d "$LOOP_ROOT"
    LOOP_BOOT=""; LOOP_ROOT=""
}
trap cleanup EXIT

# ---------- 1. immagine di partenza ----------
if [ -z "${BASE_IMAGE:-}" ]; then
    BASE_IMAGE="$WORK/raspios-lite-arm64.img.xz"
    if [ ! -f "$BASE_IMAGE" ]; then
        echo "==> Scarico Raspberry Pi OS Lite arm64"
        curl -fL --retry 3 -o "$BASE_IMAGE.part" "$BASE_URL"
        mv "$BASE_IMAGE.part" "$BASE_IMAGE"
    fi
fi
echo "==> Preparo $OUT da $(basename "$BASE_IMAGE")"
case "$BASE_IMAGE" in
    *.xz) xz -dc "$BASE_IMAGE" > "$WORK/$OUT" ;;
    *)    cp "$BASE_IMAGE" "$WORK/$OUT" ;;
esac
IMG="$WORK/$OUT"

# ---------- 2. spazio per i pacchetti ----------
truncate -s "+${EXTRA_MB}M" "$IMG"
parted -s "$IMG" resizepart 2 100%
LOOP_BOOT="$(part_loop "$IMG" 1)"
LOOP_ROOT="$(part_loop "$IMG" 2)"
e2fsck -fy "$LOOP_ROOT" >/dev/null || [ $? -le 1 ]
resize2fs "$LOOP_ROOT" >/dev/null

# ---------- 3. monta e prepara il chroot ----------
mkdir -p "$ROOT"
mount "$LOOP_ROOT" "$ROOT"
if [ -d "$ROOT/boot/firmware" ]; then BOOT="$ROOT/boot/firmware"; else BOOT="$ROOT/boot"; fi
mount "$LOOP_BOOT" "$BOOT"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"

RESOLV_BAK=""
if [ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ]; then
    RESOLV_BAK="$ROOT/etc/resolv.conf.onvif-bak"
    mv "$ROOT/etc/resolv.conf" "$RESOLV_BAK"
fi
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
# Niente avvio di servizi dentro il chroot
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod +x "$ROOT/usr/sbin/policy-rc.d"

rm -rf "$ROOT/tmp/onvif-viewer"
mkdir -p "$ROOT/tmp/onvif-viewer"
cp -r "$SRC/app" "$SRC/systemd" "$SRC/polkit" "$SRC/install.sh" "$ROOT/tmp/onvif-viewer/"

# ---------- 4. installazione nel chroot ----------
echo "==> Installo ONVIF Viewer nell'immagine (emulazione arm64, qualche minuto)"
chroot "$ROOT" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 \
    PI_USER="$PI_USER" PI_PASSWORD="$PI_PASSWORD" ENABLE_SSH="$ENABLE_SSH" \
    HOSTNAME_PI="$HOSTNAME_PI" /bin/bash -euo pipefail -c '
    IMAGE_BUILD=1 bash /tmp/onvif-viewer/install.sh

    echo "==> Utente $PI_USER"
    if ! id "$PI_USER" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "$PI_USER"
    fi
    for g in sudo adm video render audio input netdev plugdev gpio i2c spi dialout; do
        getent group "$g" >/dev/null && usermod -aG "$g" "$PI_USER"
    done
    echo "$PI_USER:$PI_PASSWORD" | chpasswd
    chage -d 0 "$PI_USER"          # obbliga a cambiarla al primo accesso
    # Niente procedura guidata di primo avvio: l utente esiste già
    systemctl disable userconfig.service 2>/dev/null || true
    systemctl mask userconfig.service 2>/dev/null || true
    rm -f /etc/ssh/sshd_config.d/rename_user.conf

    if [ "$ENABLE_SSH" = 1 ]; then
        systemctl enable ssh 2>/dev/null || true
    fi

    echo "$HOSTNAME_PI" > /etc/hostname
    if grep -q "^127.0.1.1" /etc/hosts; then
        sed -i "s/^127.0.1.1.*/127.0.1.1\t$HOSTNAME_PI/" /etc/hosts
    else
        printf "127.0.1.1\t%s\n" "$HOSTNAME_PI" >> /etc/hosts
    fi

    apt-get clean
    rm -rf /var/lib/apt/lists/* /tmp/onvif-viewer
'

rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/resolv.conf"
[ -n "$RESOLV_BAK" ] && mv "$RESOLV_BAK" "$ROOT/etc/resolv.conf"

# File informativo visibile anche da Windows nella partizione di boot
cat > "$BOOT/onvif-viewer.txt" <<EOF
ONVIF Viewer - Raspberry Pi 4
Immagine creata il $(date -u +%Y-%m-%d)

All'avvio il monitor mostra l'indirizzo dell'interfaccia web (http://<ip>).
Accesso web iniziale: admin / admin
Accesso SSH/console: $PI_USER / (password impostata alla creazione), da cambiare al primo accesso
EOF

# ---------- 5. chiusura ----------
sync
for m in boot/firmware boot dev/pts dev proc sys run; do
    mountpoint -q "$ROOT/$m" && umount "$ROOT/$m"
done
umount "$ROOT"
e2fsck -fy "$LOOP_ROOT" >/dev/null || [ $? -le 1 ]
cleanup
trap - EXIT

mv "$IMG" "$SRC/image/$OUT"
if [ "${NO_XZ:-0}" != 1 ]; then
    echo "==> Comprimo"
    xz -T0 -6 -f "$SRC/image/$OUT"
    OUT="$OUT.xz"
fi
(cd "$SRC/image" && sha256sum "$OUT" > "$OUT.sha256")
echo
echo "Immagine pronta: $SRC/image/$OUT"
echo "Scrivila sulla microSD con Raspberry Pi Imager (Scegli OS > Usa immagine personalizzata)."
