import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import tempfile
import threading
import unittest

os.environ["GD_SANDBOX"] = os.path.join(tempfile.mkdtemp(prefix="gd_test_"), "sandbox")
import hosted_server as hs  # noqa: E402


def tree_hashes(root):
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != ".greendata"]
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = hashlib.sha256(fh.read()).hexdigest()  # same style as the scan's names
    return out


class Visitor:
    """A tiny browser: keeps the cookie and the page token."""

    def __init__(self, port):
        self.port, self.cookie, self.token = port, None, None
        self.load_page()

    def _req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        return r, raw

    def load_page(self):
        r, raw = self._req("GET", "/")
        assert r.status == 200
        self.cookie = r.getheader("Set-Cookie").split(";")[0]
        page = raw.decode()
        self.token = re.search(r'window.GD_TOKEN="([^"]+)"', page).group(1)
        self.page = page

    def post(self, path, body, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h["X-GD-Token"] = self.token
        r, raw = self._req("POST", path, body, h)
        return r.status, json.loads(raw or b"{}")


class HostedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, _ = hs.make_hosted_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(os.path.dirname(hs.SANDBOX), ignore_errors=True)

    def test_page_is_served_with_hosted_ui(self):
        v = Visitor(self.port)
        self.assertIn("window.GD_HOSTED=true", v.page)
        self.assertIn("Live server mode", v.page)
        self.assertNotIn("__GD_TOKEN__", v.page)

    def test_health(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/healthz")
        self.assertEqual(c.getresponse().status, 200)

    def test_real_apply_and_rollback_on_server(self):
        v = Visitor(self.port)
        st, r = v.post("/api/demo", {})
        self.assertEqual((st, r["dir"]), (200, "demo"))
        folder = os.path.join(hs.SANDBOX, v.cookie.split("=")[1], "demo")
        before = tree_hashes(folder)

        st, scan = v.post("/api/scan", {"dir": "demo", "keywords": "sales", "threshold": 35, "weights": [35, 25, 25, 15]})
        self.assertEqual(st, 200)
        self.assertEqual(scan["dir"], "demo")
        self.assertNotIn(hs.SANDBOX, json.dumps(scan))  # no server paths leak
        approved = [f["n"] for f in scan["files"] if f["action"] in ("dedupe", "archive", "compress")]
        self.assertTrue(approved)

        st, m = v.post("/api/apply", {"session": scan["session"], "approved": approved, "threshold": 35, "weights": [35, 25, 25, 15]})
        self.assertEqual(st, 200, m)
        self.assertGreater(m["saved_bytes"], 0)
        self.assertTrue(os.path.isdir(os.path.join(folder, ".greendata")))
        self.assertNotEqual(tree_hashes(folder), before)  # files really changed on the server

        st, rb = v.post("/api/rollback", {"dir": "demo", "run": m["run"]})
        self.assertEqual(st, 200, rb)
        self.assertEqual(rb["problems"], [])
        self.assertEqual(tree_hashes(folder), before)  # every byte restored

    def test_sensitive_files_are_never_changed(self):
        v = Visitor(self.port)
        v.post("/api/demo", {})
        st, scan = v.post("/api/scan", {"dir": "demo"})
        folder = os.path.join(hs.SANDBOX, v.cookie.split("=")[1], "demo")
        pii = [f["n"] for f in scan["files"] if f["pii"]]
        self.assertTrue(pii)
        h = tree_hashes(folder)
        v.post("/api/apply", {"session": scan["session"], "approved": [f["n"] for f in scan["files"]]})
        for n in pii:
            self.assertEqual(tree_hashes(folder)[n], h[n])

    def test_typed_paths_are_rejected(self):
        v = Visitor(self.port)
        for bad in ("/etc", "../../etc", "/tmp", "C:\\Windows", "demo/..", ".", "", "a" * 60):
            st, r = v.post("/api/scan", {"dir": bad})
            self.assertEqual(st, 400, bad)
            self.assertIn("error", r)
        st, r = v.post("/api/rollback", {"dir": "/etc"})
        self.assertEqual(st, 400)

    def test_visitors_are_isolated(self):
        a, b = Visitor(self.port), Visitor(self.port)
        a.post("/api/demo", {})
        st, scan = a.post("/api/scan", {"dir": "demo"})
        self.assertEqual(st, 200)
        # B has no demo folder yet, and cannot use A's scan session.
        st, _ = b.post("/api/scan", {"dir": "demo"})
        self.assertEqual(st, 400)
        st, r = b.post("/api/apply", {"session": scan["session"], "approved": [f["n"] for f in scan["files"]]})
        self.assertEqual(st, 400)
        st, r = b.post("/api/replan", {"session": scan["session"]})
        self.assertEqual(st, 400)

    def test_token_cookie_and_origin_are_required(self):
        v = Visitor(self.port)
        st, _ = v.post("/api/demo", {}, token=False)
        self.assertEqual(st, 403)
        st, _ = v.post("/api/demo", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        no_cookie = Visitor(self.port)
        no_cookie.cookie = None
        st, _ = no_cookie.post("/api/demo", {})
        self.assertEqual(st, 400)

    def test_upload_sanitises_paths_and_enforces_limits(self):
        v = Visitor(self.port)
        b64 = lambda s: base64.b64encode(s).decode()
        files = [
            {"path": "notes/a.txt", "b64": b64(b"sales sales sales\n" * 50), "mtime": 1_600_000_000_000},
            {"path": "notes/a_copy.txt", "b64": b64(b"sales sales sales\n" * 50), "mtime": 1_600_000_000_000},
            {"path": "c.csv", "b64": b64(b"x,y\n1,2\n" * 100)},
        ]
        st, r = v.post("/api/upload", {"files": files})
        self.assertEqual((st, r["count"], r["dir"]), (200, 3, "upload"))
        st, scan = v.post("/api/scan", {"dir": "upload"})
        self.assertEqual(st, 200)
        self.assertTrue(any(f["dup"] for f in scan["files"]))

        for bad in ("../../x.txt", "a/../../x.txt", "/abs.txt", ".greendata/x"):
            st, r = v.post("/api/upload", {"files": [{"path": bad, "b64": b64(b"hi")}]})
            if bad == "/abs.txt":  # a leading slash is just stripped, the file lands inside the sandbox
                self.assertEqual(st, 200)
            else:
                self.assertEqual(st, 400, bad)
        sandbox = os.path.join(hs.SANDBOX, v.cookie.split("=")[1])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(sandbox), "x.txt")))

        big = [{"path": "big.bin", "b64": b64(b"0" * (hs.MAX_FILE_BYTES + 1))}]
        self.assertEqual(v.post("/api/upload", {"files": big})[0], 400)
        many = [{"path": f"f{i}.txt", "b64": b64(b"a")} for i in range(hs.MAX_FILES + 1)]
        self.assertEqual(v.post("/api/upload", {"files": many})[0], 400)
        self.assertEqual(v.post("/api/upload", {"files": [{"path": "a.txt", "b64": "!!!not base64"}]})[0], 400)

    def test_old_sandboxes_are_swept(self):
        sid = "ab" * 16
        p = os.path.join(hs.SANDBOX, sid)
        os.makedirs(p, exist_ok=True)
        old = os.path.getmtime(p) - hs.TTL_SECONDS - 10
        os.utime(p, (old, old))
        hs.sweep_sandboxes()
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
