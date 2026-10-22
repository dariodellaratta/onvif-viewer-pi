"""Lettura e modifica della configurazione IP del Raspberry tramite NetworkManager (nmcli).

Raspberry Pi OS Bookworm e successivi usano NetworkManager di default.
"""

import ipaddress
import subprocess
import threading
import time

NMCLI = "nmcli"


class NetworkError(Exception):
    pass


def _run(args, check=True):
    try:
        r = subprocess.run([NMCLI, *args], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise NetworkError("nmcli non trovato: NetworkManager non è installato")
    except subprocess.TimeoutExpired:
        raise NetworkError("nmcli non risponde")
    if check and r.returncode != 0:
        raise NetworkError((r.stderr or r.stdout).strip() or f"nmcli {' '.join(args)} fallito")
    return r.stdout


def _kv(text):
    """Output di 'nmcli -t --escape no' → lista di (chiave, valore)."""
    out = []
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out.append((k.strip(), v.strip()))
    return out


def devices():
    rows = []
    for line in _run(["-t", "--escape", "no", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                      "device", "status"]).splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4 or parts[1] not in ("ethernet", "wifi"):
            continue
        dev, typ, state, conn = parts
        info = {"device": dev, "type": typ, "state": state, "connection": conn,
                "addresses": [], "gateway": "", "dns": [], "method": "auto",
                "cfg_address": "", "cfg_gateway": "", "cfg_dns": ""}
        for k, v in _kv(_run(["-t", "--escape", "no", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,"
                              "GENERAL.HWADDR", "device", "show", dev], check=False)):
            if k.startswith("IP4.ADDRESS") and v:
                info["addresses"].append(v)
            elif k == "IP4.GATEWAY":
                info["gateway"] = v
            elif k.startswith("IP4.DNS") and v:
                info["dns"].append(v)
            elif k == "GENERAL.HWADDR":
                info["mac"] = v
        name = conn or _find_connection(dev)
        if name:
            info["connection"] = name
            s = dict(_kv(_run(["-t", "--escape", "no", "-f",
                               "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                               "connection", "show", name], check=False)))
            info["method"] = s.get("ipv4.method") or "auto"
            info["cfg_address"] = (s.get("ipv4.addresses") or "").split(",")[0].strip()
            info["cfg_gateway"] = s.get("ipv4.gateway") or ""
            info["cfg_dns"] = (s.get("ipv4.dns") or "").replace(",", " ")
        rows.append(info)
    return rows


def _find_connection(dev):
    for line in _run(["-t", "--escape", "no", "-f", "NAME,TYPE", "connection", "show"],
                     check=False).splitlines():
        name, _, typ = line.rpartition(":")
        if not name or typ not in ("802-3-ethernet", "802-11-wireless"):
            continue
        iface = dict(_kv(_run(["-t", "--escape", "no", "-f", "connection.interface-name",
                               "connection", "show", name], check=False)))
        if iface.get("connection.interface-name") == dev:
            return name
    return ""


def validate(method, address="", gateway="", dns=""):
    """Ritorna (indirizzo/prefisso, gateway, [dns]) normalizzati, o solleva NetworkError."""
    if method == "auto":
        return "", "", []
    if method != "manual":
        raise NetworkError("Metodo non valido")
    try:
        iface = ipaddress.IPv4Interface(address.strip())
    except ValueError:
        raise NetworkError("Indirizzo IP non valido: usa il formato 192.168.1.50/24")
    if "/" not in address:
        raise NetworkError("Specifica anche la maschera, es. 192.168.1.50/24")
    if iface.ip == iface.network.network_address or iface.ip == iface.network.broadcast_address:
        if iface.network.prefixlen < 31:
            raise NetworkError("L'indirizzo coincide con l'indirizzo di rete o di broadcast")
    gw = ""
    if gateway.strip():
        try:
            gw_ip = ipaddress.IPv4Address(gateway.strip())
        except ValueError:
            raise NetworkError("Gateway non valido")
        if gw_ip not in iface.network:
            raise NetworkError(f"Il gateway {gw_ip} non è nella rete {iface.network}")
        gw = str(gw_ip)
    servers = []
    for d in dns.replace(",", " ").split():
        try:
            servers.append(str(ipaddress.IPv4Address(d)))
        except ValueError:
            raise NetworkError(f"DNS non valido: {d}")
    return iface.with_prefixlen, gw, servers


def configure(dev, method, address="", gateway="", dns="", delay=2.0):
    """Salva la configurazione IPv4 e la applica dopo `delay` secondi, così la
    risposta HTTP arriva al browser prima che l'indirizzo cambi."""
    addr, gw, servers = validate(method, address, gateway, dns)
    known = {d["device"]: d for d in devices()}
    if dev not in known:
        raise NetworkError(f"Interfaccia {dev} non trovata")
    name = known[dev]["connection"]
    if not name:
        if known[dev]["type"] != "ethernet":
            raise NetworkError(f"Nessuna connessione configurata su {dev}")
        name = f"onvif-{dev}"
        _run(["connection", "add", "type", "ethernet", "ifname", dev, "con-name", name])

    if method == "auto":
        args = ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "",
                "ipv4.dns", ""]
    else:
        args = ["ipv4.method", "manual", "ipv4.addresses", addr,
                "ipv4.gateway", gw, "ipv4.dns", " ".join(servers)]
    _run(["connection", "modify", name, *args])

    def apply():
        time.sleep(delay)
        _run(["connection", "up", name, "ifname", dev], check=False)

    threading.Thread(target=apply, daemon=True).start()
    return name


def hostname():
    return _run(["general", "hostname"], check=False).strip()


def set_hostname(name):
    name = name.strip().lower()
    if not name or len(name) > 63 or not all(c.isalnum() or c == "-" for c in name) \
            or name.startswith("-") or name.endswith("-"):
        raise NetworkError("Nome host non valido (lettere, numeri e trattini)")
    _run(["general", "hostname", name])
