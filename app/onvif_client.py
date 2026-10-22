"""Client ONVIF minimale: WS-Discovery, profili media, URI RTSP e snapshot.

Usa solo SOAP 1.2 su HTTP con WS-Security UsernameToken (PasswordDigest).
Se la telecamera risponde 401 riprova con HTTP Digest, che alcuni modelli
richiedono al posto di (o insieme a) WS-Security.
"""

import base64
import datetime as dt
import hashlib
import os
import re
import socket
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.auth import HTTPDigestAuth

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
}

WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PW_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
             "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
B64 = ("http://docs.oasis-open.org/wss/2004/01/"
       "oasis-200401-wss-soap-message-security-1.0#Base64Binary")


class OnvifError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find_local(elem, name):
    """Primo discendente con quel nome locale, ignorando il namespace."""
    for e in elem.iter():
        if _local(e.tag) == name:
            return e
    return None


def _text(elem, name, default=""):
    e = _find_local(elem, name)
    return e.text.strip() if e is not None and e.text else default


class OnvifCamera:
    def __init__(self, host, port=80, username="", password="", timeout=8):
        self.host = host
        self.port = int(port or 80)
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self.device_url = f"http://{self._hostport()}/onvif/device_service"
        self.media_url = None
        self.time_offset = dt.timedelta(0)
        self.session = requests.Session()

    def _hostport(self):
        h = f"[{self.host}]" if ":" in self.host else self.host
        return h if self.port == 80 else f"{h}:{self.port}"

    # ---------- SOAP ----------

    def _security_header(self):
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.time_offset).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            f'<Security s:mustUnderstand="1" xmlns="{WSSE}"><UsernameToken>'
            f"<Username>{_xml_escape(self.username)}</Username>"
            f'<Password Type="{PW_DIGEST}">{digest}</Password>'
            f'<Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{WSU}">{created}</Created>'
            f"</UsernameToken></Security>"
        )

    def _call(self, url, body, auth=True):
        header = self._security_header() if auth else ""
        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" '
            f'xmlns:trt="{NS["trt"]}" xmlns:tt="{NS["tt"]}">'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            r = self.session.post(url, data=envelope.encode(), headers=headers,
                                  timeout=self.timeout)
            if r.status_code == 401 and auth and self.username:
                r = self.session.post(url, data=envelope.encode(), headers=headers,
                                      timeout=self.timeout,
                                      auth=HTTPDigestAuth(self.username, self.password))
        except requests.RequestException as e:
            raise OnvifError(f"Telecamera non raggiungibile ({self.host}:{self.port}): {e}")

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise OnvifError(f"Risposta non valida dalla telecamera (HTTP {r.status_code})")

        fault = _find_local(root, "Fault")
        if fault is not None:
            reason = _text(fault, "Text") or _text(fault, "faultstring") or "errore sconosciuto"
            subcode = " ".join(e.text or "" for e in fault.iter() if _local(e.tag) == "Value")
            if "NotAuthorized" in subcode or "auth" in reason.lower() or r.status_code in (400, 401):
                raise OnvifError(f"Autenticazione rifiutata: {reason}")
            raise OnvifError(f"Errore ONVIF: {reason}")
        if r.status_code == 401:
            raise OnvifError("Autenticazione rifiutata (HTTP 401): controlla utente e password")
        if r.status_code >= 400:
            raise OnvifError(f"Errore HTTP {r.status_code} dalla telecamera")
        return root

    # ---------- Device ----------

    def sync_time(self):
        """Allinea il timestamp del digest all'orologio della telecamera."""
        try:
            root = self._call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError:
            return
        utc = _find_local(root, "UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                int(_text(utc, "Year")), int(_text(utc, "Month")), int(_text(utc, "Day")),
                int(_text(utc, "Hour")), int(_text(utc, "Minute")), int(_text(utc, "Second")),
                tzinfo=dt.timezone.utc)
        except ValueError:
            return
        self.time_offset = cam - dt.datetime.now(dt.timezone.utc)

    def connect(self):
        self.sync_time()
        root = self._call(self.device_url,
                          "<tds:GetCapabilities><tds:Category>All</tds:Category>"
                          "</tds:GetCapabilities>")
        media = _find_local(root, "Media")
        xaddr = _text(media, "XAddr") if media is not None else ""
        self.media_url = self._fix_xaddr(xaddr) if xaddr else self.device_url
        return self

    def _fix_xaddr(self, xaddr):
        """Molte telecamere dietro NAT o con IP cambiato pubblicano un XAddr
        sbagliato: teniamo il path ma usiamo l'host che sappiamo raggiungere."""
        p = urlsplit(xaddr)
        return urlunsplit((p.scheme or "http", self._hostport(), p.path or "/", p.query, ""))

    def device_info(self):
        root = self._call(self.device_url, "<tds:GetDeviceInformation/>")
        return {k: _text(root, k) for k in
                ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber")}

    # ---------- Media ----------

    def profiles(self):
        if not self.media_url:
            self.connect()
        root = self._call(self.media_url, "<trt:GetProfiles/>")
        out = []
        for p in root.iter():
            if _local(p.tag) != "Profiles":
                continue
            enc = None
            for c in p:
                if _local(c.tag) == "VideoEncoderConfiguration":
                    enc = c
            item = {"token": p.get("token", ""), "name": _text(p, "Name") or p.get("token", "")}
            if enc is not None:
                item["encoding"] = _text(enc, "Encoding")
                item["width"] = _text(enc, "Width")
                item["height"] = _text(enc, "Height")
                item["fps"] = _text(enc, "FrameRateLimit")
            out.append(item)
        if not out:
            raise OnvifError("La telecamera non espone profili video")
        return out

    def stream_uri(self, token):
        if not self.media_url:
            self.connect()
        body = (
            "<trt:GetStreamUri><trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            f"</trt:StreamSetup><trt:ProfileToken>{_xml_escape(token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        uri = _text(self._call(self.media_url, body), "Uri")
        if not uri:
            raise OnvifError("La telecamera non ha restituito un URI RTSP")
        return uri

    def snapshot_uri(self, token):
        if not self.media_url:
            self.connect()
        body = (f"<trt:GetSnapshotUri><trt:ProfileToken>{_xml_escape(token)}"
                "</trt:ProfileToken></trt:GetSnapshotUri>")
        uri = _text(self._call(self.media_url, body), "Uri")
        return self._fix_xaddr(uri) if uri else ""

    def snapshot(self, token):
        uri = self.snapshot_uri(token)
        if not uri:
            raise OnvifError("Snapshot non supportato da questa telecamera")
        try:
            r = self.session.get(uri, timeout=self.timeout)
            if r.status_code == 401 and self.username:
                r = self.session.get(uri, timeout=self.timeout,
                                     auth=HTTPDigestAuth(self.username, self.password))
                if r.status_code == 401:
                    r = self.session.get(uri, timeout=self.timeout,
                                         auth=(self.username, self.password))
        except requests.RequestException as e:
            raise OnvifError(f"Snapshot non disponibile: {e}")
        if r.status_code != 200:
            raise OnvifError(f"Snapshot non disponibile (HTTP {r.status_code})")
        return r.content, r.headers.get("Content-Type", "image/jpeg")


def rtsp_with_credentials(uri, username, password, host_override=None):
    """Inserisce utente/password nell'URI RTSP e, se richiesto, corregge l'host."""
    p = urlsplit(uri)
    host = host_override or p.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{p.port}" if p.port else "")
    if username:
        netloc = f"{quote(username, safe='')}:{quote(password or '', safe='')}@{netloc}"
    return urlunsplit((p.scheme or "rtsp", netloc, p.path, p.query, p.fragment))


def mask_password(uri):
    return re.sub(r"(rtsp://[^:/@]+:)[^@]*@", r"\1****@", uri)


def discover(timeout=3.0):
    """WS-Discovery: trova i dispositivi ONVIF nella rete locale."""
    msg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<e:Envelope xmlns:e="{NS["s"]}" xmlns:w="{NS["a"]}" xmlns:d="{NS["d"]}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<e:Header>"
        f"<w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found = {}
    try:
        sock.bind(("", 0))
        for _ in range(2):  # UDP: inviamo due volte
            sock.sendto(msg, ("239.255.255.250", 3702))
        deadline = dt.datetime.now() + dt.timedelta(seconds=timeout)
        while dt.datetime.now() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            found.update(_parse_probe_match(data, addr[0]))
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: d["host"])


def _parse_probe_match(data, sender_ip):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    out = {}
    for m in root.iter():
        if _local(m.tag) != "ProbeMatch":
            continue
        xaddrs = _text(m, "XAddrs").split()
        scopes = _text(m, "Scopes").split()
        # Preferisci l'XAddr IPv4 che coincide con il mittente
        xaddr = next((x for x in xaddrs if sender_ip in x), xaddrs[0] if xaddrs else "")
        p = urlsplit(xaddr)
        name = hardware = ""
        for s in scopes:
            if "/name/" in s:
                name = requests.utils.unquote(s.rsplit("/", 1)[-1])
            elif "/hardware/" in s:
                hardware = requests.utils.unquote(s.rsplit("/", 1)[-1])
        host = p.hostname or sender_ip
        out[host] = {"host": host, "port": p.port or 80, "xaddr": xaddr,
                     "name": name, "hardware": hardware}
    return out


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
