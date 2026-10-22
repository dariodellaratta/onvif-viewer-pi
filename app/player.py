#!/usr/bin/env python3
"""Supervisore del player: mostra lo stream RTSP a schermo intero su HDMI con mpv.

- Se non c'è una telecamera configurata mostra una schermata con l'indirizzo
  dell'interfaccia web.
- Se lo stream cade, mostra un messaggio e riprova ogni pochi secondi.
- Quando la configurazione cambia, riavvia mpv con i nuovi parametri.
"""

import logging
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from onvif_client import mask_password, rtsp_with_credentials  # noqa: E402

log = logging.getLogger("player")
RETRY_SECONDS = 5
MPV = os.environ.get("MPV_BIN", "mpv")
VO_ARGS = os.environ.get("MPV_VO", "--vo=gpu --gpu-context=drm").split()

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def build_url(cfg):
    cam = cfg["camera"]
    if cam.get("manual_rtsp"):
        return cam["manual_rtsp"].strip()
    if not cam.get("stream_uri"):
        return ""
    return rtsp_with_credentials(cam["stream_uri"], cam.get("username"),
                                 cam.get("password"), host_override=cam.get("host") or None)


def common_args():
    return [MPV, *VO_ARGS, "--fs", "--no-osc", "--no-input-default-bindings",
            "--input-vo-keyboard=no", "--cursor-autohide=always",
            "--osd-font-size=42", "--msg-level=all=warn", "--no-terminal"]


def stream_cmd(cfg, url):
    p = cfg["player"]
    cmd = common_args() + [
        f"--hwdec={p.get('hwdec') or 'auto-safe'}",
        f"--rtsp-transport={'udp' if p.get('transport') == 'udp' else 'tcp'}",
        "--network-timeout=10",
        "--osd-level=0",
    ]
    if p.get("low_latency"):
        cmd += ["--profile=low-latency", "--cache=no"]
    if not p.get("audio"):
        cmd.append("--no-audio")
    if not p.get("keep_aspect", True):
        cmd.append("--keepaspect=no")
    if int(p.get("rotate") or 0):
        cmd.append(f"--video-rotate={int(p['rotate'])}")
    return cmd + ["--", url]


def splash_cmd(text):
    return common_args() + [
        "--osd-level=1", f"--osd-msg1={text}", "--no-audio", "--loop=inf",
        "av://lavfi:color=c=0x101418:s=1280x720:r=2",
    ]


def local_ips():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        return [ip for ip in out.stdout.split() if ":" not in ip]
    except Exception:
        return []


def web_hint():
    ips = local_ips()
    if not ips:
        return "Nessuna rete: collega il cavo di rete"
    return "Configura da  " + "   ".join(f"http://{ip}" for ip in ips)


def run_until(cmd, start_mtime, max_seconds=None):
    """Esegue mpv finché termina, cambia la config o scade il tempo.
    Ritorna True se mpv è uscito da solo."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    t0 = time.monotonic()
    try:
        while not _stop:
            if proc.poll() is not None:
                return True
            if config.mtime() != start_mtime:
                return False
            if max_seconds and time.monotonic() - t0 > max_seconds:
                return False
            time.sleep(0.5)
        return False
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while not _stop:
        m = config.mtime()
        cfg = config.load()
        url = build_url(cfg)
        if not url:
            log.info("Nessuna telecamera configurata, mostro la schermata di attesa")
            # Rinfresca ogni minuto: l'IP del Raspberry può cambiare (DHCP)
            run_until(splash_cmd("Nessuna telecamera configurata\n\n" + web_hint()), m, 60)
            continue

        log.info("Avvio stream %s", mask_password(url))
        t0 = time.monotonic()
        exited = run_until(stream_cmd(cfg, url), m)
        if exited and not _stop:
            ran = time.monotonic() - t0
            log.warning("Stream terminato dopo %.0fs, riprovo tra %ds", ran, RETRY_SECONDS)
            host = cfg["camera"].get("host") or "telecamera"
            run_until(splash_cmd(f"Connessione a {host} persa, nuovo tentativo...\n\n"
                                 + web_hint()), m, RETRY_SECONDS)


if __name__ == "__main__":
    main()
