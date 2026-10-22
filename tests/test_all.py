import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp()
os.environ["ONVIF_VIEWER_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["FAKE_NMCLI_LOG"] = os.path.join(TMP, "nmcli.log")

import mock_onvif  # noqa: E402
import config  # noqa: E402
import network  # noqa: E402
import onvif_client as oc  # noqa: E402

network.NMCLI = os.path.join(HERE, "fake_nmcli")


class OnvifTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # telecamera con l'orologio indietro di 2 ore: il client deve compensare
        cls.srv = mock_onvif.start(clock_skew=dt.timedelta(hours=-2))
        cls.port = cls.srv.server_address[1]

    def cam(self, pw=mock_onvif.PASS):
        return oc.OnvifCamera("127.0.0.1", self.port, mock_onvif.USER, pw).connect()

    def test_profiles_and_stream(self):
        c = self.cam()
        self.assertEqual(c.media_url, f"http://127.0.0.1:{self.port}/onvif/Media")
        p = c.profiles()
        self.assertEqual([x["token"] for x in p], ["prof_main", "prof_sub"])
        self.assertEqual((p[0]["width"], p[0]["height"], p[0]["encoding"], p[0]["fps"]),
                         ("1920", "1080", "H264", "25"))
        self.assertEqual(c.stream_uri("prof_sub"), "rtsp://10.99.99.99:554/prof_sub")
        self.assertEqual(c.device_info()["Model"], "Cam-1")

    def test_snapshot(self):
        data, ctype = self.cam().snapshot("prof_main")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_wrong_password(self):
        with self.assertRaises(oc.OnvifError) as e:
            self.cam("sbagliata")
        self.assertIn("Autenticazione", str(e.exception))

    def test_unreachable(self):
        with self.assertRaises(oc.OnvifError):
            oc.OnvifCamera("127.0.0.1", 1, "a", "b", timeout=1).connect()

    def test_rtsp_credentials(self):
        u = oc.rtsp_with_credentials("rtsp://10.99.99.99:554/s?x=1", "ad min", "p@ss:w/", "192.168.1.5")
        self.assertEqual(u, "rtsp://ad%20min:p%40ss%3Aw%2F@192.168.1.5:554/s?x=1")
        self.assertEqual(oc.mask_password(u), "rtsp://ad%20min:****@192.168.1.5:554/s?x=1")

    def test_probe_match_parse(self):
        xml = b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches><d:ProbeMatch>
          <d:Scopes>onvif://www.onvif.org/name/My%20Cam onvif://www.onvif.org/hardware/DS-2CD</d:Scopes>
          <d:XAddrs>http://[fe80::1]/onvif/device_service http://192.168.1.64:8000/onvif/device_service</d:XAddrs>
          </d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>'''
        r = oc._parse_probe_match(xml, "192.168.1.64")
        self.assertEqual(r["192.168.1.64"]["port"], 8000)
        self.assertEqual(r["192.168.1.64"]["name"], "My Cam")


class NetworkTest(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(network.validate("manual", "192.168.1.50/24", "192.168.1.1", "8.8.8.8, 1.1.1.1"),
                         ("192.168.1.50/24", "192.168.1.1", ["8.8.8.8", "1.1.1.1"]))
        for bad in [("192.168.1.50", "", ""), ("192.168.1.0/24", "", ""),
                    ("192.168.1.50/24", "10.0.0.1", ""), ("300.1.1.1/24", "", ""),
                    ("192.168.1.50/24", "", "dns.google")]:
            with self.assertRaises(network.NetworkError):
                network.validate("manual", *bad)

    def test_devices_and_configure(self):
        d = network.devices()
        self.assertEqual([x["device"] for x in d], ["eth0", "wlan0"])
        self.assertEqual(d[0]["addresses"], ["192.168.1.23/24"])
        self.assertEqual(d[1]["connection"], "")  # nessun profilo legato a wlan0
        open(os.environ["FAKE_NMCLI_LOG"], "w").close()
        network.configure("eth0", "manual", "192.168.1.50/24", "192.168.1.1", "1.1.1.1", delay=0)
        time.sleep(0.5)
        log = open(os.environ["FAKE_NMCLI_LOG"]).read()
        self.assertIn("'modify' 'Wired connection 1' 'ipv4.method' 'manual' 'ipv4.addresses' "
                      "'192.168.1.50/24' 'ipv4.gateway' '192.168.1.1' 'ipv4.dns' '1.1.1.1'", log)
        self.assertIn("'up' 'Wired connection 1' 'ifname' 'eth0'", log)


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_onvif.start()
        cls.port = cls.srv.server_address[1]
        import web
        web.time.sleep = lambda s: None
        cls.web = web
        cls.c = web.app.test_client()

    def post(self, url, body):
        return self.c.post(url, json=body, headers={"X-Requested-With": "onvif-viewer"})

    def test_flow(self):
        c = self.c
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "x"}).status_code, 200)
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "admin"}).status_code, 302)
        self.assertIn(b"password iniziale", c.get("/").data)

        # CSRF: senza header personalizzato
        self.assertEqual(c.post("/api/camera", json={}).status_code, 400)

        cam = {"host": "127.0.0.1", "port": self.port, "username": mock_onvif.USER,
               "password": mock_onvif.PASS}
        r = self.post("/api/profiles", cam)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["info"]["Manufacturer"], "Acme")

        r = self.post("/api/camera", dict(cam, profile_token="prof_sub", profile_name="SubStream",
                                          player={"transport": "udp", "rotate": "180"}))
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["stream"], "rtsp://10.99.99.99:554/prof_sub")
        cfg = config.load()
        self.assertEqual(cfg["camera"]["password"], mock_onvif.PASS)
        self.assertEqual(cfg["player"]["rotate"], 180)

        # password vuota = mantieni quella salvata
        r = self.post("/api/camera", dict(cam, password="", profile_token="prof_main"))
        self.assertEqual(r.status_code, 200, r.json)

        r = c.get("/api/snapshot")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")

        r = self.post("/api/profiles", dict(cam, password="no"))
        self.assertEqual(r.status_code, 400)

        # player: comando mpv con credenziali e host corretto
        import player
        url = player.build_url(config.load())
        self.assertEqual(url, f"rtsp://admin:s3gret%3A%26@127.0.0.1:554/prof_main")
        cmd = player.stream_cmd(config.load(), url)
        self.assertIn("--rtsp-transport=udp", cmd)
        self.assertIn("--video-rotate=180", cmd)
        self.assertEqual(cmd[-1], url)

        r = self.post("/api/camera", {"manual_rtsp": "ftp://x"})
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/camera", {"manual_rtsp": "rtsp://u:p@1.2.3.4/live"})
        self.assertEqual(r.json["stream"], "rtsp://u:****@1.2.3.4/live")
        self.assertEqual(player.build_url(config.load()), "rtsp://u:p@1.2.3.4/live")

        # rete
        r = c.get("/api/network")
        self.assertEqual(r.json["hostname"], "raspberrypi")
        r = self.post("/api/network", {"device": "eth0", "method": "manual", "address": "192.168.1.50"})
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/network", {"device": "eth0", "method": "manual",
                                       "address": "192.168.1.50/24", "gateway": "192.168.1.1"})
        self.assertEqual(r.json["new_url"], "http://192.168.1.50")

        # password web
        self.assertEqual(self.post("/api/password", {"current": "admin", "new": "abc"}).status_code, 400)
        self.assertEqual(self.post("/api/password", {"current": "admin", "new": "nuova123"}).status_code, 200)
        c.get("/logout")
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "admin"}).status_code, 200)
        self.assertEqual(c.post("/login", data={"username": "admin", "password": "nuova123"}).status_code, 302)
        self.assertEqual(json.load(open(config.CONFIG_PATH))["camera"]["manual_rtsp"], "rtsp://u:p@1.2.3.4/live")


class PlayerLoopTest(unittest.TestCase):
    def test_restart_on_config_change(self):
        import player
        fake = os.path.join(TMP, "mpv")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nsleep 30\n")
        os.chmod(fake, 0o755)
        m = config.mtime()
        t0 = time.monotonic()
        import threading
        threading.Timer(1.0, lambda: config.save(config.load())).start()
        exited = player.run_until([fake], m)
        self.assertFalse(exited)
        self.assertLess(time.monotonic() - t0, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
