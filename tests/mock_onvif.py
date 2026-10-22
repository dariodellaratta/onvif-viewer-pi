"""Telecamera ONVIF simulata per i test (verifica il digest WS-Security)."""

import base64
import datetime as dt
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASS = "admin", "s3gret:&"

ENV = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
       'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
       'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{}</s:Body></s:Envelope>')

FAULT = ENV.format('<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized'
                   '</s:Value></s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">Sender not Authorized'
                   '</s:Text></s:Reason></s:Fault>')


def make_handler(port, clock_skew):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/soap+xml"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/snap.jpg":
                return self._send(200, b"\xff\xd8JPEGDATA\xff\xd9", "image/jpeg")
            self._send(404, "")

        def _authorized(self, body):
            m = re.search(r"<Username>(.*?)</Username>.*?<Password[^>]*>(.*?)</Password>.*?"
                          r"<Nonce[^>]*>(.*?)</Nonce>.*?<Created[^>]*>(.*?)</Created>", body, re.S)
            if not m:
                return False
            user, digest, nonce, created = m.groups()
            exp = base64.b64encode(hashlib.sha1(base64.b64decode(nonce) + created.encode()
                                                + PASS.encode()).digest()).decode()
            ts = dt.datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
            cam_now = dt.datetime.now(dt.timezone.utc) + clock_skew
            return user == USER and digest == exp and abs((cam_now - ts).total_seconds()) < 30

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            if "GetSystemDateAndTime" in body:
                n = dt.datetime.now(dt.timezone.utc) + clock_skew
                return self._send(200, ENV.format(
                    f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                    f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                    f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
                    f"</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"))
            if not self._authorized(body):
                return self._send(400, FAULT)
            if "GetCapabilities" in body:
                # XAddr volutamente errato (IP pubblicato diverso): il client deve correggerlo
                return self._send(200, ENV.format(
                    "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>"
                    "<tt:XAddr>http://10.99.99.99/onvif/Media</tt:XAddr></tt:Media>"
                    "</tds:Capabilities></tds:GetCapabilitiesResponse>"))
            if "GetDeviceInformation" in body:
                return self._send(200, ENV.format(
                    "<tds:GetDeviceInformationResponse><tds:Manufacturer>Acme</tds:Manufacturer>"
                    "<tds:Model>Cam-1</tds:Model><tds:FirmwareVersion>1.2</tds:FirmwareVersion>"
                    "<tds:SerialNumber>XYZ</tds:SerialNumber></tds:GetDeviceInformationResponse>"))
            if self.path != "/onvif/Media":
                return self._send(404, "")
            if "GetProfiles" in body:
                prof = ""
                for tok, name, w, h in (("prof_main", "MainStream", 1920, 1080),
                                        ("prof_sub", "SubStream", 640, 360)):
                    prof += (f'<trt:Profiles token="{tok}" fixed="true"><tt:Name>{name}</tt:Name>'
                             f'<tt:VideoEncoderConfiguration token="v{tok}"><tt:Name>enc</tt:Name>'
                             f"<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{w}</tt:Width>"
                             f"<tt:Height>{h}</tt:Height></tt:Resolution><tt:RateControl>"
                             f"<tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl>"
                             f"</tt:VideoEncoderConfiguration></trt:Profiles>")
                return self._send(200, ENV.format(f"<trt:GetProfilesResponse>{prof}</trt:GetProfilesResponse>"))
            if "GetStreamUri" in body:
                tok = re.search(r"ProfileToken>(.*?)<", body).group(1)
                return self._send(200, ENV.format(
                    f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://10.99.99.99:554/{tok}"
                    f"</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"))
            if "GetSnapshotUri" in body:
                return self._send(200, ENV.format(
                    f"<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>http://10.99.99.99:{port}/snap.jpg"
                    f"</tt:Uri></trt:MediaUri></trt:GetSnapshotUriResponse>"))
            self._send(400, FAULT)
    return H


def start(clock_skew=dt.timedelta(0)):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    srv.RequestHandlerClass = make_handler(srv.server_address[1], clock_skew)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
