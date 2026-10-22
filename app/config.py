"""Configurazione persistente in /etc/onvif-viewer/config.json.

Il file viene scritto in modo atomico: il player ne osserva la data di modifica
e si riavvia quando cambia.
"""

import json
import os
import tempfile

CONFIG_PATH = os.environ.get("ONVIF_VIEWER_CONFIG", "/etc/onvif-viewer/config.json")

DEFAULTS = {
    "camera": {
        "host": "",
        "port": 80,
        "username": "",
        "password": "",
        "profile_token": "",
        "profile_name": "",
        "stream_uri": "",        # URI restituito dalla telecamera (senza credenziali)
        "manual_rtsp": "",       # se valorizzato, ignora ONVIF e usa questo URL
    },
    "player": {
        "transport": "tcp",      # tcp | udp
        "low_latency": True,
        "hwdec": "auto-safe",
        "keep_aspect": True,
        "audio": False,
        "rotate": 0,             # 0 | 90 | 180 | 270
    },
    "web": {
        "username": "admin",
        "password_hash": "",     # vuoto = password iniziale "admin"
        "secret_key": "",
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def load():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return _merge(DEFAULTS, data)


def save(cfg):
    d = os.path.dirname(CONFIG_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def mtime():
    try:
        return os.stat(CONFIG_PATH).st_mtime_ns
    except FileNotFoundError:
        return 0
