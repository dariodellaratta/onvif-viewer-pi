#!/usr/bin/env python3
"""Interfaccia web di configurazione: telecamera ONVIF, rete del Raspberry, sistema."""

import functools
import os
import secrets
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import (Flask, Response, jsonify, redirect, render_template, request,  # noqa: E402
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import config  # noqa: E402
import network  # noqa: E402
from onvif_client import OnvifCamera, OnvifError, discover, mask_password  # noqa: E402

DEFAULT_PASSWORD = "admin"
VERSION = "1.0.0"


def _init_secret():
    cfg = config.load()
    if not cfg["web"].get("secret_key"):
        cfg["web"]["secret_key"] = secrets.token_hex(32)
        try:
            config.save(cfg)
        except OSError:
            pass
    return cfg["web"]["secret_key"]


app = Flask(__name__)
app.secret_key = _init_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  PERMANENT_SESSION_LIFETIME=8 * 3600)


# ---------- autenticazione ----------

def _check_login(username, password):
    w = config.load()["web"]
    if username != w.get("username", "admin"):
        return False
    if not w.get("password_hash"):
        return password == DEFAULT_PASSWORD
    return check_password_hash(w["password_hash"], password)


def login_required(view):
    @functools.wraps(view)
    def wrapper(*a, **kw):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="Sessione scaduta, accedi di nuovo"), 401
            return redirect(url_for("login"))
        # Le chiamate API mutanti richiedono un header personalizzato (difesa CSRF)
        if request.path.startswith("/api/") and request.method == "POST" \
                and request.headers.get("X-Requested-With") != "onvif-viewer":
            return jsonify(error="Richiesta non valida"), 400
        return view(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if _check_login(request.form.get("username", ""), request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user"] = request.form["username"]
            return redirect(url_for("index"))
        time.sleep(1.5)
        error = "Credenziali non valide"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- pagine ----------

@app.route("/")
@login_required
def index():
    cfg = config.load()
    return render_template("index.html", cam=cfg["camera"], player=cfg["player"],
                           web_user=cfg["web"]["username"],
                           default_password=not cfg["web"].get("password_hash"),
                           version=VERSION)


# ---------- API telecamera ----------

def _camera_from_request(data, cfg):
    host = (data.get("host") or "").strip()
    if not host:
        raise OnvifError("Inserisci l'indirizzo IP della telecamera")
    password = data.get("password")
    if not password and host == cfg["camera"]["host"]:
        password = cfg["camera"]["password"]  # campo lasciato vuoto = mantieni
    try:
        port = int(data.get("port") or 80)
    except ValueError:
        raise OnvifError("Porta non valida")
    return OnvifCamera(host, port, (data.get("username") or "").strip(), password or "")


@app.post("/api/discover")
@login_required
def api_discover():
    try:
        return jsonify(devices=discover(timeout=3))
    except OSError as e:
        return jsonify(error=f"Ricerca non riuscita: {e}"), 500


@app.post("/api/profiles")
@login_required
def api_profiles():
    cfg = config.load()
    try:
        cam = _camera_from_request(request.get_json(force=True), cfg).connect()
        try:
            info = cam.device_info()
        except OnvifError:
            info = {}
        return jsonify(info=info, profiles=cam.profiles())
    except OnvifError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/camera")
@login_required
def api_camera_save():
    data = request.get_json(force=True)
    cfg = config.load()
    manual = (data.get("manual_rtsp") or "").strip()
    if manual and not manual.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        return jsonify(error="L'URL manuale deve iniziare con rtsp://"), 400

    cam_cfg = dict(cfg["camera"], manual_rtsp=manual)
    if not manual:
        try:
            cam = _camera_from_request(data, cfg).connect()
            token = data.get("profile_token") or ""
            if not token:
                token = cam.profiles()[0]["token"]
            uri = cam.stream_uri(token)
        except OnvifError as e:
            return jsonify(error=str(e)), 400
        cam_cfg.update(host=cam.host, port=cam.port, username=cam.username,
                       password=cam.password, profile_token=token,
                       profile_name=data.get("profile_name") or token, stream_uri=uri)
    cfg["camera"] = cam_cfg

    p = data.get("player")
    if isinstance(p, dict):
        try:
            rotate = int(p.get("rotate") or 0)
        except ValueError:
            rotate = 0
        cfg["player"].update(
            transport="udp" if p.get("transport") == "udp" else "tcp",
            low_latency=bool(p.get("low_latency", True)),
            keep_aspect=bool(p.get("keep_aspect", True)),
            audio=bool(p.get("audio", False)),
            rotate=rotate if rotate in (0, 90, 180, 270) else 0,
        )
    config.save(cfg)
    shown = manual or cam_cfg["stream_uri"]
    return jsonify(ok=True, stream=mask_password(shown))


@app.post("/api/camera/clear")
@login_required
def api_camera_clear():
    cfg = config.load()
    cfg["camera"] = dict(config.DEFAULTS["camera"])
    config.save(cfg)
    return jsonify(ok=True)


@app.get("/api/snapshot")
@login_required
def api_snapshot():
    c = config.load()["camera"]
    if not c["host"] or not c["profile_token"]:
        return jsonify(error="Nessuna telecamera configurata"), 404
    try:
        cam = OnvifCamera(c["host"], c["port"], c["username"], c["password"]).connect()
        data, ctype = cam.snapshot(c["profile_token"])
    except OnvifError as e:
        return jsonify(error=str(e)), 502
    return Response(data, mimetype=ctype, headers={"Cache-Control": "no-store"})


# ---------- API rete ----------

@app.get("/api/network")
@login_required
def api_network_get():
    try:
        return jsonify(devices=network.devices(), hostname=network.hostname())
    except network.NetworkError as e:
        return jsonify(error=str(e)), 500


@app.post("/api/network")
@login_required
def api_network_set():
    d = request.get_json(force=True)
    try:
        conn = network.configure(d.get("device", ""), d.get("method", "auto"),
                                 d.get("address", ""), d.get("gateway", ""), d.get("dns", ""))
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400
    new_url = ""
    if d.get("method") == "manual":
        new_url = "http://" + d["address"].split("/")[0].strip()
    return jsonify(ok=True, connection=conn, new_url=new_url)


@app.get("/api/wifi")
@login_required
def api_wifi_scan():
    try:
        return jsonify(networks=network.wifi_scan(), saved=network.wifi_saved())
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/wifi/connect")
@login_required
def api_wifi_connect():
    d = request.get_json(force=True)
    try:
        name = network.wifi_connect(d.get("ssid", ""), d.get("password", ""),
                                    bool(d.get("hidden")))
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, connection=name)


@app.post("/api/wifi/forget")
@login_required
def api_wifi_forget():
    try:
        network.wifi_forget(request.get_json(force=True).get("name", ""))
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/hostname")
@login_required
def api_hostname():
    try:
        network.set_hostname(request.get_json(force=True).get("hostname", ""))
    except network.NetworkError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ---------- API sistema ----------

@app.get("/api/status")
@login_required
def api_status():
    def active(unit):
        try:
            return subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except Exception:
            return "sconosciuto"
    try:
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:
        temp = None
    try:
        uptime = float(open("/proc/uptime").read().split()[0])
    except Exception:
        uptime = None
    c = config.load()["camera"]
    stream = c["manual_rtsp"] or c["stream_uri"]
    return jsonify(player=active("onvif-player.service"), temperature=temp, uptime=uptime,
                   ips=subprocess.run(["hostname", "-I"], capture_output=True,
                                      text=True).stdout.split(),
                   camera=c["host"] or ("manuale" if c["manual_rtsp"] else ""),
                   profile=c["profile_name"], stream=mask_password(stream) if stream else "")


@app.post("/api/password")
@login_required
def api_password():
    d = request.get_json(force=True)
    if not _check_login(session["user"], d.get("current", "")):
        return jsonify(error="Password attuale errata"), 400
    new = d.get("new", "")
    if len(new) < 6:
        return jsonify(error="La nuova password deve avere almeno 6 caratteri"), 400
    cfg = config.load()
    cfg["web"]["password_hash"] = generate_password_hash(new)
    config.save(cfg)
    return jsonify(ok=True)


@app.post("/api/player/restart")
@login_required
def api_player_restart():
    config.save(config.load())  # il player si riavvia quando cambia la config
    return jsonify(ok=True)


@app.post("/api/reboot")
@login_required
def api_reboot():
    subprocess.Popen(["sh", "-c", "sleep 2; systemctl reboot"])
    return jsonify(ok=True)


def main():
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "80"))
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
